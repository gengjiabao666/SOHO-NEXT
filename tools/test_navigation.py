#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_navigation.py
测试侧边栏导航功能

使用方法：
    python test_navigation.py

功能说明：
    1. 启动浏览器并登录 Echotik
    2. 使用多种 CSS/文本选择器尝试定位侧边栏中的「选品」菜单项
    3. 找到可见元素后尝试点击，并截图保存结果
    4. 用于调试和验证侧边栏导航选择器的可用性
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


async def test_navigation():
    """
    测试侧边栏导航功能

    通过多种选择器策略尝试定位并点击侧边栏中的「选品」菜单项，
    验证哪种选择器能够成功匹配到目标元素。
    测试完成后浏览器保持打开30秒供人工观察。
    """
    # 初始化日志系统，设置日志标识为 "test_nav"
    setup_logger("test_nav")

    # 输出测试开始的日志标记
    log_node("启动导航测试", level="START")

    # 获取代理服务器配置（如果配置了代理的话）
    proxy = _get_proxy_settings()

    # 使用 async_playwright 异步上下文管理器，自动管理 Playwright 生命周期
    async with async_playwright() as pw:
        # ==================== 浏览器初始化 ====================
        # 启动 Chromium 浏览器
        browser = await pw.chromium.launch(
            headless=False,  # 显示浏览器窗口便于观察测试过程
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

        # ==================== 测试侧边栏导航 ====================
        # 测试点击侧边栏中的「选品」菜单项
        log_node("测试点击「选品」", level="INFO")
        # 等待3秒，确保页面完全加载和渲染完成
        await page.wait_for_timeout(3000)

        # 定义多种选择器策略，从精确到模糊依次尝试
        # 不同选择器适用于不同的页面结构，确保至少有一种能匹配到目标元素
        selectors = [
            ":text-is('选品')",                          # Playwright 精确文本匹配选择器
            "text=选品",                                  # Playwright 文本选择器（包含匹配）
            "div:has-text('选品')",                       # 包含「选品」文本的 div 元素
            "span:has-text('选品')",                      # 包含「选品」文本的 span 元素
            "[class*='sidebar'] :text-is('选品')",        # 侧边栏容器内精确匹配「选品」
            "[class*='menu'] :text-is('选品')",           # 菜单容器内精确匹配「选品」
        ]

        # 遍历所有选择器，逐一测试
        for sel in selectors:
            try:
                # 使用当前选择器定位元素，取第一个匹配项
                loc = page.locator(sel).first
                # 获取匹配到的元素数量
                count = await loc.count()
                # 如果找到了元素，检查其是否可见；否则标记为不可见
                is_visible = await loc.is_visible() if count > 0 else False

                # 记录当前选择器的测试结果（匹配数量和可见性）
                log_node(f"选择器测试", level="INFO",
                        selector=sel,
                        count=count,
                        visible=is_visible)

                # 如果找到了可见的元素，尝试点击它
                if count > 0 and is_visible:
                    log_node(f"尝试点击...", level="INFO", selector=sel)
                    # 点击元素，设置5秒超时
                    await loc.click(timeout=5_000)
                    log_node("点击成功！", level="INFO", selector=sel)
                    # 等待2秒，让页面响应点击事件并完成导航/展开
                    await page.wait_for_timeout(2000)

                    # 截图保存点击后的页面状态，用于人工验证
                    screenshot_path = Path("logs") / "test_nav_after_click.png"
                    await page.screenshot(path=str(screenshot_path))
                    log_node("截图已保存", level="INFO", path=str(screenshot_path))
                    # 找到可用选择器并成功点击后，跳出循环不再尝试其他选择器
                    break

            except Exception as e:
                # 当前选择器失败，记录警告日志（截取错误信息前80个字符），继续尝试下一个
                log_node(f"选择器失败", level="WARN",
                        selector=sel, error=str(e)[:80])

        # 测试完成，保持浏览器打开30秒供人工检查页面状态
        log_node("测试完成，浏览器将保持打开30秒", level="INFO")
        await page.wait_for_timeout(30_000)
        # 关闭浏览器，释放资源
        await browser.close()


# 脚本入口：当直接运行此文件时，启动异步事件循环执行测试函数
if __name__ == "__main__":
    asyncio.run(test_navigation())
