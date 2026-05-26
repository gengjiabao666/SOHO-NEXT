#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debug_page.py
调试工具：查看登录后页面的实际内容

使用方法：
    python debug_page.py

功能：
    1. 自动登录 Echotik
    2. 截图保存完整页面
    3. 打印页面上所有可见文本（前100行）
    4. 搜索并打印侧边栏相关的关键词文本
    5. 检测页面中的侧边栏 DOM 元素结构
"""

# 导入异步IO库，用于支持异步操作
import asyncio
# 导入操作系统接口库（用于环境变量等操作）
import os
# 导入路径处理库，提供面向对象的文件路径操作
from pathlib import Path

# 从 dotenv 库导入 load_dotenv，用于从 .env 文件加载环境变量
from dotenv import load_dotenv
# 从 Playwright 异步API导入异步上下文管理器
from playwright.async_api import async_playwright

# 导入项目自定义的浏览器会话管理类（封装了登录等操作）
from browser.session import BrowserSession
# 导入代理设置获取函数（从调度器模块）
from scheduler.trigger import _get_proxy_settings
# 导入日志工具：log_node 用于结构化日志输出，setup_logger 用于初始化日志配置
from utils.logger import log_node, setup_logger

# 加载 .env 文件中的环境变量（如代理配置、登录凭据等）
load_dotenv()


async def debug_page():
    """
    调试页面内容

    主要用于开发调试阶段，登录后自动分析页面结构：
    - 截取完整页面截图
    - 提取并打印页面可见文本
    - 按关键词搜索侧边栏相关内容
    - 检测常见侧边栏 CSS 选择器的匹配情况
    调试完成后浏览器保持打开60秒供人工检查。
    """
    # 初始化日志系统，设置日志标识为 "debug"
    setup_logger("debug")

    # 输出调试工具启动的日志标记
    log_node("启动调试工具", level="START")

    # 获取代理服务器配置（如果配置了代理的话）
    proxy = _get_proxy_settings()

    # 使用 async_playwright 异步上下文管理器，自动管理 Playwright 生命周期
    async with async_playwright() as pw:
        # ==================== 浏览器初始化 ====================
        # 启动 Chromium 浏览器
        browser = await pw.chromium.launch(
            headless=False,  # 显示浏览器窗口，便于人工观察调试
            proxy=proxy,     # 设置代理（可能为 None，表示不使用代理）
        )
        # 创建浏览器上下文，设置视口大小和 User-Agent
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},  # 设置浏览器视口为 1440x900 分辨率
            user_agent=(
                # 模拟 Chrome 120 浏览器的 User-Agent 字符串，避免被网站识别为自动化工具
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        # 在上下文中打开一个新的标签页
        page = await context.new_page()
        # 创建 BrowserSession 实例，用于管理登录状态等会话操作
        session = BrowserSession(context)

        # ==================== 登录流程 ====================
        try:
            # 调用会话管理器的登录方法，确保用户已登录
            await session.ensure_login(page)
            log_node("登录成功", level="INFO")
        except Exception as e:
            # 登录失败时记录错误日志，关闭浏览器并退出
            log_node("登录失败", level="ERROR", error=str(e))
            await browser.close()
            return

        # ==================== 等待页面加载 ====================
        # 等待20秒，确保页面完全加载（包括异步渲染的内容、动态加载的侧边栏等）
        log_node("等待页面完全加载（20秒）...", level="INFO")
        await page.wait_for_timeout(20_000)

        # ==================== 截图保存 ====================
        # 定义截图保存路径（logs 目录下）
        screenshot_path = Path("logs") / "debug_page_full.png"
        # 截取完整页面截图（full_page=True 会截取整个可滚动区域，而非仅视口）
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log_node("完整页面截图已保存", level="INFO", path=str(screenshot_path))

        # ==================== 获取并分析页面文本 ====================
        log_node("获取页面文本内容...", level="INFO")
        try:
            # 获取 body 元素内的所有可见文本内容，设置5秒超时
            body_text = await page.inner_text("body", timeout=5_000)
            # 按换行符分割文本，过滤掉空行，并去除每行首尾空白
            lines = [line.strip() for line in body_text.split("\n") if line.strip()]

            # 打印分隔线和标题
            print("\n" + "="*60)
            print("页面可见文本（前100行）:")
            print("="*60)
            # 遍历前100行文本并带行号打印（便于定位）
            for i, line in enumerate(lines[:100], 1):
                print(f"{i:3d}. {line}")

            # ==================== 搜索侧边栏关键词 ====================
            print("\n" + "="*60)
            print("侧边栏相关文本:")
            print("="*60)
            # 定义要搜索的侧边栏相关关键词列表
            keywords = ["选品", "小店", "热销", "卖家", "商品", "达人", "直播"]
            # 遍历每个关键词，在页面文本中搜索匹配行
            for kw in keywords:
                # 筛选包含当前关键词的所有文本行
                matching = [line for line in lines if kw in line]
                if matching:
                    print(f"\n包含「{kw}」的行:")
                    # 最多打印5条匹配结果，避免输出过多
                    for line in matching[:5]:
                        print(f"  - {line}")

        except Exception as e:
            # 获取页面文本失败时记录错误日志
            log_node("获取页面文本失败", level="ERROR", error=str(e))

        # ==================== 检查侧边栏 DOM 元素 ====================
        log_node("检查侧边栏元素...", level="INFO")
        # 定义常见的侧边栏 CSS 选择器列表，用于探测页面中的侧边栏结构
        sidebar_selectors = [
            "[class*='sidebar']",    # class 属性中包含 "sidebar" 的元素
            "[class*='side-bar']",   # class 属性中包含 "side-bar" 的元素
            "[class*='menu']",       # class 属性中包含 "menu" 的元素
            "nav",                   # HTML5 语义化导航标签
            "aside",                 # HTML5 语义化侧边栏标签
        ]

        # 遍历每个选择器，检测页面中是否存在匹配元素
        for sel in sidebar_selectors:
            try:
                # 统计当前选择器匹配到的元素数量
                count = await page.locator(sel).count()
                if count > 0:
                    # 找到匹配元素，打印选择器和数量
                    print(f"\n找到侧边栏元素: {sel} (数量: {count})")
                    # 获取第一个匹配元素的文本内容，预览前200个字符
                    text = await page.locator(sel).first.inner_text()
                    print(f"内容预览: {text[:200]}")
            except Exception as e:
                # 检查失败时打印错误信息（截取前60个字符）
                print(f"检查 {sel} 失败: {str(e)[:60]}")

        # ==================== 调试完成 ====================
        print("\n" + "="*60)
        print("调试完成，浏览器将保持打开状态60秒供检查")
        print("="*60)

        # 保持浏览器打开60秒，供人工在浏览器中检查页面元素和结构
        await page.wait_for_timeout(60_000)
        # 关闭浏览器，释放资源
        await browser.close()


# 脚本入口：当直接运行此文件时，启动异步事件循环执行调试函数
if __name__ == "__main__":
    asyncio.run(debug_page())
