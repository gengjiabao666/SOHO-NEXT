# 导入异步IO库，用于支持异步操作
import asyncio
# 导入正则表达式库（Playwright录制自动引入，本脚本未直接使用）
import re
# 从Playwright异步API中导入核心类：Playwright实例、异步上下文管理器、断言工具
from playwright.async_api import Playwright, async_playwright, expect


async def run(playwright: Playwright) -> None:
    """
    Echotik 每日数据采集录制脚本（Daily）

    功能流程：
    1. 登录 Echotik 网站
    2. 进入面板页面，开始操作
    3. 导航到「商品排行榜 - 热销商品（Top Sold）」页面，选择"Daily"时间维度
    4. 设置导出200条记录，触发导出并等待下载弹窗
    5. 导航到「卖家排行榜 - 最佳跨境卖家（Best Cross-border Seller）」页面，选择"Daily"时间维度
    6. 设置导出200条记录，触发导出并等待下载弹窗
    7. 关闭所有页面和浏览器
    """

    # ==================== 浏览器初始化 ====================
    # 启动 Chromium 浏览器，headless=False 表示显示浏览器窗口（便于调试观察）
    browser = await playwright.chromium.launch(headless=False)
    # 创建新的浏览器上下文（隔离的会话环境，独立的cookie和缓存）
    context = await browser.new_context()
    # 在上下文中打开一个新的标签页
    page = await context.new_page()

    # ==================== 登录流程 ====================
    # 导航到 Echotik 登录页面
    await page.goto("https://www.echotik.live/login")
    # 点击邮箱输入框，使其获得焦点
    await page.get_by_role("textbox", name="Email").click()
    # 填入登录邮箱地址
    await page.get_by_role("textbox", name="Email").fill("yr9m6eyds7@xghff.com")
    # 点击密码输入框，使其获得焦点
    await page.get_by_role("textbox", name="Password").click()
    # 填入登录密码
    await page.get_by_role("textbox", name="Password").fill("aa998877")
    # 点击"Login"按钮提交登录表单（exact=True 精确匹配按钮文本，避免误点）
    await page.get_by_role("button", name="Login", exact=True).click()

    # ==================== 进入面板页面 ====================
    # 登录成功后，导航到面板（dashboard）页面
    await page.goto("https://www.echotik.live/board")
    # 点击"Start Now"按钮，开始使用平台功能
    await page.get_by_role("button", name="Start Now").click()

    # ==================== 第一部分：导出每日热销商品数据 ====================

    # --- 展开侧边栏「商品」菜单 ---
    # 点击侧边栏第3个菜单项（商品模块）的展开/折叠箭头图标
    # 这里尝试了多次点击（录制时的实际操作），前两次点击了SVG path元素，第三次点击了icon元素
    await page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon > path").click()
    await page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon > path").click()
    await page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon").click()

    # 在展开的子菜单中，点击"Top Sold"（热销商品排行榜）链接
    await page.locator("#arco-menu-0-submenu-inline-1").get_by_role("link", name="Top Sold").click()

    # 直接导航到热销商品排行榜页面（带有具体的查询参数）
    # time_type=daily 表示按日查看，time_range=20260305 表示日期，order=sale_cnt 按销量排序
    await page.goto("https://www.echotik.live/products/leaderboard/top-sold?time_type=daily&time_range=20260305&page=1&order=sale_cnt")

    # 点击"Daily"标签，确保选中每日数据维度
    await page.get_by_text("Daily", exact=True).click()

    # --- 设置导出记录数为200条 ---
    # 点击页面上第3个按钮（索引从0开始，nth(2)即第3个），这是记录数选择的下拉触发按钮
    await page.get_by_role("button").nth(2).click()
    # 在下拉菜单中选择"200 Records"选项
    await page.get_by_text("200 Records").click()

    # --- 触发导出并等待下载弹窗 ---
    # expect_popup() 用于捕获点击"Export"后弹出的新窗口/标签页
    async with page.expect_popup() as page1_info:
        # 点击"Export"按钮，触发数据导出（会打开新标签页进行下载）
        await page.get_by_role("button", name="Export").click()
    # 获取弹出的新页面对象（下载页面）
    page1 = await page1_info.value

    # ==================== 第二部分：导出每日最佳跨境卖家数据 ====================

    # --- 展开侧边栏「卖家/店铺」菜单 ---
    # 点击侧边栏第4个菜单项（卖家/店铺模块）的展开/折叠箭头图标
    await page.locator("div:nth-child(4) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon").click()

    # 在展开的子菜单中，点击"Best Cross-border Seller"（最佳跨境卖家）链接
    await page.locator("#arco-menu-0-submenu-inline-2").get_by_role("link", name="Best Cross-border Seller").click()

    # 点击"Daily"标签，确保选中每日数据维度
    await page.get_by_text("Daily", exact=True).click()

    # --- 设置导出记录数为200条 ---
    # 点击记录数选择的下拉触发按钮
    await page.get_by_role("button").nth(2).click()
    # 选择"200 Records"
    await page.get_by_text("200 Records").click()

    # --- 触发导出并等待下载弹窗 ---
    async with page.expect_popup() as page2_info:
        # 点击"Export"按钮导出跨境卖家数据
        await page.get_by_role("button", name="Export").click()
    # 获取弹出的下载页面对象
    page2 = await page2_info.value

    # ==================== 清理：关闭所有页面和浏览器 ====================
    # 关闭第二个弹出页面（跨境卖家导出下载页）
    await page2.close()
    # 关闭第一个弹出页面（热销商品导出下载页）
    await page1.close()
    # 关闭主操作页面
    await page.close()

    # ---------------------
    # 关闭浏览器上下文（释放会话资源）
    await context.close()
    # 关闭浏览器实例（完全退出浏览器进程）
    await browser.close()


async def main() -> None:
    """
    主入口函数：创建 Playwright 异步上下文并执行采集任务
    async_playwright() 作为异步上下文管理器，自动管理 Playwright 的启动和关闭
    """
    async with async_playwright() as playwright:
        await run(playwright)


# 脚本入口：使用 asyncio.run() 启动异步事件循环，执行主函数
asyncio.run(main())
