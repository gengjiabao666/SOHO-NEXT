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
from utils.events import write_event, STAGE_LOGIN

load_dotenv()

# 登录成功判断：不再只依赖 "Hi,"，同时兼容 board 页和侧边栏关键元素
LOGIN_SUCCESS_SELECTORS = [
    "text=Hi,",
    "text=Top Sold",
    "text=New Products",
    "text=Best Cross-border Seller",
    "text=Product Category",
]

LOGIN_SUCCESS_URL_HINTS = [
    "/board",
    "/products/",
    "/shop/",
    "/leaderboard/",
]

APP_ENTRY_CANDIDATES = [
    "https://www.echotik.live/en/products/top-sold",
    "https://www.echotik.live/products/top-sold",
    "https://www.echotik.live/en/leaderboard/product-ranking",
    "https://www.echotik.live/leaderboard/product-ranking",
    "https://www.echotik.live/en/board",
    "https://www.echotik.live/board",
    "https://www.echotik.live/en",
]

LOGIN_ERROR_KEYWORDS = [
    "incorrect", "invalid", "wrong", "error",
    "not match", "too many", "try again", "verification",
    "captcha", "blocked", "forbidden", "denied",
    "不正确", "错误", "失败", "账号或密码", "密码错误",
    "验证码", "验证", "请稍后再试", "频繁", "受限",
]

COOKIE_DIR = Path("config")
LOG_DIR = Path("logs")


