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

# 标准库：Base64 编解码（本模块未直接使用，但可能被间接依赖）
import base64
# 标准库：哈希算法（用于根据账号生成 Cookie 文件名）
import hashlib
# 标准库：JSON 序列化/反序列化（用于读写 Cookie 文件）
import json
# 标准库：操作系统接口（读取环境变量）
import os
# 标准库：日期时间（用于生成截图文件名中的时间戳）
from datetime import datetime
# 标准库：路径操作（用于构建 Cookie 和日志文件路径）
from pathlib import Path

# 第三方库：从 .env 文件加载环境变量到 os.environ
from dotenv import load_dotenv
# Playwright 异步 API：BrowserContext 代表一个浏览器上下文（含 Cookie 等状态），Page 代表一个标签页
from playwright.async_api import BrowserContext, Page

# 项目自定义的结构化日志工具
from utils.logger import log_node
from utils.events import write_event, STAGE_LOGIN

# 加载 .env 文件中的环境变量（如 ECHOTIK_ACCOUNTS、ECHOTIK_PASSWORDS 等）
load_dotenv()

# 登录成功后页面上才会出现的元素
# 基于录制结果：登录后显示 "Hi, 用户名" 字样，用于判断是否已登录
LOGIN_SUCCESS_SELECTOR = "text=Hi,"

# 登录失败时页面可能出现的错误提示关键词（中英文都覆盖）
# 用于在登录后扫描页面文本，检测是否有错误提示
LOGIN_ERROR_KEYWORDS = [
    "incorrect", "invalid", "wrong", "error",
    "不正确", "错误", "失败", "账号或密码", "密码错误",
]

# Cookie 文件保存目录，格式为 config/cookies_{账号MD5前8位}.json
COOKIE_DIR = Path("config")
# 调试截图保存目录，格式为 logs/debug_login_{标签}_{时间戳}.png
LOG_DIR    = Path("logs")


