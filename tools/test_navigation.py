#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_navigation.py
测试侧边栏导航功能

使用方法：
    python test_navigation.py
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from browser.session import BrowserSession
from scheduler.trigger import _get_proxy_settings
from utils.logger import log_node, setup_logger

load_dotenv()


async def test_navigation():
    """测试侧边栏导航"""
    setup_logger("test_nav")

    log_node("启动导航测试", level="START")

    proxy = _get_proxy_settings()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # 显示浏览器窗口便于观察
            proxy=proxy,
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        session = BrowserSession(context)

        # 登录
        try:
            await session.ensure_login(page)
            log_node("登录成功", level="INFO")
        except Exception as e:
            log_node("登录失败", level="ERROR", error=str(e))
            await browser.close()
            return

        # 测试点击「选品」
        log_node("测试点击「选品」", level="INFO")
        await page.wait_for_timeout(3000)

        # 尝试多种选择器
        selectors = [
            ":text-is('选品')",
            "text=选品",
            "div:has-text('选品')",
            "span:has-text('选品')",
            "[class*='sidebar'] :text-is('选品')",
            "[class*='menu'] :text-is('选品')",
        ]

        for sel in selectors:
            try:
                loc = page.locator(sel).first
                count = await loc.count()
                is_visible = await loc.is_visible() if count > 0 else False

                log_node(f"选择器测试", level="INFO",
                        selector=sel,
                        count=count,
                        visible=is_visible)

                if count > 0 and is_visible:
                    log_node(f"尝试点击...", level="INFO", selector=sel)
                    await loc.click(timeout=5_000)
                    log_node("点击成功！", level="INFO", selector=sel)
                    await page.wait_for_timeout(2000)

                    # 截图
                    screenshot_path = Path("logs") / "test_nav_after_click.png"
                    await page.screenshot(path=str(screenshot_path))
                    log_node("截图已保存", level="INFO", path=str(screenshot_path))
                    break

            except Exception as e:
                log_node(f"选择器失败", level="WARN",
                        selector=sel, error=str(e)[:80])

        log_node("测试完成，浏览器将保持打开30秒", level="INFO")
        await page.wait_for_timeout(30_000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(test_navigation())