class BrowserSession:
    """管理浏览器登录态，支持 Cookie 复用和多账号切换。"""

    def __init__(self, context: BrowserContext):
        self.context = context
        self._accounts = self._load_accounts()

    def set_single_account(self, account: str, password: str):
        self._accounts = [{"account": account, "password": password}]
        log_node("指定单账号登录", level="INFO", account=self._mask(account))

    def _load_accounts(self) -> list[dict]:
        accounts_str = os.getenv("ECHOTIK_ACCOUNTS", "")
        passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")

        accounts = [a.strip().strip('"').strip("'") for a in accounts_str.split(",") if a.strip()]
        passwords = [p.strip().strip('"').strip("'") for p in passwords_str.split(",") if p.strip()]

        if not accounts:
            raise RuntimeError("未配置 ECHOTIK_ACCOUNTS，请检查 .env 文件")

        pairs = [
            {"account": a, "password": passwords[i] if i < len(passwords) else ""}
            for i, a in enumerate(accounts)
        ]

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

    async def _save_debug_screenshot(self, page: Page, label: str, full_page: bool = False):
        try:
            LOG_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            path = LOG_DIR / f"debug_login_{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=full_page)
            log_node("调试截图已保存", level="INFO", path=str(path))
        except Exception as e:
            log_node("调试截图失败", level="WARN", error=str(e)[:60])

    async def _get_page_text(self, page: Page, limit: int = 500) -> str:
        try:
            text = await page.inner_text("body", timeout=3_000)
            return " ".join(text.split())[:limit]
        except Exception:
            return ""

    async def _check_page_for_login_error(self, page: Page) -> str:
        try:
            text = (await page.inner_text("body", timeout=3_000)).lower()
            for kw in LOGIN_ERROR_KEYWORDS:
                if kw.lower() in text:
                    idx = text.find(kw.lower())
                    start = max(0, idx - 15)
                    end = min(len(text), idx + 60)
                    return text[start:end].strip().replace("\n", " ")
        except Exception:
            pass
        return ""

    async def _is_app_ready(self, page: Page) -> bool:
        current_url = page.url.lower()
        if not any(hint in current_url for hint in LOGIN_SUCCESS_URL_HINTS):
            return False

        for sel in LOGIN_SUCCESS_SELECTORS:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0:
                    return True
            except Exception:
                continue

        return False

    async def _ensure_app_entry(self, page: Page, account: str = "") -> bool:
        """确保进入真正可采集的业务页，而不是官网营销首页。"""
        for url in APP_ENTRY_CANDIDATES:
            try:
                log_node("尝试进入业务页", level="INFO", target=url)
                await page.goto(url, timeout=30_000)
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                await page.wait_for_timeout(2000)
                await self._dismiss_popup(page, stage="app_entry")
                if await self._is_app_ready(page):
                    log_node("业务页已就绪", level="INFO", url=page.url)
                    return True
            except Exception as e:
                log_node("业务页跳转失败", level="DEBUG", target=url, error=str(e)[:80])
                continue
        return False

    async def _dismiss_popup(self, page: Page, stage: str = "post_login") -> bool:
        log_node("检查是否有弹窗需要关闭...", level="INFO", stage=stage)
        await page.wait_for_timeout(1500)

        popup_selectors = [
            "button:has-text('Start Now')",
            "button:has-text('start now')",
            "button:has-text('Got it')",
            "button:has-text('Close')",
            "button:has-text('close')",
            "button:has-text('Continue')",
            "button:has-text('继续')",
            "button:has-text('立即开始')",
            "button:has-text('知道了')",
            "button:has-text('确定')",
            "button:has-text('我知道了')",
            "[aria-label='Close']",
            "[class*='close']",
            "[class*='Close']",
            "[class*='modal'] button:last-child",
            "[class*='dialog'] button:last-child",
            "[class*='popup'] button",
        ]

        popup_found = False
        for sel in popup_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count <= 0:
                    continue
                if await loc.first.is_visible():
                    await loc.first.click(timeout=5_000)
                    popup_found = True
                    log_node("弹窗已点击关闭", level="INFO", selector=sel, stage=stage)
                    await page.wait_for_timeout(1500)
                    await self._save_debug_screenshot(page, f"popup_{stage}")
                    break
            except Exception as e:
                log_node("尝试关闭弹窗失败", level="DEBUG", selector=sel[:50], error=str(e)[:60], stage=stage)
                continue

        if not popup_found:
            log_node("未检测到弹窗，继续", level="INFO", stage=stage)
        return popup_found

    async def ensure_login(self, page: Page) -> bool:
        for acc in self._accounts:
            account = acc["account"]
            masked = self._mask(account)
            cookie_path = self._cookie_path(account)

            if cookie_path.exists():
                log_node("尝试复用Cookie登录", level="INFO", account=masked)
                try:
                    with open(cookie_path) as f:
                        cookies = json.load(f)
                    await self.context.add_cookies(cookies)
                    await page.goto("https://www.echotik.live", timeout=30_000)
                    await page.wait_for_timeout(2000)
                    await self._dismiss_popup(page, stage="cookie_login")

                    if await self._ensure_app_entry(page, account=masked):
                        log_node("Cookie有效，登录成功", level="INFO", account=masked, url=page.url)
                        write_event(STAGE_LOGIN, "SUCCESS", context={"account": masked, "method": "cookie"})
                        return True

                    log_node("Cookie未进入业务页，清除并改用账号密码登录", level="WARN", account=masked, url=page.url)
                    await self.context.clear_cookies()
                except Exception as e:
                    log_node("Cookie登录出错", level="WARN", account=masked, error=str(e)[:80])
                    await self.context.clear_cookies()

            log_node("开始账号密码登录流程", level="INFO", account=masked)
            try:
                success = await self._do_login(page, account, acc["password"])
                if success:
                    cookies = await self.context.cookies()
                    COOKIE_DIR.mkdir(exist_ok=True)
                    with open(cookie_path, "w") as f:
                        json.dump(cookies, f, ensure_ascii=False, indent=2)
                    log_node("Cookie已保存", level="INFO", path=str(cookie_path))
                    write_event(STAGE_LOGIN, "SUCCESS", context={"account": masked, "method": "password"})
                    return True

                log_node("账号密码登录后未检测到成功标志，跳过此账号", level="WARN", account=masked)
            except Exception as e:
                log_node("账号密码登录抛出异常", level="WARN", account=masked, error=str(e)[:120])
                continue

        write_event(STAGE_LOGIN, "FAILED", detail="所有账号均登录失败")
        raise RuntimeError("所有账号均登录失败，请查看 logs/debug_login_*.png 截图排查原因")

    async def _do_login(self, page: Page, account: str, password: str) -> bool:
        masked = self._mask(account)

        log_node("步骤1/5 导航到登录页", level="INFO", account=masked)
        await page.goto("https://www.echotik.live/login", timeout=30_000)
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        log_node("登录页加载完成", level="INFO", url=page.url)

        # 先尝试清一次登录页自身的公告/遮罩
        await self._dismiss_popup(page, stage="login_page")

        log_node("步骤2/5 等待表单元素可交互", level="INFO")
        try:
            email_input = page.get_by_role("textbox", name="Email")
            await email_input.wait_for(state="visible", timeout=8_000)
            log_node("账号输入框已就绪", level="INFO")
        except Exception as e:
            log_node("账号输入框等待超时，尝试截图后继续", level="WARN", error=str(e)[:80])
            await self._save_debug_screenshot(page, "step2_no_form", full_page=True)

        log_node("步骤3/5 填写账号和密码", level="INFO", account=masked, password_len=len(password))
        try:
            await page.get_by_role("textbox", name="Email").fill(account)
            log_node("账号已填入", level="INFO")
            await page.get_by_role("textbox", name="Password").fill(password)
            log_node("密码已填入", level="INFO", password_len=len(password))
        except Exception as e:
            log_node("填写表单失败", level="ERROR", error=str(e)[:120])
            await self._save_debug_screenshot(page, "step3_fill_failed", full_page=True)
            raise

        await self._save_debug_screenshot(page, "step3_filled")

        log_node("步骤4/5 点击登录按钮", level="INFO")
        try:
            await page.get_by_role("button", name="Login", exact=True).click()
            log_node("登录按钮已点击", level="INFO")
        except Exception as e:
            log_node("点击登录按钮失败", level="ERROR", error=str(e)[:120])
            await self._save_debug_screenshot(page, "step4_click_failed", full_page=True)
            raise

        log_node("步骤5/5 等待登录结果（最多20秒）", level="INFO")
        await page.wait_for_timeout(2000)

        deadline_rounds = 18
        for i in range(deadline_rounds):
            try:
                await page.wait_for_load_state("networkidle", timeout=2_000)
            except Exception:
                pass

            # 登录阶段与登录后都尝试处理新增弹窗/遮罩
            await self._dismiss_popup(page, stage=f"after_login_click_{i+1}")

            current_url = page.url
            error_text = await self._check_page_for_login_error(page)
            if error_text:
                log_node("页面检测到登录错误提示", level="ERROR", error_on_page=error_text, url=current_url)
                await self._save_debug_screenshot(page, "step5_login_error", full_page=True)
                return False

            if await self._ensure_app_entry(page, account=masked):
                log_node("检测到登录成功标志，登录完成", level="INFO", account=masked, url=page.url, round=i + 1)
                log_node("等待页面完全加载（10秒）...", level="INFO")
                await page.wait_for_timeout(10_000)
                await self._dismiss_popup(page, stage="post_login_final")
                await self._save_debug_screenshot(page, "login_success_board", full_page=True)
                return True

            if i < deadline_rounds - 1:
                await page.wait_for_timeout(1000)

        current_url = page.url
        page_text = await self._get_page_text(page, limit=300)
        log_node(
            "未检测到登录成功标志",
            level="WARN",
            selectors=" | ".join(LOGIN_SUCCESS_SELECTORS[:4]),
            url=current_url,
            page_text=page_text,
            hint="请查看整页截图确认是否有新增弹窗/遮罩/验证",
        )
        await self._save_debug_screenshot(page, "step5_no_success_flag", full_page=True)
        return False