class BrowserSession:
    """
    管理浏览器登录态，支持 Cookie 复用和多账号切换

    核心职责：
    1. 管理多个 Echotik 账号的登录凭证
    2. 优先使用已保存的 Cookie 实现免密登录
    3. Cookie 失效时自动切换到账号密码登录
    4. 登录成功后保存 Cookie 供下次复用
    5. 全程记录详细日志和截图，便于排查问题
    """

    def __init__(self, context: BrowserContext):
        """
        初始化浏览器会话管理器

        参数:
            context: Playwright 浏览器上下文，包含 Cookie 等浏览器状态
        """
        # 保存浏览器上下文引用，后续用于操作 Cookie
        self.context = context
        # 从环境变量加载所有账号密码对
        self._accounts = self._load_accounts()

    def set_single_account(self, account: str, password: str):
        """
        指定使用单个账号登录（由 trigger 多账号轮换调用）

        当外部调度器（trigger）需要指定特定账号时调用此方法，
        覆盖从 .env 加载的账号列表，只保留指定的单个账号

        参数:
            account: 账号（邮箱）
            password: 密码
        """
        # 用单个账号覆盖账号列表
        self._accounts = [{"account": account, "password": password}]
        # 记录日志，账号做脱敏处理
        log_node("指定单账号登录", level="INFO",
                 account=self._mask(account))

    def _load_accounts(self) -> list[dict]:
        """
        从环境变量加载账号密码列表

        从 .env 中读取 ECHOTIK_ACCOUNTS 和 ECHOTIK_PASSWORDS，
        两者都用逗号分隔多个值。账号和密码按位置一一对应。
        如果密码数量少于账号数量，缺少的密码默认为空字符串。

        返回:
            账号密码字典列表，每个元素为 {"account": "xxx", "password": "yyy"}

        异常:
            RuntimeError: 未配置 ECHOTIK_ACCOUNTS 时抛出
        """
        # 从环境变量读取逗号分隔的账号和密码字符串
        accounts_str  = os.getenv("ECHOTIK_ACCOUNTS", "")
        passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")

        # 按逗号分割，去除首尾空白；同时去除用户可能误加的引号
        accounts  = [a.strip().strip('"').strip("'")
                     for a in accounts_str.split(",") if a.strip()]
        passwords = [p.strip().strip('"').strip("'")
                     for p in passwords_str.split(",") if p.strip()]

        # 账号列表为空时直接报错，无法继续
        if not accounts:
            raise RuntimeError("未配置 ECHOTIK_ACCOUNTS，请检查 .env 文件")

        # 将账号和密码按索引配对，密码不足时用空字符串填充
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
                account=self._mask(p["account"]),  # 账号脱敏显示
                password_len=len(pwd),               # 只显示密码长度
                # 只显示密码首尾各一个字符，中间用 *** 代替
                password_hint=f"{pwd[:1]}***{pwd[-1:]}" if len(pwd) >= 2 else "（空）",
            )

        return pairs

    def _cookie_path(self, account: str) -> Path:
        """
        根据账号生成对应的 Cookie 文件路径

        使用账号的 MD5 哈希前8位作为文件名的一部分，
        避免文件名中出现特殊字符（如 @ 等），同时保证不同账号对应不同文件

        参数:
            account: 账号字符串（通常是邮箱）

        返回:
            Cookie 文件路径，如 config/cookies_a1b2c3d4.json
        """
        # 对账号做 MD5 哈希，取前8位作为唯一标识
        h = hashlib.md5(account.encode()).hexdigest()[:8]
        return COOKIE_DIR / f"cookies_{h}.json"

    def _mask(self, account: str) -> str:
        """
        对账号进行脱敏处理，用于日志输出

        邮箱格式：显示用户名前3个字符 + *** + @域名
        非邮箱格式：显示前3个字符 + ***

        参数:
            account: 原始账号字符串

        返回:
            脱敏后的账号字符串，如 "abc***@gmail.com"
        """
        # 非邮箱格式，直接截取前3个字符
        if "@" not in account:
            return account[:3] + "***"
        # 邮箱格式，分别处理用户名和域名部分
        name, domain = account.split("@", 1)
        return name[:3] + "***@" + domain

    async def _dismiss_popup(self, page: Page):
        """
        关闭登录后可能出现的弹窗（如「New version has arrived」）
        点击「Start Now」按钮，弹窗消失后侧边栏才可操作。
        如果没有弹窗则静默跳过。

        Echotik 登录后经常弹出版本更新提示或公告弹窗，
        如果不关闭会遮挡页面元素，导致后续操作失败。
        本方法会依次尝试多个常见的弹窗关闭按钮选择器。
        """
        log_node("检查是否有弹窗需要关闭...", level="INFO")

        # 等待2秒让弹窗有时间渲染出来
        await page.wait_for_timeout(2000)

        # 常见弹窗关闭按钮的 CSS 选择器列表，按优先级排列
        popup_selectors = [
            "button:has-text('Start Now')",       # Echotik 版本更新弹窗
            "button:has-text('start now')",       # 同上（小写变体）
            "button:has-text('立即开始')",         # 中文版本
            "button:has-text('知道了')",           # 通用确认弹窗
            "button:has-text('确定')",             # 通用确认弹窗
            "button:has-text('我知道了')",         # 通用确认弹窗
            "[class*='modal'] button:last-child",  # 通用模态框的最后一个按钮（通常是确认/关闭）
            "[class*='dialog'] button:last-child", # 通用对话框的最后一个按钮
            "[class*='popup'] button",             # 通用弹窗按钮
        ]

        # 标记是否找到并关闭了弹窗
        popup_found = False
        for sel in popup_selectors:
            try:
                # 查找匹配选择器的元素
                loc = page.locator(sel)
                count = await loc.count()
                if count > 0:
                    # 元素存在，进一步检查是否可见（避免点击隐藏元素）
                    is_visible = await loc.first.is_visible()
                    if is_visible:
                        # 点击第一个匹配的可见元素来关闭弹窗
                        await loc.first.click(timeout=5_000)
                        log_node("弹窗已点击关闭", level="INFO", selector=sel)
                        popup_found = True

                        # 弹窗关闭后等待3秒，让页面恢复正常状态
                        await page.wait_for_timeout(3_000)
                        # 截图确认弹窗已关闭、页面正常
                        await self._save_debug_screenshot(page, "popup_check_3s")
                        log_node("弹窗关闭后3秒截图，开始后续流程", level="INFO")

                        # 已成功关闭弹窗，不再尝试其他选择器
                        break
            except Exception as e:
                # 单个选择器尝试失败不影响整体流程，继续尝试下一个
                log_node("尝试关闭弹窗失败", level="DEBUG",
                        selector=sel[:50], error=str(e)[:60])
                continue

        # 如果所有选择器都没有匹配到弹窗，记录日志
        if not popup_found:
            log_node("未检测到弹窗，继续", level="INFO")
            await self._save_debug_screenshot(page, "no_popup_sidebar_ready")

    async def _save_debug_screenshot(self, page: Page, label: str):
        """
        保存调试截图到 logs/debug_login_*.png

        在登录流程的各个关键步骤保存截图，便于事后排查问题。
        截图文件名包含步骤标签和时间戳，方便按时间顺序查看。

        参数:
            page: Playwright 页面对象
            label: 截图标签（如 "step3_filled"、"login_success_board"）
        """
        try:
            # 确保日志目录存在
            LOG_DIR.mkdir(exist_ok=True)
            # 生成时间戳（时分秒格式），用于文件名去重
            ts   = datetime.now().strftime("%H%M%S")
            # 拼接截图文件路径
            path = LOG_DIR / f"debug_login_{label}_{ts}.png"
            # 截取当前可视区域并保存为 PNG
            await page.screenshot(path=str(path), full_page=False)
            log_node("调试截图已保存", level="INFO", path=str(path))
        except Exception as e:
            # 截图失败不影响主流程，仅记录警告
            log_node("调试截图失败", level="WARN", error=str(e)[:60])

    async def _check_page_for_login_error(self, page: Page) -> str:
        """
        扫描页面文字，找出登录错误提示信息

        获取页面 body 的全部文本，逐一匹配 LOGIN_ERROR_KEYWORDS 中的关键词。
        如果命中，截取关键词周围的上下文文本（前15字+后30字）作为错误信息返回。

        参数:
            page: Playwright 页面对象

        返回:
            错误提示文本（命中时），或空字符串（未命中时）
        """
        try:
            # 获取页面 body 全部文本，转小写用于不区分大小写的匹配
            text = (await page.inner_text("body", timeout=3_000)).lower()
            # 遍历所有错误关键词
            for kw in LOGIN_ERROR_KEYWORDS:
                if kw.lower() in text:
                    # 命中关键词，截取其周围的上下文（前15字 + 关键词 + 后30字）
                    idx   = text.find(kw.lower())
                    start = max(0, idx - 15)       # 向前取15个字符，不超出开头
                    end   = min(len(text), idx + 45)  # 向后取45个字符，不超出结尾
                    # 返回清理后的上下文文本（去除首尾空白，换行替换为空格）
                    return text[start:end].strip().replace("\n", " ")
        except Exception:
            # 页面文本读取失败时静默跳过
            pass
        # 未命中任何错误关键词，返回空字符串
        return ""

    async def ensure_login(self, page: Page) -> bool:
        """
        确保当前页面处于登录状态

        这是本类的核心对外方法，其他模块通过调用此方法来确保浏览器已登录 Echotik。

        策略：
            1. 遍历已保存的 Cookie，尝试无感登录（免输入账号密码）
            2. Cookie 失效时，用账号密码重新登录
            3. 登录成功后保存新的 Cookie 供下次复用
            4. 某个账号失败时自动尝试下一个账号
            5. 全部失败则抛出异常，并保存截图供排查

        参数:
            page: Playwright 页面对象

        返回:
            True 表示登录成功

        异常:
            RuntimeError: 所有账号均登录失败时抛出
        """
        # 遍历所有配置的账号，依次尝试登录
        for acc in self._accounts:
            account     = acc["account"]
            masked      = self._mask(account)       # 脱敏后的账号，用于日志
            cookie_path = self._cookie_path(account)  # 该账号对应的 Cookie 文件路径

            # ── 尝试 Cookie 登录（优先，速度快且无需输入密码） ──
            if cookie_path.exists():
                log_node("尝试复用Cookie登录", level="INFO", account=masked)
                try:
                    # 读取已保存的 Cookie 文件
                    with open(cookie_path) as f:
                        cookies = json.load(f)
                    # 将 Cookie 注入浏览器上下文
                    await self.context.add_cookies(cookies)
                    # 导航到 Echotik 首页，触发 Cookie 验证
                    await page.goto("https://www.echotik.live", timeout=30_000)
                    # 等待页面加载
                    await page.wait_for_timeout(2000)

                    # 检查页面是否出现登录成功标志（"Hi, 用户名"）
                    if await page.locator(LOGIN_SUCCESS_SELECTOR).count() > 0:
                        log_node("Cookie有效，登录成功", level="INFO", account=masked)
                        write_event(STAGE_LOGIN, "SUCCESS", context={"account": masked, "method": "cookie"})
                        return True

                    # Cookie 已失效（页面未出现登录成功标志），清除 Cookie 后改用密码登录
                    log_node("Cookie已失效，清除并改用账号密码登录",
                             level="WARN", account=masked)
                    await self.context.clear_cookies()
                except Exception as e:
                    # Cookie 登录过程出错（如文件损坏、网络问题），清除 Cookie 后继续
                    log_node("Cookie登录出错", level="WARN",
                             account=masked, error=str(e)[:80])
                    await self.context.clear_cookies()

            # ── 账号密码登录（Cookie 不存在或已失效时的备选方案） ──
            log_node("开始账号密码登录流程", level="INFO", account=masked)
            try:
                # 调用实际的登录流程
                success = await self._do_login(page, account, acc["password"])
                if success:
                    # 登录成功，保存 Cookie 供下次复用
                    cookies = await self.context.cookies()
                    COOKIE_DIR.mkdir(exist_ok=True)  # 确保 config 目录存在
                    with open(cookie_path, "w") as f:
                        json.dump(cookies, f, ensure_ascii=False, indent=2)
                    log_node("Cookie已保存", level="INFO",
                             path=str(cookie_path))
                    write_event(STAGE_LOGIN, "SUCCESS", context={"account": masked, "method": "password"})
                    return True

                # 登录后未检测到成功标志，跳过此账号尝试下一个
                log_node("账号密码登录后未检测到成功标志，跳过此账号",
                         level="WARN", account=masked)

            except Exception as e:
                # 登录过程抛出异常，记录后继续尝试下一个账号
                log_node("账号密码登录抛出异常", level="WARN",
                         account=masked, error=str(e)[:120])
                continue

        # 所有账号都尝试完毕仍未成功，抛出异常终止流程
        write_event(STAGE_LOGIN, "FAILED", detail="所有账号均登录失败")
        raise RuntimeError("所有账号均登录失败，请查看 logs/debug_login_*.png 截图排查原因")

    async def _do_login(self, page: Page, account: str, password: str) -> bool:
        """
        执行账号密码登录流程，带完整的步骤日志和失败截图

        整个登录流程分为5个步骤：
        1. 导航到登录页
        2. 等待表单元素可交互
        3. 填写账号和密码
        4. 点击登录按钮
        5. 等待并检查登录结果

        每个步骤都有详细日志和失败截图，便于排查问题。

        参数:
            page: Playwright 页面对象
            account: 账号（邮箱）
            password: 密码

        返回:
            True 表示登录成功，False 表示登录失败
        """
        # 生成脱敏账号，用于日志输出
        masked = self._mask(account)

        # ── 步骤1：导航到登录页 ──
        log_node("步骤1/5 导航到登录页", level="INFO", account=masked)
        # 打开 Echotik 登录页面，30秒超时
        await page.goto("https://www.echotik.live/login", timeout=30_000)
        # 等待 DOM 内容加载完成（不等待图片等资源）
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        log_node("登录页加载完成", level="INFO", url=page.url)

        # ── 步骤2：等待表单元素可交互 ──
        log_node("步骤2/5 等待表单元素可交互", level="INFO")
        try:
            # 通过 ARIA role 和 name 定位邮箱输入框
            email_input = page.get_by_role("textbox", name="Email")
            # 等待输入框变为可见状态，最多等8秒
            await email_input.wait_for(state="visible", timeout=8_000)
            log_node("账号输入框已就绪", level="INFO")
        except Exception as e:
            # 输入框等待超时，可能是页面结构变化，截图后继续尝试
            log_node("账号输入框等待超时，尝试截图后继续",
                     level="WARN", error=str(e)[:80])
            await self._save_debug_screenshot(page, "step2_no_form")

        # ── 步骤3：填写账号和密码 ──
        log_node("步骤3/5 填写账号和密码", level="INFO",
                 account=masked,
                 password_len=len(password))
        try:
            # 定位邮箱输入框并填入账号
            await page.get_by_role("textbox", name="Email").fill(account)
            log_node("账号已填入", level="INFO")

            # 定位密码输入框并填入密码
            await page.get_by_role("textbox", name="Password").fill(password)
            log_node("密码已填入", level="INFO",
                     password_len=len(password))
        except Exception as e:
            # 填写失败（如元素不存在或不可交互），截图后抛出异常
            log_node("填写表单失败", level="ERROR", error=str(e)[:120])
            await self._save_debug_screenshot(page, "step3_fill_failed")
            raise

        # 截图记录填写状态（调试用，确认账号密码是否正确填入）
        await self._save_debug_screenshot(page, "step3_filled")

        # ── 步骤4：点击登录按钮 ──
        log_node("步骤4/5 点击登录按钮", level="INFO")
        try:
            # 通过 ARIA role 精确匹配 "Login" 按钮并点击
            await page.get_by_role("button", name="Login", exact=True).click()
            log_node("登录按钮已点击", level="INFO")
        except Exception as e:
            # 点击失败（如按钮被遮挡或不存在），截图后抛出异常
            log_node("点击登录按钮失败", level="ERROR", error=str(e)[:120])
            await self._save_debug_screenshot(page, "step4_click_failed")
            raise

        # ── 步骤5：等待登录结果 ──
        log_node("步骤5/5 等待登录结果（最多10秒）", level="INFO")
        # 先等待2秒，让服务器处理登录请求
        await page.wait_for_timeout(2000)

        # 尝试等待页面网络请求全部完成（登录成功后通常会有页面跳转）
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            # networkidle 超时不影响后续判断，继续检查登录状态
            log_node("等待networkidle超时，继续检查登录状态", level="WARN")

        # 记录登录后的当前 URL（用于判断是否发生了页面跳转）
        current_url = page.url
        log_node("登录后当前URL", level="INFO", url=current_url)

        # 检查页面是否有错误提示（如"密码错误"、"账号不存在"等）
        error_text = await self._check_page_for_login_error(page)
        if error_text:
            log_node("页面检测到登录错误提示", level="ERROR",
                     error_on_page=error_text)
            await self._save_debug_screenshot(page, "step5_login_error")
            return False

        # 检查页面是否出现登录成功标志（"Hi, 用户名"）
        success = await page.locator(LOGIN_SUCCESS_SELECTOR).count() > 0
        if success:
            log_node("检测到登录成功标志，登录完成",
                     level="INFO", account=masked, url=current_url)
            # 等待20秒让页面完全加载（Echotik 首页加载较慢，需要较长时间）
            log_node("等待页面完全加载（20秒）...", level="INFO")
            await page.wait_for_timeout(20_000)
            # 截图记录登录成功后的页面状态
            await self._save_debug_screenshot(page, "login_success_board")

            # 关闭登录后可能出现的「New version has arrived」等弹窗
            await self._dismiss_popup(page)
        else:
            # 未检测到成功标志，可能是页面结构变化或登录实际失败
            log_node("未检测到登录成功标志",
                     level="WARN",
                     selector=LOGIN_SUCCESS_SELECTOR,
                     url=current_url,
                     hint="请查看截图确认页面实际状态")
            await self._save_debug_screenshot(page, "step5_no_success_flag")

        return success