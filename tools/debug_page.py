#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
debug_page.py
调试工具：查看登录后页面的实际内容

使用方法：
    python debug_page.py

功能：
    1. 自动登录 Echotik
    2. 截图保存
    3. 打印页面上所有可见文本
    4. 打印侧边栏相关元素
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


async def debug_page():
    """调试页面内容"""
    setup_logger("debug")

    log_node("启动调试工具", level="START")

    proxy = _get_proxy_settings()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,  # 显示浏览器窗口
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

        # 等待页面完全加载
        log_node("等待页面完全加载（20秒）...", level="INFO")
        await page.wait_for_timeout(20_000)

        # 截图
        screenshot_path = Path("logs") / "debug_page_full.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)
        log_node("完整页面截图已保存", level="INFO", path=str(screenshot_path))

        # 获取页面文本
        log_node("获取页面文本内容...", level="INFO")
        try:
            body_text = await page.inner_text("body", timeout=5_000)
            lines = [line.strip() for line in body_text.split("\n") if line.strip()]

            print("\n" + "="*60)
            print("页面可见文本（前100行）:")
            print("="*60)
            for i, line in enumerate(lines[:100], 1):
                print(f"{i:3d}. {line}")

            # 查找侧边栏相关文本
            print("\n" + "="*60)
            print("侧边栏相关文本:")
            print("="*60)
            keywords = ["选品", "小店", "热销", "卖家", "商品", "达人", "直播"]
            for kw in keywords:
                matching = [line for line in lines if kw in line]
                if matching:
                    print(f"\n包含「{kw}」的行:")
                    for line in matching[:5]:
                        print(f"  - {line}")

        except Exception as e:
            log_node("获取页面文本失败", level="ERROR", error=str(e))

        # 检查侧边栏元素
        log_node("检查侧边栏元素...", level="INFO")
        sidebar_selectors = [
            "[class*='sidebar']",
            "[class*='side-bar']",
            "[class*='menu']",
            "nav",
            "aside",
        ]

        for sel in sidebar_selectors:
            try:
                count = await page.locator(sel).count()
                if count > 0:
                    print(f"\n找到侧边栏元素: {sel} (数量: {count})")
                    # 获取第一个元素的文本
                    text = await page.locator(sel).first.inner_text()
                    print(f"内容预览: {text[:200]}")
            except Exception as e:
                print(f"检查 {sel} 失败: {str(e)[:60]}")

        print("\n" + "="*60)
        print("调试完成，浏览器将保持打开状态60秒供检查")
        print("="*60)

        await page.wait_for_timeout(60_000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(debug_page())
