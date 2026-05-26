#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
visual_export_batch.py
批量采集指定品类的多个榜单

用法：
    python visual_export_batch.py --category "Pet Supplies" --tasks "top_sold:w,top_sold:m,new_products:d,shops:d,shops:w,shops:m"

修复记录 (2026-03-11):
    - 多账号轮换支持
    - 任务组合验证（拒绝无效的 ranking:win 组合）
    - 事件监听器清理
    - 订阅到期检测
    - 改用 headless=True（移除 Xvfb 依赖）

修复记录 (2026-03-12):
    - 增加 popup 监听和 popup download 监听
    - 增加 API fallback（用 popup URL 直接请求下载）
    - 修复 _notify 私有导入问题
    - 增加页面稳定等待（解决 hover 超时问题）
"""

# ==================== 标准库导入 ====================
import asyncio          # 异步IO框架，用于协程调度
import argparse         # 命令行参数解析
import os               # 操作系统接口，用于环境变量
import sys              # 系统相关，用于修改模块搜索路径
from datetime import datetime  # 日期时间，用于截图文件名时间戳
from pathlib import Path       # 路径操作，跨平台文件路径处理

# ==================== 第三方库导入 ====================
from dotenv import load_dotenv            # 从 .env 文件加载环境变量
from playwright.async_api import async_playwright  # Playwright 异步API，浏览器自动化

# 添加项目根目录到 sys.path，确保能导入项目内部模块
sys.path.insert(0, str(Path(__file__).parent))

# ==================== 项目内部模块导入 ====================
from browser.session import BrowserSession              # 浏览器会话管理，处理登录逻辑
from utils.logger import log_node                       # 统一日志输出工具
from utils.notifier import notify_subscription_expired  # 订阅到期通知（如发送邮件/消息）
from utils.quota import record_export, check_quota_warning  # 导出配额记录和预警


class SubscriptionExpiredError(RuntimeError):
    """
    账号订阅到期异常。
    当检测到 Echotik 账号订阅已过期时抛出此异常，
    触发多账号轮换机制切换到下一个可用账号。
    """
    pass

# 加载 .env 文件中的环境变量（如账号密码、代理端口等）
load_dotenv()

# 测试输出目录：存放导出的 Excel 文件
TEST_DIR = Path(__file__).parent / "test"
# 日志截图目录：存放每步操作的截图
LOG_DIR = Path(__file__).parent / "logs"

# ==================== 榜单配置 ====================
# 定义各榜单的菜单路径、品类筛选支持和时间维度
# 键为榜单类型标识符，值为配置字典
RANKING_CONFIG = {
    "top_sold": {
        "name": "热销榜",                    # 榜单中文名称
        "menu_parent": "Products",           # 侧边栏一级菜单名称
        "menu_child": "Top Sold",            # 侧边栏二级菜单名称
        "has_category_filter": True,         # 是否支持品类筛选
        "time_tabs": {"d": "", "w": "Weekly", "m": "Monthly"},  # 时间维度：d=日榜(默认), w=周榜, m=月榜
    },
    "new_products": {
        "name": "新品榜",
        "menu_parent": "Products",
        "menu_child": "New Products",
        "has_category_filter": True,
        "time_tabs": {"d": ""},              # 新品榜只有日榜，空字符串表示默认选中无需点击
    },
    "shops": {
        "name": "小店榜",
        "menu_parent": "Shop",               # 小店榜在 Shop 菜单下
        "menu_child": "Best Cross-border Seller",
        "has_category_filter": True,         # 小店榜也支持品类筛选
        "time_tabs": {"d": "", "w": "Weekly", "m": "Monthly"},
    },
}


class BatchExporter:
    """
    批量采集导出器。
    支持多榜单（热销榜/新品榜/小店榜）、多时间维度（日/周/月）、多品类的批量导出。
    具备多账号轮换、订阅到期检测、popup 下载监听、API fallback 等容错机制。
    """

    def __init__(self):
        """初始化批量导出器"""
        self.screenshot_count = 0   # 截图序号计数器
        self.total_downloaded = 0   # 已成功下载的文件总数

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
            # 构造截图文件路径：batch_序号_标签_时间戳.png（序号3位数，适应批量任务）
            path = LOG_DIR / f"batch_{self.screenshot_count:03d}_{label}_{ts}.png"
            # 执行截图（只截取可视区域）
            await page.screenshot(path=str(path), full_page=False)
            log_node(f"截图 #{self.screenshot_count}", level="INFO",
                     label=label, path=str(path))
            return str(path)
        except Exception as e:
            # 截图失败不影响主流程，仅记录警告
            log_node("截图失败", level="WARN", error=str(e)[:60])
            return None

    async def check_subscription_expired(self, page, account: str = ""):
        """
        检测账号订阅是否到期。
        通过检查页面文本中是否包含免费版提示信息来判断订阅状态。
        如果到期，发送通知并抛出 SubscriptionExpiredError 触发账号切换。

        参数:
            page: Playwright 页面对象
            account: 当前账号（用于日志和通知）
        异常:
            SubscriptionExpiredError: 检测到订阅到期时抛出
        """
        try:
            # 获取页面 body 文本内容，超时 2 秒
            body_text = await page.inner_text("body", timeout=2_000)
        except Exception:
            return  # 获取失败则跳过检测
        # 检查是否包含免费版或升级提示关键词
        if "Current free version" in body_text or "Upgrade for more privileges" in body_text:
            await self.screenshot(page, "subscription_expired")
            log_node("账号订阅到期", level="ERROR", account=account)
            # 发送订阅到期通知（如邮件、企业微信等）
            notify_subscription_expired(account)
            raise SubscriptionExpiredError(f"账号订阅已到期: {account}")

    async def dismiss_popup(self, page):
        """
        关闭页面上可能出现的弹窗。
        Echotik 登录后或页面切换时可能弹出引导弹窗、公告弹窗等。

        参数:
            page: Playwright 页面对象
        返回:
            True 表示成功关闭了弹窗，False 表示未检测到弹窗
        """
        # 等待 1 秒让弹窗渲染
        await page.wait_for_timeout(1000)
        # 定义多种弹窗关闭按钮的选择器
        popup_selectors = [
            "button:has-text('Start Now')",          # 英文"立即开始"
            "button:has-text('知道了')",              # 中文"知道了"
            "button:has-text('确定')",                # 中文"确定"
            "[class*='modal'] button:last-child",    # 模态框最后一个按钮
        ]
        # 依次尝试每个选择器
        for sel in popup_selectors:
            try:
                loc = page.locator(sel)
                # 检查元素是否存在且可见
                if await loc.count() > 0 and await loc.first.is_visible():
                    # 点击关闭弹窗
                    await loc.first.click(timeout=3_000)
                    log_node("弹窗已关闭", level="INFO", selector=sel)
                    # 等待弹窗关闭动画
                    await page.wait_for_timeout(1_000)
                    return True
            except Exception:
                # 当前选择器失败，继续尝试下一个
                continue
        return False

    async def click_by_text(self, page, text: str, timeout: int = 8000) -> bool:
        """
        通过文字内容点击页面元素（多种选择器策略）。
        使用多种选择器策略提高点击成功率。

        参数:
            page: Playwright 页面对象
            text: 要点击的元素文字内容
            timeout: 点击超时时间（毫秒）
        返回:
            True 表示点击成功，False 表示所有策略均失败
        """
        # 定义多种选择器策略，从精确匹配到模糊匹配
        selectors = [
            f":text-is('{text}')",            # 精确文字匹配
            f"text={text}",                   # Playwright 文字选择器
            f"button:has-text('{text}')",     # 按钮内包含文字
            f"a:has-text('{text}')",          # 链接内包含文字
            f"span:has-text('{text}')",       # span 内包含文字
            f"div:has-text('{text}')",        # div 内包含文字
        ]

        # 依次尝试每个选择器
        for sel in selectors:
            try:
                # 取第一个匹配的元素
                loc = page.locator(sel).first
                # 检查元素是否存在
                if await loc.count() == 0:
                    continue
                # 检查元素是否可见
                if not await loc.is_visible():
                    continue
                # 滚动到元素可见区域
                await loc.scroll_into_view_if_needed(timeout=3_000)
                # 执行点击操作
                await loc.click(timeout=timeout)
                log_node(f"点击成功: {text}", level="INFO", selector=sel[:60])
                # 等待页面响应
                await page.wait_for_timeout(1_500)
                return True
            except Exception:
                # 当前选择器失败，继续尝试下一个
                continue
        return False

    async def navigate_to_ranking(self, page, ranking_type: str, account: str = ""):
        """
        导航到指定榜单页面。
        根据 RANKING_CONFIG 配置，依次点击一级菜单和二级菜单，
        然后等待表格数据加载完成。每步都检查订阅是否到期。

        参数:
            page: Playwright 页面对象
            ranking_type: 榜单类型标识符（top_sold/new_products/shops）
            account: 当前账号（用于订阅检测日志）
        异常:
            RuntimeError: 无法打开菜单或找到菜单项时抛出
            SubscriptionExpiredError: 检测到订阅到期时抛出
        """
        # 从配置中获取当前榜单的菜单路径信息
        config = RANKING_CONFIG[ranking_type]
        log_node("=" * 60, level="INFO")
        log_node(f"导航到 {config['name']}", level="INFO")
        log_node("=" * 60, level="INFO")

        # 关闭可能存在的弹窗
        await self.dismiss_popup(page)
        # 检查订阅状态
        await self.check_subscription_expired(page, account)

        # ===== 点击一级菜单（如 Products 或 Shop） =====
        menu_parent = config["menu_parent"]
        success = await self.click_by_text(page, menu_parent)
        if not success:
            # 文字点击失败，尝试通过 Arco Design 框架的展开箭头点击
            try:
                if menu_parent == "Products":
                    # Products 是侧边栏第3个菜单项
                    arrow = page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                else:
                    # Shop 是侧边栏第4个菜单项
                    arrow = page.locator("div:nth-child(4) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                await arrow.click(timeout=5_000)
                # 等待子菜单展开动画
                await page.wait_for_timeout(1_500)
            except Exception as e:
                raise RuntimeError(f"无法打开 {menu_parent} 菜单: {e}")

        # 再次检查订阅状态（页面可能在菜单展开后刷新）
        await self.check_subscription_expired(page, account)

        # ===== 点击二级菜单（如 Top Sold / New Products / Best Cross-border Seller） =====
        menu_child = config["menu_child"]
        # 通过文字点击二级菜单项
        success = await self.click_by_text(page, menu_child)
        if not success:
            # 二级菜单点击失败，抛出异常
            raise RuntimeError(f"无法找到 {menu_child} 菜单项")

        # 等待页面数据加载（5秒基础等待，让页面完成初始渲染）
        await page.wait_for_timeout(5_000)
        # 等待表格数据出现（尝试多种表格选择器，兼容不同页面结构）
        for sel in ["table tbody tr", "[class*='rank-item']"]:
            try:
                # 等待第一个匹配元素变为可见，超时 10 秒
                await page.locator(sel).first.wait_for(state="visible", timeout=10_000)
                break
            except Exception:
                # 当前选择器未匹配到，尝试下一个
                continue

        # 页面加载后再次检查订阅状态
        await self.check_subscription_expired(page, account)
        log_node(f"{config['name']} 页面加载完成", level="INFO")

    async def select_category(self, page, category: str, account: str = ""):
        """
        选择商品品类筛选条件。
        在榜单页面上展开品类筛选器，选择指定品类。
        如果页面没有品类筛选器（如某些小店榜），则跳过。

        参数:
            page: Playwright 页面对象
            category: 品类名称（如 "Pet Supplies"）
            account: 当前账号（用于订阅检测）
        异常:
            RuntimeError: 品类选择失败时抛出
        """
        log_node(f"选择品类: {category}", level="INFO")

        # 等待 Product Category 筛选器出现
        try:
            await page.wait_for_selector("text=Product Category", state="visible", timeout=10_000)
        except Exception:
            # 未找到筛选器，可能是小店榜等不支持品类筛选的页面
            log_node("未找到 Product Category 筛选器（可能是小店榜）", level="WARN")
            return

        # 等待筛选器完全渲染
        await page.wait_for_timeout(2_000)

        # ===== 点击 "More" 展开全部品类 =====
        more_selectors = [
            ":has-text('Product Category') >> text=More",  # 在 Product Category 容器内找 More
            "text=/More\\s*[∨▼]/",                         # 带箭头的 More 按钮
        ]
        # 依次尝试每个 More 按钮选择器
        for sel in more_selectors:
            try:
                # 取第一个匹配元素
                loc = page.locator(sel).first
                # 检查元素是否可见
                if await loc.is_visible():
                    # 点击展开品类列表
                    await loc.click(timeout=5_000)
                    log_node("More 按钮已点击", level="INFO")
                    # 等待品类列表展开
                    await page.wait_for_timeout(2_000)
                    break
            except Exception:
                # 当前选择器失败，继续尝试下一个
                continue

        # 检查订阅状态
        await self.check_subscription_expired(page, account)

        # ===== 选择目标品类 =====
        # 多种选择器策略定位品类标签
        category_selectors = [
            f"button:has-text('{category}')",                              # 按钮形式的品类标签
            f":has-text('Product Category') >> :has-text('{category}')",  # 在 Product Category 容器内查找
        ]
        # 依次尝试每个选择器
        for sel in category_selectors:
            try:
                # 取第一个匹配元素
                loc = page.locator(sel).first
                # 检查元素是否可见
                if await loc.is_visible():
                    # 滚动到元素可见区域并点击
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                    await loc.click(timeout=5_000)
                    log_node(f"品类已选择: {category}", level="INFO")
                    # 等待品类筛选生效，表格刷新数据
                    await page.wait_for_timeout(3_000)
                    return
            except Exception:
                # 当前选择器失败，继续尝试下一个
                continue

        # 所有选择器都失败
        raise RuntimeError(f"无法选择品类: {category}")

    async def select_time_window(self, page, time_tab: str, account: str = ""):
        """
        选择时间窗口（日/周/月）。
        日榜为默认选中状态，无需点击；周榜和月榜需要点击对应的 Tab。

        参数:
            page: Playwright 页面对象
            time_tab: 时间维度标签文字（如 "Weekly"、"Monthly"），空字符串表示日榜（默认）
            account: 当前账号（用于订阅检测）
        """
        # 空字符串表示日榜，默认已选中，无需操作
        if not time_tab:
            log_node("日榜为默认选中，跳过 Tab 点击", level="INFO")
            return

        log_node(f"切换到时间窗口: {time_tab}", level="INFO")
        # 通过文字点击对应的时间 Tab
        success = await self.click_by_text(page, time_tab)
        if success:
            # 等待表格根据新时间维度刷新数据
            await page.wait_for_timeout(3_000)
            # 切换后检查订阅状态
            await self.check_subscription_expired(page, account)
            log_node(f"时间窗口已切换: {time_tab}", level="INFO")
        else:
            log_node(f"时间窗口切换失败: {time_tab}", level="WARN")

    async def export_data(self, page, label: str, account: str = "", export_count: int = 50):
        """
        导出数据（支持 main download + popup download + API fallback 三重机制）。
        Echotik 的导出可能通过主页面下载、弹出新窗口下载、或直接 API 请求下载，
        本方法覆盖所有三种情况，确保导出成功率。

        参数:
            page: Playwright 页面对象
            label: 任务标签（如 "热销榜_w_Pet Supplies"），用于文件名和日志
            account: 当前账号（用于订阅检测）
            export_count: 导出条数（默认50）
        返回:
            保存的文件路径 (Path 对象)
        异常:
            RuntimeError: 所有下载方式均失败时抛出
        """
        log_node(f"开始导出: {label}", level="INFO", export_count=export_count)

        # ===== 等待页面稳定（解决品类选择后页面重新渲染导致元素失效的问题） =====
        log_node("等待页面稳定...", level="INFO")
        await page.wait_for_timeout(2_000)
        try:
            # 等待网络请求完成（500ms 内无新请求视为稳定）
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass  # 超时也继续，不阻塞主流程

        # ===== 注册 download / popup 事件监听器（全程生效） =====
        main_downloads = []     # 主页面触发的下载事件列表
        popup_list = []         # 弹出的新窗口列表
        popup_downloads = []    # 新窗口触发的下载事件列表
        download_obj = None     # 最终获取到的下载对象
        popup_page = None       # 弹出的新窗口页面对象

        def _on_main_download(d):
            """主页面下载事件回调"""
            main_downloads.append(d)

        def _on_popup(p):
            """新窗口弹出事件回调"""
            popup_list.append(p)

        # 注册事件监听器
        page.on("download", _on_main_download)
        page.on("popup", _on_popup)

        try:
            # ===== 步骤1：hover 触发导出下拉菜单 =====
            log_node("步骤1: hover 导出下拉箭头", level="INFO")
            await self.screenshot(page, f"{label}_before_dropdown")

            # 多策略定位下拉箭头按钮
            dropdown_btn = None
            dropdown_selectors = [
                ".arco-btn-group button:last-child",       # Arco Design 按钮组的最后一个按钮
                ".arco-dropdown-button button:last-child",  # Arco 下拉按钮
                "button:has-text('Export') + button",       # Export 按钮旁边的按钮（下拉箭头）
            ]
            for sel in dropdown_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        dropdown_btn = loc
                        log_node("下拉箭头定位成功", level="INFO", selector=sel[:60])
                        break
                except Exception:
                    continue

            # 所有选择器都失败时，使用 fallback：页面第3个按钮
            if dropdown_btn is None:
                dropdown_btn = page.get_by_role("button").nth(2)
                log_node("使用 fallback 选择器 nth(2)", level="WARN")

            try:
                # 悬停触发下拉菜单显示
                await dropdown_btn.hover(timeout=8_000)
                log_node("下拉箭头已悬停", level="INFO")
                # 等待下拉菜单渲染
                await page.wait_for_timeout(1_500)
            except Exception as e:
                # 悬停失败时尝试直接点击
                log_node("hover 失败，尝试 click", level="WARN", error=str(e)[:60])
                try:
                    await dropdown_btn.click(timeout=5_000)
                except Exception:
                    pass

            await self.screenshot(page, f"{label}_dropdown_opened")
            # 检查订阅状态
            await self.check_subscription_expired(page, account)

            # ===== 步骤2：选择导出条数 =====
            log_node("步骤2: 选择条数", level="INFO")
            await page.wait_for_timeout(800)

            # 根据 export_count 参数构造条数选项文字
            count_selected = False
            count_texts = [f"{export_count} Records", f"{export_count}条", str(export_count)]
            for text in count_texts:
                try:
                    await page.get_by_text(text).click(timeout=5_000)
                    log_node(f"已选择条数: {text}", level="INFO")
                    count_selected = True
                    # 等待选择生效
                    await page.wait_for_timeout(1_500)
                    break
                except Exception:
                    continue

            if not count_selected:
                log_node("条数选择失败", level="WARN")

            await self.screenshot(page, f"{label}_count_selected")
            await self.check_subscription_expired(page, account)

            # 等待 5 秒，观察选条数是否已经直接触发了 popup 窗口
            await page.wait_for_timeout(5_000)

            if popup_list:
                # ===== 情况A：选条数后直接触发了 popup 窗口 =====
                popup_page = popup_list[0]
                log_node("选条数后直接触发了 popup", level="INFO",
                         url=popup_page.url[:120] if popup_page.url else "")
                # 在 popup 窗口上也注册 download 监听，捕获 popup 内的下载事件
                def _on_popup_download(d):
                    popup_downloads.append(d)
                popup_page.on("download", _on_popup_download)
            else:
                # ===== 情况B：选条数没触发 popup，需要手动点击 Export 按钮 =====
                log_node("步骤3: 选条数未触发 popup，点击 Export 按钮", level="INFO")
                try:
                    # 使用 expect_popup 等待 Export 按钮点击后弹出的新窗口
                    async with page.expect_popup(timeout=30_000) as popup_info:
                        await page.get_by_role("button", name="Export").click(timeout=8_000)
                        log_node("Export 按钮已点击", level="INFO")
                        await page.wait_for_timeout(1_000)
                    # 获取弹出的 popup 页面
                    popup_page = await popup_info.value
                    log_node("popup 已打开", level="INFO",
                             url=popup_page.url[:120] if popup_page.url else "")
                    # 在 popup 上注册 download 监听
                    def _on_popup_download(d):
                        popup_downloads.append(d)
                    popup_page.on("download", _on_popup_download)
                except Exception as e:
                    log_node("Export 按钮点击或 popup 等待失败", level="WARN",
                             error=str(e)[:80])
                    await self.screenshot(page, f"{label}_export_fail")

            await self.screenshot(page, f"{label}_export_clicked")
            await self.check_subscription_expired(page, account)

            # ===== 等待下载事件（最多 16 秒，每 2 秒检查 main 和 popup 两个来源） =====
            for _ in range(8):
                await page.wait_for_timeout(2_000)
                # 优先检查主页面下载事件
                if main_downloads:
                    download_obj = main_downloads[0]
                    log_node("下载事件来源: main_download", level="INFO")
                    break
                # 其次检查 popup 窗口下载事件
                if popup_downloads:
                    download_obj = popup_downloads[0]
                    log_node("下载事件来源: popup_download", level="INFO")
                    break

            # ===== Fallback：用 popup URL 直接发起 API 请求下载 =====
            # 当 main 和 popup 的 download 事件都未触发时，尝试直接请求 popup 的 URL
            if not download_obj and popup_page and popup_page.url:
                log_node("download 事件未触发，改用 popup URL 直接请求", level="WARN",
                         url=popup_page.url[:120])
                try:
                    # 使用浏览器上下文的 request API（自动携带 cookie）
                    api = page.context.request
                    resp = await api.get(popup_page.url)
                    if resp.status == 200:
                        body = await resp.body()
                        # 校验响应内容是否为 xlsx 文件（xlsx 本质是 ZIP，以 PK 魔数开头）
                        if body[:4] == b"PK\x03\x04":
                            save_path = TEST_DIR / f"{label}.xlsx"
                            # 直接写入二进制内容
                            save_path.write_bytes(body)
                            size_kb = len(body) / 1024
                            log_node("文件已保存（API直接请求）", level="INFO",
                                     path=str(save_path), size=f"{size_kb:.1f}KB")
                            self.total_downloaded += 1
                            # 记录导出配额并检查预警
                            record_export(task=label, count=export_count)
                            check_quota_warning()
                            return save_path
                        else:
                            # 响应内容不是 xlsx 格式
                            log_node("API 返回非 xlsx 内容", level="ERROR",
                                     content_preview=body[:200].decode("utf-8", errors="replace"))
                    else:
                        log_node("API 请求失败", level="ERROR", status=resp.status)
                except Exception as e:
                    log_node("API 直接请求异常", level="ERROR", error=str(e)[:80])

            # ===== 关闭 popup 窗口（清理资源） =====
            if popup_page:
                try:
                    await popup_page.close()
                except Exception:
                    pass

            # 三种下载方式都失败
            if not download_obj:
                raise RuntimeError("下载超时：main/popup download 均未触发，API fallback 也失败")

            # ===== 保存通过 download 事件获取的文件 =====
            # 使用浏览器建议的文件名，如果没有则用任务标签作为文件名
            filename = download_obj.suggested_filename or f"{label}.xlsx"
            save_path = TEST_DIR / filename

            # 将下载内容保存到指定路径
            await download_obj.save_as(str(save_path))
            # 计算并记录文件大小
            size_kb = save_path.stat().st_size / 1024
            log_node("文件已保存", level="INFO",
                     path=str(save_path), size=f"{size_kb:.1f}KB")

            self.total_downloaded += 1
            # 记录导出配额并检查预警
            record_export(task=label, count=export_count)
            check_quota_warning()
            return save_path

        finally:
            # ===== 清理事件监听器（防止内存泄漏和重复触发） =====
            try:
                page.remove_listener("download", _on_main_download)
                page.remove_listener("popup", _on_popup)
            except Exception:
                pass

    async def run_task(self, page, ranking_type: str, time_window: str,
                       category: str = None, account: str = "", export_count: int = 50):
        """
        执行单个采集任务。
        完整流程：导航到榜单 → 选择品类 → 切换时间维度 → 导出数据。
        捕获异常确保单个任务失败不影响后续任务。

        参数:
            page: Playwright 页面对象
            ranking_type: 榜单类型（top_sold/new_products/shops）
            time_window: 时间维度（d/w/m）
            category: 品类名称（可选）
            account: 当前账号（用于日志）
            export_count: 导出条数
        返回:
            True 表示任务成功，False 表示任务失败
        异常:
            SubscriptionExpiredError: 订阅到期时向上抛出，触发账号切换
        """
        # 从配置中获取榜单信息
        config = RANKING_CONFIG[ranking_type]
        # 获取时间维度对应的 Tab 文字
        time_tab = config["time_tabs"].get(time_window, "")

        # 构造任务标签（用于日志和文件名）
        label = f"{config['name']}_{time_window}"
        if category:
            label += f"_{category}"

        log_node("=" * 60, level="INFO")
        log_node(f"任务开始: {label}", level="INFO")
        log_node("=" * 60, level="INFO")

        try:
            # 步骤1：导航到指定榜单页面
            await self.navigate_to_ranking(page, ranking_type, account)
            await self.screenshot(page, f"{label}_01_loaded")

            # 步骤2：选择品类（如果有品类参数且榜单支持品类筛选）
            if category and config["has_category_filter"]:
                await self.select_category(page, category, account)
                await self.screenshot(page, f"{label}_02_category")

            # 步骤3：切换时间维度（日/周/月）
            await self.select_time_window(page, time_tab, account)
            await self.screenshot(page, f"{label}_03_timewindow")

            # 步骤4：导出数据
            save_path = await self.export_data(page, label, account, export_count)
            await self.screenshot(page, f"{label}_04_exported")

            log_node(f"任务完成: {label}", level="INFO", file=str(save_path))
            return True

        except SubscriptionExpiredError:
            # 订阅到期异常向上抛出，由 run() 方法处理账号切换
            raise
        except Exception as e:
            # 其他异常记录错误但不中断后续任务
            log_node(f"任务失败: {label}", level="ERROR", error=str(e)[:120])
            await self.screenshot(page, f"{label}_99_failed")
            return False

    async def run(self, tasks: list, category: str = None, export_count: int = 50):
        """
        主流程（多账号轮换）。
        遍历所有配置的账号，依次尝试登录并执行任务列表。
        如果某个账号订阅到期，自动切换到下一个账号继续执行。

        参数:
            tasks: 任务列表，每个元素为 (ranking_type, time_window) 元组
            category: 品类名称（可选）
            export_count: 每个任务导出条数
        """
        # 确保输出目录和日志目录存在
        TEST_DIR.mkdir(exist_ok=True)
        LOG_DIR.mkdir(exist_ok=True)

        # ===== 读取账号列表（从环境变量，逗号分隔） =====
        accounts_str = os.getenv("ECHOTIK_ACCOUNTS", "")
        passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")
        # 解析账号列表，去除空白和引号
        accounts = [a.strip().strip('"').strip("'")
                    for a in accounts_str.split(",") if a.strip()]
        passwords = [p.strip().strip('"').strip("'")
                     for p in passwords_str.split(",") if p.strip()]

        # 校验是否配置了账号
        if not accounts:
            log_node("未配置账号", level="ERROR")
            return

        total_accounts = len(accounts)

        # ===== 启动 Playwright 并遍历账号 =====
        async with async_playwright() as p:
            for acct_idx in range(total_accounts):
                acct = accounts[acct_idx]
                # 获取对应密码（如果密码数量不足则使用空字符串）
                pwd = passwords[acct_idx] if acct_idx < len(passwords) else ""
                # 账号脱敏处理（用于日志显示）
                acct_masked = acct[:3] + "***@" + acct.split("@")[-1] if "@" in acct else acct[:3] + "***"

                log_node(f"切换到账号 {acct_idx + 1}/{total_accounts}",
                         level="START", account=acct_masked)

                # 启动无头浏览器（headless=True，不需要 Xvfb）
                browser = await p.chromium.launch(headless=True)
                # 创建浏览器上下文，设置视口和 User-Agent（模拟真实 Chrome 浏览器）
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    # 使用常见的 Chrome User-Agent，避免被网站识别为自动化工具
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                # 创建新页面标签
                page = await context.new_page()

                # ===== 登录流程 =====
                log_node("开始登录", level="INFO")
                # 创建浏览器会话管理器
                session = BrowserSession(context)
                # 设置当前账号和密码
                session.set_single_account(acct, pwd)

                try:
                    # 执行登录（支持 cookie 复用、验证码处理等）
                    await session.ensure_login(page)
                    log_node("登录成功", level="INFO")
                except RuntimeError as e:
                    # 登录失败，关闭浏览器并尝试下一个账号
                    log_node("登录失败，尝试下一个账号",
                             level="WARN", account=acct_masked, error=str(e)[:80])
                    await browser.close()
                    continue

                # ===== 执行任务列表 =====
                # 成功任务计数器
                success_count = 0
                try:
                    # 遍历所有待执行的采集任务
                    for ranking_type, time_window in tasks:
                        # 逐个执行采集任务
                        result = await self.run_task(
                            page, ranking_type, time_window, category, acct_masked, export_count
                        )
                        # 任务成功则计数加一
                        if result:
                            success_count += 1
                        # 任务间等待 2 秒，避免请求过于频繁
                        await page.wait_for_timeout(2_000)

                    # 所有任务执行完毕，输出汇总信息
                    log_node("=" * 60, level="INFO")
                    log_node(f"批量任务完成", level="INFO",
                             total=len(tasks), success=success_count,
                             failed=len(tasks) - success_count)
                    log_node("=" * 60, level="INFO")
                    # 关闭浏览器释放资源
                    await browser.close()
                    return  # 成功完成所有任务，退出账号轮换循环

                except SubscriptionExpiredError:
                    # 订阅到期，关闭浏览器并切换到下一个账号
                    log_node("账号订阅到期，切换下一个账号",
                             level="WARN", account=acct_masked)
                    await browser.close()
                    continue

            # 所有账号都已尝试完毕（全部失败或到期）
            log_node("所有账号均已尝试", level="ERROR")


def parse_args():
    """
    解析命令行参数。

    支持的参数:
        --category: 品类名称，默认 "Pet Supplies"
        --tasks: 任务列表（必填），格式为 "榜单类型:时间维度"，多个用逗号分隔
        --count: 每个任务导出条数，默认 50
    返回:
        解析后的参数命名空间对象
    """
    # 创建命令行参数解析器
    parser = argparse.ArgumentParser(description="批量采集 Echotik 榜单")
    # --category: 品类名称参数，默认 Pet Supplies
    parser.add_argument("--category", default="Pet Supplies", help="品类名称")
    # --tasks: 必填参数，指定要执行的任务列表
    parser.add_argument("--tasks", required=True,
                        help="任务列表，格式：top_sold:w,top_sold:m,new_products:d,shops:d")
    # --count: 每个任务导出条数，默认 50 条
    parser.add_argument("--count", type=int, default=50,
                        help="每个任务导出条数（默认50）")
    # 解析并返回命令行参数
    return parser.parse_args()


def validate_tasks(task_str: str) -> list:
    """
    验证并解析任务列表字符串，拒绝无效的榜单类型和时间维度组合。

    参数:
        task_str: 逗号分隔的任务字符串，如 "top_sold:w,new_products:d"
    返回:
        有效任务列表，每个元素为 (ranking_type, time_window) 元组。
        如果存在任何无效任务，返回空列表。
    """
    # 有效任务列表
    tasks = []
    # 错误信息列表
    errors = []

    # 按逗号分隔，逐个验证任务
    for task in task_str.split(","):
        # 按冒号分隔为 榜单类型:时间维度
        parts = task.strip().split(":")
        # 格式必须是 "类型:维度"，即恰好两部分
        if len(parts) != 2:
            errors.append(f"格式错误: {task}")
            continue

        # 解构为榜单类型和时间维度
        ranking_type, time_window = parts
        # 验证榜单类型是否在配置中
        if ranking_type not in RANKING_CONFIG:
            errors.append(f"未知榜单类型: {ranking_type}")
            continue

        # 验证时间维度是否被该榜单支持
        config = RANKING_CONFIG[ranking_type]
        if time_window not in config["time_tabs"]:
            valid_wins = list(config["time_tabs"].keys())
            errors.append(f"{ranking_type} 不支持 {time_window}，有效值: {valid_wins}")
            continue

        # 验证通过，加入任务列表
        tasks.append((ranking_type, time_window))

    # 如果有任何错误，输出所有错误并返回空列表
    if errors:
        for err in errors:
            log_node(err, level="ERROR")
        return []

    return tasks


# ==================== 脚本入口 ====================
if __name__ == "__main__":
    # 解析命令行参数
    args = parse_args()

    # 验证任务列表的合法性
    tasks = validate_tasks(args.tasks)
    if not tasks:
        log_node("未解析到有效任务", level="ERROR")
        sys.exit(1)

    # 输出任务信息
    log_node("任务列表", level="INFO", tasks=tasks, category=args.category, export_count=args.count)

    # 创建批量导出器并运行
    exporter = BatchExporter()
    asyncio.run(exporter.run(tasks, args.category, args.count))
