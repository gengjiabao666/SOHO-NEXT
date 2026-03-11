#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser/session.py
登录与 Cookie 管理模块

功能：
    1. 优先尝试用已保存的 Cookie 登录（无需重新输入账号密码）
    2. Cookie 失效时，从 .env 读取账号密码重新登录
    3. 支持多账号（逗号分隔），某账号失败时自动切换下一个
    4. 登录成功后将 Cookie 保存到 config/cookies_{hash}.json
    5. 登录失败时自动截图保存到 logs/debug_login_*.png，便于排查

调试技巧：
    设置 .env 中 BROWSER_HEADLESS=false 可以看到真实浏览器窗口，
    直观确认脚本在哪一步出了问题。
"""

import base64
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import BrowserContext, Page

from utils.logger import log_node

load_dotenv()

# 登录成功后页面上才会出现的元素
# 基于录制结果：登录后显示 "Hi, 用户名" 字样
LOGIN_SUCCESS_SELECTOR = "text=Hi,"

# 登录失败时页面可能出现的错误提示关键词
LOGIN_ERROR_KEYWORDS = [
    "incorrect", "invalid", "wrong", "error",
    "不正确", "错误", "失败", "账号或密码", "密码错误",
]

COOKIE_DIR = Path("config")
LOG_DIR    = Path("logs")


class BrowserSession:
    """管理浏览器登录态，支持 Cookie 复用和多账号切换"""

    def __init__(self, context: BrowserContext):
        self.context = context
        self._accounts = self._load_accounts()

    def set_single_account(self, account: str, password: str):
        """指定使用单个账号登录（由 trigger 多账号轮换调用）"""
        self._accounts = [{"account": account, "password": password}]
        log_node("指定单账号登录", level="INFO",
                 account=self._mask(account))

    def _load_accounts(self) -> list[dict]:
        accounts_str  = os.getenv("ECHOTIK_ACCOUNTS", "")
        passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")

        # 去除首尾空白；同时去除用户可能误加的引号
        accounts  = [a.strip().strip('"').strip("'")
                     for a in accounts_str.split(",") if a.strip()]
        passwords = [p.strip().strip('"').strip("'")
                     for p in passwords_str.split(",") if p.strip()]

        if not accounts:
            raise RuntimeError("未配置 ECHOTIK_ACCOUNTS，请检查 .env 文件")

        pairs = [
            {"account": a, "password": passwords[i] if i < len(passwords) else ""}
            for i, a in enumerate(accounts)
        ]

        # 启动时打印脱敏诊断信息，帮助排查 .env 配置问题
        for p in pairs:
            pwd = p["password"]
            log_node(
                "账号信息加载",
                level="INFO",
                account=self._mask(p["account"]),
                password_len=len(pwd),
                password_hint=f"{pwd[:1]}***{pwd[-1:]}" if len(pwd) >= 2 else "（空）",
            )

        return pairs

    def _cookie_path(self, account: str) -> Path:
        h = hashlib.md5(account.encode()).hexdigest()[:8]
        return COOKIE_DIR / f"cookies_{h}.json"

    def _mask(self, account: str) -> str:
        if "@" not in account:
            return account[:3] + "***"
        name, domain = account.split("@", 1)
        return name[:3] + "***@" + domain

    async def _dismiss_popup(self, page: Page):
        """
        关闭登录后可能出现的弹窗（如「New version has arrived」）
        点击「Start Now」按钮，弹窗消失后侧边栏才可操作。
        如果没有弹窗则静默跳过。
        """
        log_node("检查是否有弹窗需要关闭...", level="INFO")

        # 等待一下让弹窗有时间出现
        await page.wait_for_timeout(2000)

        popup_selectors = [
            "button:has-text('Start Now')",
            "button:has-text('start now')",
            "button:has-text('立即开始')",
            "button:has-text('知道了')",
            "button:has-text('确定')",
            "button:has-text('我知道了')",
            "[class*='modal'] button:last-child",
            "[class*='dialog'] button:last-child",
            "[class*='popup'] button",
        ]

        popup_found = False
        for sel in popup_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count > 0:
                    # 检查是否可见
                    is_visible = await loc.first.is_visible()
                    if is_visible:
                        await loc.first.click(timeout=5_000)
                        log_node("弹窗已点击关闭", level="INFO", selector=sel)
                        popup_found = True

                        # 弹窗关闭后等待3秒，截图确认页面正常后进入后续流程
                        await page.wait_for_timeout(3_000)
                        await self._save_debug_screenshot(page, "popup_check_3s")
                        log_node("弹窗关闭后3秒截图，开始后续流程", level="INFO")

                        break
            except Exception as e:
                log_node("尝试关闭弹窗失败", level="DEBUG",
                        selector=sel[:50], error=str(e)[:60])
                continue

        if not popup_found:
            log_node("未检测到弹窗，继续", level="INFO")
            await self._save_debug_screenshot(page, "no_popup_sidebar_ready")

    async def _save_debug_screenshot(self, page: Page, label: str):
        """保存调试截图到 logs/debug_login_*.png"""
        try:
            LOG_DIR.mkdir(exist_ok=True)
            ts   = datetime.now().strftime("%H%M%S")
            path = LOG_DIR / f"debug_login_{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=False)
            log_node("调试截图已保存", level="INFO", path=str(path))
        except Exception as e:
            log_node("调试截图失败", level="WARN", error=str(e)[:60])

    async def _check_page_for_login_error(self, page: Page) -> str:
        """扫描页面文字，找出登录错误提示信息"""
        try:
            text = (await page.inner_text("body", timeout=3_000)).lower()
            for kw in LOGIN_ERROR_KEYWORDS:
                if kw.lower() in text:
                    # 截取关键词周围的上下文（最多60字）
                    idx   = text.find(kw.lower())
                    start = max(0, idx - 15)
                    end   = min(len(text), idx + 45)
                    return text[start:end].strip().replace("\n", " ")
        except Exception:
            pass
        return ""

    async def ensure_login(self, page: Page) -> bool:
        """
        确保当前页面处于登录状态

        策略：
            1. 遍历已保存的 Cookie，尝试无感登录
            2. Cookie 失效时，用账号密码重新登录
            3. 全部失败则抛出异常，并保存截图供排查
        """
        for acc in self._accounts:
            account     = acc["account"]
            masked      = self._mask(account)
            cookie_path = self._cookie_path(account)

            # ── 尝试 Cookie 登录 ──
            if cookie_path.exists():
                log_node("尝试复用Cookie登录", level="INFO", account=masked)
                try:
                    with open(cookie_path) as f:
                        cookies = json.load(f)
                    await self.context.add_cookies(cookies)
                    await page.goto("https://www.echotik.live", timeout=30_000)
                    await page.wait_for_timeout(2000)

                    if await page.locator(LOGIN_SUCCESS_SELECTOR).count() > 0:
                        log_node("Cookie有效，登录成功", level="INFO", account=masked)
                        return True

                    log_node("Cookie已失效，清除并改用账号密码登录",
                             level="WARN", account=masked)
                    await self.context.clear_cookies()
                except Exception as e:
                    log_node("Cookie登录出错", level="WARN",
                             account=masked, error=str(e)[:80])
                    await self.context.clear_cookies()

            # ── 账号密码登录 ──
            log_node("开始账号密码登录流程", level="INFO", account=masked)
            try:
                success = await self._do_login(page, account, acc["password"])
                if success:
                    cookies = await self.context.cookies()
                    COOKIE_DIR.mkdir(exist_ok=True)
                    with open(cookie_path, "w") as f:
                        json.dump(cookies, f, ensure_ascii=False, indent=2)
                    log_node("Cookie已保存", level="INFO",
                             path=str(cookie_path))
                    return True

                log_node("账号密码登录后未检测到成功标志，跳过此账号",
                         level="WARN", account=masked)

            except Exception as e:
                log_node("账号密码登录抛出异常", level="WARN",
                         account=masked, error=str(e)[:120])
                continue

        raise RuntimeError("所有账号均登录失败，请查看 logs/debug_login_*.png 截图排查原因")

    async def _do_login(self, page: Page, account: str, password: str) -> bool:
        """
        执行账号密码登录流程，带完整的步骤日志和失败截图
        """
        masked = self._mask(account)

        # ── 步骤1：导航到登录页 ──
        log_node("步骤1/5 导航到登录页", level="INFO", account=masked)
        await page.goto("https://www.echotik.live/login", timeout=30_000)
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        log_node("登录页加载完成", level="INFO", url=page.url)

        # ── 步骤2：等待表单元素可交互 ──
        log_node("步骤2/5 等待表单元素可交互", level="INFO")
        try:
            email_input = page.get_by_role("textbox", name="Email")
            await email_input.wait_for(state="visible", timeout=8_000)
            log_node("账号输入框已就绪", level="INFO")
        except Exception as e:
            log_node("账号输入框等待超时，尝试截图后继续",
                     level="WARN", error=str(e)[:80])
            await self._save_debug_screenshot(page, "step2_no_form")

        # ── 步骤3：填写账号和密码 ──
        log_node("步骤3/5 填写账号和密码", level="INFO",
                 account=masked,
                 password_len=len(password))
        try:
            await page.get_by_role("textbox", name="Email").fill(account)
            log_node("账号已填入", level="INFO")

            await page.get_by_role("textbox", name="Password").fill(password)
            log_node("密码已填入", level="INFO",
                     password_len=len(password))
        except Exception as e:
            log_node("填写表单失败", level="ERROR", error=str(e)[:120])
            await self._save_debug_screenshot(page, "step3_fill_failed")
            raise

        # 截图记录填写状态（调试用）
        await self._save_debug_screenshot(page, "step3_filled")

        # ── 步骤4：点击登录按钮 ──
        log_node("步骤4/5 点击登录按钮", level="INFO")
        try:
            await page.get_by_role("button", name="Login", exact=True).click()
            log_node("登录按钮已点击", level="INFO")
        except Exception as e:
            log_node("点击登录按钮失败", level="ERROR", error=str(e)[:120])
            await self._save_debug_screenshot(page, "step4_click_failed")
            raise

        # ── 步骤5：等待登录结果 ──
        log_node("步骤5/5 等待登录结果（最多10秒）", level="INFO")
        await page.wait_for_timeout(2000)

        # 尝试等待页面跳转或成功标志出现
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            log_node("等待networkidle超时，继续检查登录状态", level="WARN")

        current_url = page.url
        log_node("登录后当前URL", level="INFO", url=current_url)

        # 检查是否有错误提示
        error_text = await self._check_page_for_login_error(page)
        if error_text:
            log_node("页面检测到登录错误提示", level="ERROR",
                     error_on_page=error_text)
            await self._save_debug_screenshot(page, "step5_login_error")
            return False

        # 检查成功标志
        success = await page.locator(LOGIN_SUCCESS_SELECTOR).count() > 0
        if success:
            log_node("检测到登录成功标志，登录完成",
                     level="INFO", account=masked, url=current_url)
            # 等待15秒让页面完全加载（Echotik首页需约30秒完全加载）
            log_node("等待页面完全加载（20秒）...", level="INFO")
            await page.wait_for_timeout(20_000)
            await self._save_debug_screenshot(page, "login_success_board")

            # 关闭「New version has arrived」弹窗（点 Start Now）
            await self._dismiss_popup(page)
        else:
            log_node("未检测到登录成功标志",
                     level="WARN",
                     selector=LOGIN_SUCCESS_SELECTOR,
                     url=current_url,
                     hint="请查看截图确认页面实际状态")
            await self._save_debug_screenshot(page, "step5_no_success_flag")

        return success