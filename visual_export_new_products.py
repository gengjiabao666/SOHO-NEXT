#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
visual_export_new_products.py
视觉驱动的新品榜导出脚本

流程：
1. 启动 Xvfb 虚拟显示
2. 登录 Echotik（复用 session.py）
3. 视觉识别侧边栏 → 点击"选品"/"Product Selection"
4. 视觉识别 → 点击"新品榜"/"New Products"
5. 点击导出下拉 → 选择 200 条 → 导出
6. 保存到 test/ 目录
"""

# ==================== 标准库导入 ====================
import asyncio          # 异步IO框架，用于协程调度
import os               # 操作系统接口，用于环境变量和进程管理
import subprocess       # 子进程管理，用于启动 Xvfb
import sys              # 系统相关，用于修改模块搜索路径
from datetime import datetime  # 日期时间，用于截图文件名时间戳
from pathlib import Path       # 路径操作，跨平台文件路径处理

# ==================== 第三方库导入 ====================
from dotenv import load_dotenv            # 从 .env 文件加载环境变量
from playwright.async_api import async_playwright  # Playwright 异步API，浏览器自动化

# 添加项目根目录到 sys.path，确保能导入项目内部模块
sys.path.insert(0, str(Path(__file__).parent))

# ==================== 项目内部模块导入 ====================
from browser.session import BrowserSession  # 浏览器会话管理，处理登录逻辑
from utils.logger import log_node           # 统一日志输出工具

# 加载 .env 文件中的环境变量（如账号密码、代理端口等）
load_dotenv()

# 测试输出目录：存放导出的 Excel 文件
TEST_DIR = Path(__file__).parent / "test"
# 日志截图目录：存放每步操作的截图
LOG_DIR = Path(__file__).parent / "logs"


class VisualExporter:
    """
    视觉驱动的新品榜导出器。
    通过模拟真实用户操作（点击侧边栏菜单、选择导出条数、触发下载）
    来完成 Echotik 新品榜数据的自动导出。
    """

    def __init__(self):
        """初始化导出器，设置 Xvfb 进程引用和截图计数器"""
        self.xvfb_proc = None       # Xvfb 虚拟显示进程句柄
        self.screenshot_count = 0   # 截图序号计数器，用于文件名排序

    async def screenshot(self, page, label: str):
        """
        每步截图存证。
        在关键操作前后截图，方便调试和问题排查。

        参数:
            page: Playwright 页面对象
            label: 截图标签，用于文件名标识当前步骤
        返回:
            截图文件路径字符串，失败返回 None
        """
        # 递增截图序号
        self.screenshot_count += 1
        try:
            # 确保日志目录存在
            LOG_DIR.mkdir(exist_ok=True)
            # 生成时间戳，精确到秒
            ts = datetime.now().strftime("%H%M%S")
            # 构造截图文件路径：序号_标签_时间戳.png
            path = LOG_DIR / f"visual_{self.screenshot_count:02d}_{label}_{ts}.png"
            # 执行截图（不截取整个页面，只截取可视区域）
            await page.screenshot(path=str(path), full_page=False)
            log_node(f"📸 截图 #{self.screenshot_count}", level="INFO",
                     label=label, path=str(path))
            return str(path)
        except Exception as e:
            # 截图失败不影响主流程，仅记录警告
            log_node("截图失败", level="WARN", error=str(e)[:60])
            return None

    def start_xvfb(self):
        """
        启动 Xvfb 虚拟显示服务器。
        在无图形界面的 Linux 服务器上，Xvfb 提供虚拟 X11 显示，
        使 Playwright 的 headless=False 模式能正常工作并截图。
        """
        log_node("启动 Xvfb 虚拟显示", level="INFO")
        try:
            # 启动 Xvfb 进程，使用 :99 显示号，分辨率 1920x1080，24位色深
            self.xvfb_proc = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,  # 丢弃标准输出
                stderr=subprocess.DEVNULL   # 丢弃标准错误
            )
            # 设置 DISPLAY 环境变量，让浏览器使用虚拟显示
            os.environ["DISPLAY"] = ":99"
            log_node("Xvfb 已启动", level="INFO", display=":99")
        except Exception as e:
            log_node("Xvfb 启动失败", level="ERROR", error=str(e))
            raise

    def stop_xvfb(self):
        """
        停止 Xvfb 虚拟显示服务器。
        在脚本结束时调用，确保清理子进程资源。
        """
        if self.xvfb_proc:
            self.xvfb_proc.terminate()  # 发送 SIGTERM 信号
            self.xvfb_proc.wait()       # 等待进程退出
            log_node("Xvfb 已停止", level="INFO")

    async def dismiss_popup(self, page):
        """
        关闭页面上可能出现的弹窗（复用 session.py 的逻辑 + 视觉兜底）。
        Echotik 登录后可能弹出引导弹窗、公告弹窗等，需要先关闭才能操作页面。

        参数:
            page: Playwright 页面对象
        返回:
            True 表示成功关闭了弹窗，False 表示未检测到弹窗
        """
        log_node("检查弹窗...", level="INFO")
        # 等待 2 秒让弹窗充分渲染
        await page.wait_for_timeout(2000)

        # 定义多种弹窗关闭按钮的选择器，覆盖中英文和不同 UI 框架
        popup_selectors = [
            "button:has-text('Start Now')",          # 英文"立即开始"
            "button:has-text('start now')",          # 小写变体
            "button:has-text('立即开始')",            # 中文"立即开始"
            "button:has-text('知道了')",              # 中文"知道了"
            "button:has-text('确定')",                # 中文"确定"
            "button:has-text('OK')",                 # 英文确认
            "button:has-text('Close')",              # 英文关闭
            "[class*='modal'] button:last-child",    # 模态框最后一个按钮（通常是关闭/确认）
            "[class*='dialog'] button:last-child",   # 对话框最后一个按钮
        ]

        # 依次尝试每个选择器
        for sel in popup_selectors:
            try:
                loc = page.locator(sel)
                # 检查元素是否存在且可见
                if await loc.count() > 0 and await loc.first.is_visible():
                    # 点击关闭弹窗，超时 5 秒
                    await loc.first.click(timeout=5_000)
                    log_node("弹窗已关闭", level="INFO", selector=sel)
                    # 等待弹窗关闭动画完成
                    await page.wait_for_timeout(2_000)
                    await self.screenshot(page, "popup_closed")
                    return True
            except Exception:
                # 当前选择器失败，继续尝试下一个
                continue

        log_node("未检测到弹窗", level="INFO")
        return False

    async def click_by_text(self, page, text: str, desc: str = "") -> bool:
        """
        通过文字内容点击页面元素（多种选择器策略）。
        由于 Echotik 页面结构可能变化，使用多种选择器策略提高点击成功率。

        参数:
            page: Playwright 页面对象
            text: 要点击的元素文字内容
            desc: 操作描述，用于日志记录
        返回:
            True 表示点击成功，False 表示所有策略均失败
        """
        log_node(f"尝试点击: {text}", level="INFO", desc=desc)

        # 定义多种选择器策略，从精确匹配到模糊匹配
        selectors = [
            f":text-is('{text}')",                        # 精确文字匹配
            f"text={text}",                               # Playwright 文字选择器
            f"button:has-text('{text}')",                 # 按钮内包含文字
            f"a:has-text('{text}')",                      # 链接内包含文字
            f"span:has-text('{text}')",                   # span 内包含文字
            f"div:has-text('{text}')",                    # div 内包含文字
            f"li:has-text('{text}')",                     # 列表项内包含文字
            # 侧边栏特定选择器，缩小搜索范围
            f"[class*='sidebar'] :text-is('{text}')",     # 侧边栏内精确匹配
            f"[class*='menu'] :text-is('{text}')",        # 菜单内精确匹配
            f"nav :text-is('{text}')",                    # 导航栏内精确匹配
        ]

        # 依次尝试每个选择器
        for i, sel in enumerate(selectors):
            try:
                loc = page.locator(sel).first  # 取第一个匹配元素
                # 检查元素是否存在
                if await loc.count() == 0:
                    continue

                # 检查元素是否可见
                if not await loc.is_visible():
                    continue

                # 尝试滚动到元素可视区域（元素可能在页面下方）
                try:
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass  # 滚动失败不影响点击

                # 执行点击操作，超时 8 秒
                await loc.click(timeout=8_000)
                log_node(f"✅ 点击成功: {text}", level="INFO",
                         selector=sel[:60], attempt=f"{i+1}/{len(selectors)}")
                # 等待页面响应
                await page.wait_for_timeout(1_500)
                return True

            except Exception as e:
                # 前 3 个选择器失败时记录调试日志
                if i < 3:
                    log_node(f"尝试 {i+1} 失败", level="DEBUG",
                             selector=sel[:50], error=str(e)[:60])
                continue

        # 所有选择器都失败
        log_node(f"❌ 点击失败: {text}", level="WARN", desc=desc)
        return False

    async def navigate_to_new_products(self, page):
        """
        导航到新品榜页面。
        操作步骤：
        1. 关闭可能的弹窗
        2. 点击侧边栏一级菜单 "Products"（或中文"选品"）
        3. 点击二级菜单 "New Products"（或中文"新品榜"）
        4. 等待页面数据加载完成

        参数:
            page: Playwright 页面对象
        异常:
            RuntimeError: 无法打开菜单或找到菜单项时抛出
        """
        log_node("=" * 60, level="INFO")
        log_node("开始导航到新品榜", level="INFO")
        log_node("=" * 60, level="INFO")

        # 截图记录初始状态
        await self.screenshot(page, "01_initial_state")

        # 关闭可能的弹窗（登录后常见引导弹窗）
        await self.dismiss_popup(page)

        # ========== 步骤1：点击"Products"一级菜单 ==========
        # 纯侧边栏点击，避免 URL 跳转风险
        log_node("步骤1: 点击一级菜单 Products", level="INFO")
        await self.screenshot(page, "02_before_click_sourcing")

        # 策略1：尝试通过文字点击（中英文都试）
        # 一级菜单点击是否成功的标志
        success = False
        for text in ["Products", "选品"]:
            # 尝试点击包含指定文字的元素
            if await self.click_by_text(page, text, desc="一级菜单 Products"):
                success = True
                break

        # 策略2：文字点击失败，用 Arco Design UI 框架的展开箭头选择器
        if not success:
            log_node("文字点击失败，尝试 Arco 展开箭头", level="WARN")
            try:
                # Products 是侧边栏第3个菜单项，定位其展开箭头图标
                arrow = page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                # 点击展开箭头
                await arrow.click(timeout=5_000)
                log_node("展开箭头已点击", level="INFO")
                # 等待子菜单展开动画
                await page.wait_for_timeout(1_500)
                success = True
            except Exception as e:
                # 两种策略都失败，抛出异常
                log_node("展开箭头点击失败", level="ERROR", error=str(e)[:80])
                await self.screenshot(page, "03_products_menu_fail")
                raise RuntimeError("无法打开 Products 菜单")

        # 两种策略都失败则抛出异常（理论上不会到达这里，因为策略2已经 raise）
        if not success:
            raise RuntimeError("所有策略均失败，无法打开 Products 菜单")

        # 截图记录菜单展开状态
        await self.screenshot(page, "03_sourcing_menu_opened")

        # ========== 步骤2：点击"新品榜"或"New Products"二级菜单 ==========
        log_node("步骤2: 点击二级菜单（新品榜/New Products）", level="INFO")
        # 等待子菜单完全展开
        await page.wait_for_timeout(1_000)
        await self.screenshot(page, "04_before_click_new_products")

        # 尝试多种可能的菜单文字（中英文及变体）
        success = False
        for text in ["新品榜", "New Products", "New Arrivals", "Trending New"]:
            # 尝试点击包含指定文字的元素
            if await self.click_by_text(page, text, desc="新品榜"):
                success = True
                break

        # 所有文字都未匹配到，抛出异常
        if not success:
            log_node("新品榜点击失败", level="ERROR")
            await self.screenshot(page, "05_new_products_fail")
            raise RuntimeError("无法找到新品榜菜单项")

        # ========== 等待页面数据加载 ==========
        log_node("等待页面数据加载...", level="INFO")
        # 先等待 5 秒让页面基本加载
        await page.wait_for_timeout(5_000)
        await self.screenshot(page, "05_new_products_loaded")

        # 等待表格数据出现（尝试多种表格选择器）
        for sel in ["table tbody tr", "[class*='rank-item']", "[class*='list-item']"]:
            try:
                # 等待第一个匹配元素变为可见，超时 10 秒
                await page.locator(sel).first.wait_for(state="visible", timeout=10_000)
                log_node("数据已加载", level="INFO", signal=sel)
                break
            except Exception:
                # 当前选择器未匹配到，尝试下一个
                continue

        await self.screenshot(page, "06_data_ready")
        log_node("新品榜页面加载完成", level="INFO")

    async def export_data(self, page):
        """
        导出 200 条新品榜数据。
        操作步骤：
        1. 悬停导出下拉箭头，展开下拉菜单
        2. 选择 "200 Records" 导出条数
        3. 点击 "Export" 按钮触发下载
        4. 等待下载事件并保存文件

        参数:
            page: Playwright 页面对象
        返回:
            保存的文件路径 (Path 对象)
        异常:
            RuntimeError: 下载超时时抛出
        """
        log_node("=" * 60, level="INFO")
        log_node("开始导出流程", level="INFO")
        log_node("=" * 60, level="INFO")

        # 注册下载事件监听器，收集所有下载对象
        downloads = []
        page.on("download", lambda d: downloads.append(d))

        # ========== 步骤1：悬停/点击导出下拉箭头 ==========
        log_node("步骤1: 悬停导出下拉箭头", level="INFO")
        await self.screenshot(page, "07_before_export_dropdown")

        try:
            # 尝试定位第3个按钮（参考 downloader.py 的经验，导出下拉箭头通常是第3个按钮）
            dropdown_btn = page.get_by_role("button").nth(2)
            # 悬停触发下拉菜单展开
            await dropdown_btn.hover(timeout=8_000)
            log_node("下拉箭头已悬停", level="INFO")
            # 等待下拉菜单动画完成
            await page.wait_for_timeout(1_500)
        except Exception as e:
            # 悬停失败时尝试直接点击
            log_node("悬停失败，尝试点击", level="WARN", error=str(e)[:60])
            try:
                await dropdown_btn.click(timeout=5_000)
            except Exception:
                pass  # 点击也失败则继续后续步骤

        await self.screenshot(page, "08_dropdown_opened")

        # ========== 步骤2：选择 200 条导出数量 ==========
        log_node("步骤2: 选择 200 Records", level="INFO")
        await page.wait_for_timeout(800)

        # 尝试多种文字匹配（中英文兼容）
        # 条数选择是否成功的标志
        success = False
        for text in ["200 Records", "200条", "200"]:
            try:
                # 通过文字定位并点击条数选项
                await page.get_by_text(text).click(timeout=5_000)
                log_node(f"已选择: {text}", level="INFO")
                success = True
                # 等待选择生效
                await page.wait_for_timeout(1_500)
                break
            except Exception:
                # 当前文字格式未匹配到，继续尝试下一个
                continue

        # 条数选择失败时记录警告（不中断流程）
        if not success:
            log_node("选择条数失败", level="WARN")

        # 截图记录条数选择后的状态
        await self.screenshot(page, "09_count_selected")

        # 等待 3 秒，看选择条数后是否直接触发了 popup 下载
        await page.wait_for_timeout(3_000)

        # ========== 步骤3：点击 Export 按钮（如果选条数没有直接触发下载） ==========
        log_node("步骤3: 点击 Export 按钮", level="INFO")
        try:
            # 通过 ARIA role 定位 Export 按钮
            await page.get_by_role("button", name="Export").click(timeout=8_000)
            log_node("Export 按钮已点击", level="INFO")
            await page.wait_for_timeout(2_000)
        except Exception as e:
            # 可能选条数时已经触发了下载，Export 按钮不存在或已消失
            log_node("Export 按钮点击失败（可能已触发）", level="WARN",
                     error=str(e)[:60])

        # 截图记录 Export 按钮点击后的状态
        await self.screenshot(page, "10_export_clicked")

        # ========== 等待下载事件 ==========
        # 最多等待 20 秒（10 次循环 × 2 秒间隔）
        log_node("等待下载事件...", level="INFO")
        for i in range(10):
            # 每轮等待 2 秒
            await page.wait_for_timeout(2_000)
            # 检查是否已收到下载事件
            if downloads:
                break
            # 每 4 秒截图一次记录等待状态（i=0,2,4,6,8 时截图）
            if i % 2 == 0:
                await self.screenshot(page, f"11_waiting_download_{i*2}s")

        # 下载超时处理：所有轮次都未收到下载事件
        if not downloads:
            log_node("下载事件未触发", level="ERROR")
            await self.screenshot(page, "12_download_timeout")
            raise RuntimeError("下载超时，未检测到下载事件")

        # ========== 保存下载文件 ==========
        download_obj = downloads[0]  # 取第一个下载对象
        # 使用浏览器建议的文件名，若无则使用默认名
        filename = download_obj.suggested_filename or "new_products.xlsx"
        # 构造完整保存路径
        save_path = TEST_DIR / filename

        log_node("保存文件...", level="INFO", filename=filename)
        # 将下载内容保存到指定路径
        await download_obj.save_as(str(save_path))

        # 计算并记录文件大小
        size_kb = save_path.stat().st_size / 1024
        log_node("✅ 文件已保存", level="INFO",
                 path=str(save_path), size=f"{size_kb:.1f}KB")

        # 截图记录下载完成状态
        await self.screenshot(page, "13_download_complete")
        # 返回保存的文件路径
        return save_path

    async def run(self):
        """
        主流程入口。
        完整执行：启动 Xvfb → 登录 → 导航到新品榜 → 导出数据 → 清理资源。
        """
        try:
            # 启动 Xvfb 虚拟显示（服务器环境必需）
            self.start_xvfb()

            # 创建输出目录（如果不存在）
            TEST_DIR.mkdir(exist_ok=True)
            LOG_DIR.mkdir(exist_ok=True)

            # 从环境变量读取账号密码（取第一个账号）
            account = os.getenv("ECHOTIK_ACCOUNTS", "").split(",")[0].strip()
            password = os.getenv("ECHOTIK_PASSWORDS", "").split(",")[0].strip()

            # 校验账号密码是否已配置
            if not account or not password:
                raise RuntimeError("未配置账号密码，请检查 .env")

            # 日志中脱敏显示账号信息（只显示前5个字符）
            log_node("账号信息", level="INFO",
                     account=account[:5] + "***",
                     password_len=len(password))

            # ========== 启动 Playwright 浏览器 ==========
            async with async_playwright() as p:
                # 配置 HTTP 代理（如果环境变量中设置了 PROXY_PORT）
                proxy = None
                proxy_port = os.getenv("PROXY_PORT")
                if proxy_port:
                    # 获取宿主机 IP（WSL2 环境下需要动态获取）
                    try:
                        import socket
                        host_ip = socket.gethostbyname(socket.gethostname())
                        proxy = {"server": f"http://{host_ip}:{proxy_port}"}
                        log_node("代理配置", level="INFO", proxy=proxy["server"])
                    except Exception:
                        pass  # 获取 IP 失败则不使用代理

                # 启动 Chromium 浏览器（headless=False 配合 Xvfb 可以截图）
                browser = await p.chromium.launch(
                    headless=False,  # Xvfb 模式下用 False 可以截图
                    proxy=proxy
                )

                # 创建浏览器上下文，设置视口大小为 1920x1080
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )

                # 创建新页面
                page = await context.new_page()

                # ========== 登录 Echotik ==========
                log_node("=" * 60, level="INFO")
                log_node("开始登录流程", level="INFO")
                log_node("=" * 60, level="INFO")

                # 使用 BrowserSession 处理登录（支持 cookie 复用、验证码等）
                session = BrowserSession(context)
                session.set_single_account(account, password)
                await session.ensure_login(page)

                log_node("登录成功", level="INFO")
                await self.screenshot(page, "00_login_success")

                # ========== 导航到新品榜 ==========
                await self.navigate_to_new_products(page)

                # ========== 导出数据 ==========
                save_path = await self.export_data(page)

                # ========== 任务完成 ==========
                log_node("=" * 60, level="INFO")
                log_node("🎉 任务完成", level="INFO", file=str(save_path))
                log_node("=" * 60, level="INFO")

                # 关闭浏览器
                await browser.close()

        finally:
            # 无论成功失败，都要停止 Xvfb 进程
            self.stop_xvfb()


# ==================== 脚本入口 ====================
if __name__ == "__main__":
    # 创建导出器实例并运行主流程
    exporter = VisualExporter()
    asyncio.run(exporter.run())
