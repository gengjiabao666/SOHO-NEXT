#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
visual_export_category.py
视觉驱动的品类筛选导出脚本

流程：
1. 登录 Echotik
2. 导航到 Top Sold（热销榜）
3. 点击 Product Category 的 "More" 展开全部品类
4. 选择 "Pet Supplies"
5. 等待表格刷新
6. 点击导出下拉 → 200 Records → Export
7. 保存到 test/ 目录
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


class CategoryExporter:
    """
    品类筛选导出器。
    通过模拟真实用户操作，导航到热销榜页面，选择指定品类（如 Pet Supplies），
    然后导出 200 条数据到本地 Excel 文件。
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
            # 构造截图文件路径：category_序号_标签_时间戳.png
            path = LOG_DIR / f"category_{self.screenshot_count:02d}_{label}_{ts}.png"
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
        关闭页面上可能出现的弹窗。
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
            "[class*='modal'] button:last-child",    # 模态框最后一个按钮
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

    async def click_by_text(self, page, text: str, desc: str = "", timeout: int = 8000) -> bool:
        """
        通过文字内容点击页面元素（多种选择器策略）。
        由于 Echotik 页面结构可能变化，使用多种选择器策略提高点击成功率。

        参数:
            page: Playwright 页面对象
            text: 要点击的元素文字内容
            desc: 操作描述，用于日志记录
            timeout: 点击超时时间（毫秒）
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
            f"[class*='sidebar'] :text-is('{text}')",    # 侧边栏内精确匹配
            f"[class*='menu'] :text-is('{text}')",       # 菜单内精确匹配
            f"nav :text-is('{text}')",                   # 导航栏内精确匹配
        ]

        # 依次尝试每个选择器
        for i, sel in enumerate(selectors):
            try:
                # 取第一个匹配的元素
                loc = page.locator(sel).first
                # 检查元素是否存在
                if await loc.count() == 0:
                    continue

                # 检查元素是否可见
                if not await loc.is_visible():
                    continue

                # 尝试滚动到元素可见区域（如果元素在视口外）
                try:
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass  # 滚动失败不影响点击

                # 执行点击操作
                await loc.click(timeout=timeout)
                log_node(f"✅ 点击成功: {text}", level="INFO",
                         selector=sel[:60], attempt=f"{i+1}/{len(selectors)}")
                # 等待页面响应点击事件
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

    async def navigate_to_top_sold(self, page):
        """
        导航到热销榜（Top Sold）页面。
        通过点击侧边栏的 Products 一级菜单，再点击 Top Sold 二级菜单，
        最后等待表格数据加载完成。

        参数:
            page: Playwright 页面对象
        异常:
            RuntimeError: 无法打开菜单或找到菜单项时抛出
        """
        log_node("=" * 60, level="INFO")
        log_node("开始导航到热销榜", level="INFO")
        log_node("=" * 60, level="INFO")

        # 截图记录初始状态
        await self.screenshot(page, "01_initial_state")
        # 关闭可能存在的弹窗
        await self.dismiss_popup(page)

        # ===== 步骤1：点击 Products 一级菜单 =====
        log_node("步骤1: 点击一级菜单 Products", level="INFO")
        await self.screenshot(page, "02_before_click_products")

        # 策略1：尝试通过文字点击（中英文都试）
        # 一级菜单点击是否成功的标志
        success = False
        for text in ["Products", "选品"]:
            # 尝试点击包含指定文字的元素
            if await self.click_by_text(page, text, desc="一级菜单 Products"):
                success = True
                break

        # 策略2：文字点击失败，尝试通过 Arco Design 框架的展开箭头点击
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

        # 截图记录菜单展开状态
        await self.screenshot(page, "03_products_menu_opened")

        # ===== 步骤2：点击 Top Sold 二级菜单 =====
        log_node("步骤2: 点击二级菜单 Top Sold", level="INFO")
        # 等待子菜单渲染完成
        await page.wait_for_timeout(1_000)
        await self.screenshot(page, "04_before_click_top_sold")

        # 尝试中英文文字点击二级菜单
        success = False
        for text in ["Top Sold", "热销榜"]:
            # 尝试点击包含指定文字的元素
            if await self.click_by_text(page, text, desc="Top Sold"):
                success = True
                break

        if not success:
            log_node("Top Sold 点击失败", level="ERROR")
            await self.screenshot(page, "05_top_sold_fail")
            raise RuntimeError("无法找到 Top Sold 菜单项")

        # ===== 等待页面数据加载 =====
        log_node("等待页面数据加载...", level="INFO")
        # 先等待 5 秒让页面基本加载
        await page.wait_for_timeout(5_000)
        await self.screenshot(page, "05_top_sold_loaded")

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
        log_node("热销榜页面加载完成", level="INFO")

    async def select_category(self, page, category: str = "Pet Supplies"):
        """
        选择商品品类筛选条件。
        在热销榜页面上，展开品类筛选器，选择指定品类（如 Pet Supplies），
        然后等待表格根据品类条件刷新数据。

        参数:
            page: Playwright 页面对象
            category: 品类名称，默认 "Pet Supplies"
        异常:
            RuntimeError: 筛选器未加载或品类选择失败时抛出
        """
        log_node("=" * 60, level="INFO")
        log_node(f"开始选择品类: {category}", level="INFO")
        log_node("=" * 60, level="INFO")

        await self.screenshot(page, "07_before_category_filter")

        # ===== 步骤1：等待 Product Category 筛选器出现 =====
        log_node("步骤1: 等待 Product Category 筛选器加载", level="INFO")
        try:
            # 等待包含 "Product Category" 文字的元素可见，超时 10 秒
            await page.wait_for_selector("text=Product Category", state="visible", timeout=10_000)
            log_node("Product Category 筛选器已出现", level="INFO")
        except Exception as e:
            log_node("Product Category 筛选器未出现", level="ERROR", error=str(e)[:80])
            await self.screenshot(page, "07b_no_category_filter")
            raise RuntimeError("页面未加载 Product Category 筛选器")

        # 等待筛选器完全渲染
        await page.wait_for_timeout(2_000)
        await self.screenshot(page, "07c_category_filter_ready")

        # ===== 步骤2：点击 "More" 展开全部品类列表 =====
        log_node("步骤2: 点击 Product Category 的 More 展开品类列表", level="INFO")

        # 多种策略定位 "More" 按钮
        more_selectors = [
            # 方法1：在包含 Product Category 的容器内找 More 文字
            ":has-text('Product Category') >> text=More",
            ":has-text('Product Category') >> button:has-text('More')",
            ":has-text('Product Category') >> span:has-text('More')",
            # 方法2：直接找带箭头的 More（品类筛选器特有的展开按钮）
            "text=/More\\s*[∨▼]/",
            "button:has-text('More ∨')",
            "span:has-text('More ∨')",
        ]

        # More 按钮点击是否成功的标志
        success = False
        # 依次尝试每个 More 按钮选择器
        for sel in more_selectors:
            try:
                # 定位所有匹配元素
                loc = page.locator(sel)
                # 获取匹配元素数量
                count = await loc.count()
                # 无匹配元素则跳过
                if count == 0:
                    continue

                # 遍历所有匹配元素，找到第一个可见的并点击
                for i in range(count):
                    elem = loc.nth(i)
                    # 检查元素是否可见
                    if await elem.is_visible():
                        # 点击展开品类列表
                        await elem.click(timeout=5_000)
                        log_node("More 按钮已点击", level="INFO", selector=sel[:60])
                        # 等待品类列表展开动画
                        await page.wait_for_timeout(2_000)
                        success = True
                        break
                # 已成功点击则跳出外层循环
                if success:
                    break
            except Exception as e:
                # 当前选择器失败，记录调试日志并继续尝试下一个
                log_node(f"More 按钮尝试失败", level="DEBUG",
                         selector=sel[:50], error=str(e)[:60])
                continue

        if not success:
            # More 按钮点击失败，可能品类已经全部显示，继续尝试直接选择
            log_node("More 按钮点击失败，尝试直接选择品类", level="WARN")

        # 截图记录 More 按钮点击后的状态
        await self.screenshot(page, "08_more_clicked")

        # ===== 步骤3：选择目标品类 =====
        log_node(f"步骤3: 选择品类 {category}", level="INFO")
        await page.wait_for_timeout(1_000)

        # 多种选择器策略定位品类标签
        category_selectors = [
            f"button:has-text('{category}')",                              # 按钮形式
            f"span:has-text('{category}')",                                # span 文字
            f"div:has-text('{category}')",                                 # div 文字
            f":text-is('{category}')",                                     # 精确文字匹配
            f"text={category}",                                            # Playwright 文字选择器
            # 品类标签通常是 pill/tag 样式的 UI 组件
            f"[class*='tag']:has-text('{category}')",                     # tag 样式
            f"[class*='pill']:has-text('{category}')",                    # pill 样式
            f"[class*='category']:has-text('{category}')",                # category 样式
            # 在 Product Category 容器内查找
            f":has-text('Product Category') >> :has-text('{category}')",  # 容器内查找
        ]

        # 品类选择是否成功的标志
        success = False
        # 依次尝试每个选择器策略
        for sel in category_selectors:
            try:
                # 定位所有匹配元素
                loc = page.locator(sel)
                # 获取匹配元素数量
                count = await loc.count()
                # 无匹配元素则跳过
                if count == 0:
                    continue

                # 遍历匹配元素，找到可见的品类标签并点击
                for i in range(count):
                    # 取第 i 个匹配元素
                    elem = loc.nth(i)
                    # 检查元素是否可见
                    if await elem.is_visible():
                        # 尝试滚动到元素可见区域
                        try:
                            await elem.scroll_into_view_if_needed(timeout=3_000)
                        except Exception:
                            pass  # 滚动失败不影响点击

                        # 点击选择品类
                        await elem.click(timeout=5_000)
                        log_node(f"✅ 品类已选择: {category}", level="INFO",
                                 selector=sel[:60])
                        # 等待品类选择生效
                        await page.wait_for_timeout(1_500)
                        success = True
                        break
                # 已成功选择则跳出外层循环
                if success:
                    break
            except Exception as e:
                # 当前选择器失败，记录调试日志并继续尝试下一个
                log_node(f"品类选择尝试失败", level="DEBUG",
                         selector=sel[:50], error=str(e)[:60])
                continue

        # 所有选择器策略都失败
        if not success:
            log_node(f"❌ 品类选择失败: {category}", level="ERROR")
            await self.screenshot(page, "09_category_select_fail")
            raise RuntimeError(f"无法选择品类: {category}")

        # 截图记录品类选择成功状态
        await self.screenshot(page, "09_category_selected")

        # ===== 步骤4：等待表格根据品类条件刷新 =====
        log_node("步骤4: 等待表格刷新...", level="INFO")
        # 等待 5 秒让数据请求完成
        await page.wait_for_timeout(5_000)

        # 等待网络请求完成（networkidle 表示 500ms 内无新网络请求）
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            log_node("networkidle 超时，继续", level="WARN")

        await self.screenshot(page, "10_table_refreshed")
        log_node(f"品类 {category} 筛选完成", level="INFO")

    async def export_data(self, page):
        """
        导出 200 条数据。
        通过悬停导出下拉箭头、选择导出条数、点击 Export 按钮，
        触发文件下载并保存到本地 test/ 目录。

        参数:
            page: Playwright 页面对象
        返回:
            保存的文件路径 (Path 对象)
        异常:
            RuntimeError: 下载超时未触发时抛出
        """
        log_node("=" * 60, level="INFO")
        log_node("开始导出流程", level="INFO")
        log_node("=" * 60, level="INFO")

        # 注册下载事件监听器，收集所有下载事件
        downloads = []
        page.on("download", lambda d: downloads.append(d))

        # ===== 步骤1：悬停/点击导出下拉箭头 =====
        log_node("步骤1: 悬停导出下拉箭头", level="INFO")
        await self.screenshot(page, "11_before_export_dropdown")

        try:
            # 尝试定位第3个按钮（导出下拉箭头，参考 downloader.py 的经验）
            dropdown_btn = page.get_by_role("button").nth(2)
            # 悬停触发下拉菜单显示
            await dropdown_btn.hover(timeout=8_000)
            log_node("下拉箭头已悬停", level="INFO")
            # 等待下拉菜单渲染
            await page.wait_for_timeout(1_500)
        except Exception as e:
            # 悬停失败时尝试直接点击
            log_node("悬停失败，尝试点击", level="WARN", error=str(e)[:60])
            try:
                await dropdown_btn.click(timeout=5_000)
            except Exception:
                pass

        await self.screenshot(page, "12_dropdown_opened")

        # ===== 步骤2：选择导出 200 条记录 =====
        log_node("步骤2: 选择 200 Records", level="INFO")
        await page.wait_for_timeout(800)

        # 尝试多种文字格式匹配条数选项（中英文兼容）
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

        # 条数选择失败时记录警告（不中断流程，后续 Export 按钮可能仍可用）
        if not success:
            log_node("选择条数失败", level="WARN")

        # 截图记录条数选择后的状态
        await self.screenshot(page, "13_count_selected")

        # 等待看选择条数后是否直接触发了下载（某些情况下选条数即触发）
        await page.wait_for_timeout(3_000)

        # ===== 步骤3：点击 Export 按钮（如果选条数未直接触发下载） =====
        log_node("步骤3: 点击 Export 按钮", level="INFO")
        try:
            # 通过 role 和 name 精确定位 Export 按钮
            await page.get_by_role("button", name="Export").click(timeout=8_000)
            log_node("Export 按钮已点击", level="INFO")
            await page.wait_for_timeout(2_000)
        except Exception as e:
            # 点击失败可能是因为已经触发了下载
            log_node("Export 按钮点击失败（可能已触发）", level="WARN",
                     error=str(e)[:60])

        await self.screenshot(page, "14_export_clicked")

        # ===== 等待下载事件（最多 20 秒，每 2 秒检查一次） =====
        log_node("等待下载事件...", level="INFO")
        for i in range(10):
            # 每轮等待 2 秒
            await page.wait_for_timeout(2_000)
            # 检查是否已收到下载事件
            if downloads:
                break
            # 每 4 秒截图一次记录等待状态（i=0,2,4,6,8 时截图）
            if i % 2 == 0:
                await self.screenshot(page, f"15_waiting_download_{i*2}s")

        # 检查下载是否成功
        if not downloads:
            log_node("下载事件未触发", level="ERROR")
            await self.screenshot(page, "16_download_timeout")
            raise RuntimeError("下载超时，未检测到下载事件")

        # ===== 保存下载的文件 =====
        download_obj = downloads[0]  # 取第一个下载对象
        # 使用浏览器建议的文件名，如果没有则使用默认名
        filename = download_obj.suggested_filename or "pet_supplies_top_sold.xlsx"
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
        await self.screenshot(page, "17_download_complete")
        # 返回保存的文件路径
        return save_path

    async def run(self):
        """
        主流程入口。
        完整执行：启动 Xvfb → 登录 Echotik → 导航到热销榜 → 选择品类 → 导出数据。
        使用 try/finally 确保 Xvfb 在任何情况下都能被正确关闭。
        """
        try:
            # 启动 Xvfb 虚拟显示
            self.start_xvfb()

            # 确保输出目录和日志目录存在
            TEST_DIR.mkdir(exist_ok=True)
            LOG_DIR.mkdir(exist_ok=True)

            # 从环境变量读取账号密码（取第一个账号）
            account = os.getenv("ECHOTIK_ACCOUNTS", "").split(",")[0].strip()
            password = os.getenv("ECHOTIK_PASSWORDS", "").split(",")[0].strip()

            # 校验账号密码是否配置
            if not account or not password:
                raise RuntimeError("未配置账号密码，请检查 .env")

            # 日志输出账号信息（脱敏处理，只显示前5个字符）
            log_node("账号信息", level="INFO",
                     account=account[:5] + "***",
                     password_len=len(password))

            # 启动 Playwright 浏览器自动化
            async with async_playwright() as p:
                # 配置代理（如果环境变量中设置了 PROXY_PORT）
                proxy = None
                proxy_port = os.getenv("PROXY_PORT")
                if proxy_port:
                    # 获取宿主机 IP（WSL2 环境下需要通过主机名解析）
                    try:
                        import socket
                        host_ip = socket.gethostbyname(socket.gethostname())
                        proxy = {"server": f"http://{host_ip}:{proxy_port}"}
                        log_node("代理配置", level="INFO", proxy=proxy["server"])
                    except Exception:
                        pass  # 代理配置失败不影响主流程

                # 启动 Chromium 浏览器（headless=False 配合 Xvfb 可以截图）
                browser = await p.chromium.launch(
                    headless=False,
                    proxy=proxy
                )

                # 创建浏览器上下文，设置视口大小为 1920x1080
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )

                # 创建新页面
                page = await context.new_page()

                # ===== 登录流程 =====
                log_node("=" * 60, level="INFO")
                log_node("开始登录流程", level="INFO")
                log_node("=" * 60, level="INFO")

                # 使用 BrowserSession 处理登录（支持 cookie 复用等）
                session = BrowserSession(context)
                session.set_single_account(account, password)
                await session.ensure_login(page)

                log_node("登录成功", level="INFO")
                await self.screenshot(page, "00_login_success")

                # ===== 导航到热销榜 =====
                await self.navigate_to_top_sold(page)

                # ===== 选择品类（默认 Pet Supplies） =====
                await self.select_category(page, "Pet Supplies")

                # ===== 导出数据 =====
                save_path = await self.export_data(page)

                # ===== 任务完成 =====
                log_node("=" * 60, level="INFO")
                log_node("🎉 任务完成", level="INFO", file=str(save_path))
                log_node("=" * 60, level="INFO")

                # 关闭浏览器
                await browser.close()

        finally:
            # 无论成功或失败，都要停止 Xvfb
            self.stop_xvfb()


# ==================== 脚本入口 ====================
if __name__ == "__main__":
    # 创建品类导出器实例并运行
    exporter = CategoryExporter()
    asyncio.run(exporter.run())
