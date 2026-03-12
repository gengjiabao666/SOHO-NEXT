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

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent))

from browser.session import BrowserSession
from utils.logger import log_node

load_dotenv()

TEST_DIR = Path(__file__).parent / "test"
LOG_DIR = Path(__file__).parent / "logs"


class VisualExporter:
    def __init__(self):
        self.xvfb_proc = None
        self.screenshot_count = 0

    async def screenshot(self, page, label: str):
        """每步截图存证"""
        self.screenshot_count += 1
        try:
            LOG_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            path = LOG_DIR / f"visual_{self.screenshot_count:02d}_{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=False)
            log_node(f"📸 截图 #{self.screenshot_count}", level="INFO",
                     label=label, path=str(path))
            return str(path)
        except Exception as e:
            log_node("截图失败", level="WARN", error=str(e)[:60])
            return None

    def start_xvfb(self):
        """启动 Xvfb 虚拟显示"""
        log_node("启动 Xvfb 虚拟显示", level="INFO")
        try:
            self.xvfb_proc = subprocess.Popen(
                ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            os.environ["DISPLAY"] = ":99"
            log_node("Xvfb 已启动", level="INFO", display=":99")
        except Exception as e:
            log_node("Xvfb 启动失败", level="ERROR", error=str(e))
            raise

    def stop_xvfb(self):
        """停止 Xvfb"""
        if self.xvfb_proc:
            self.xvfb_proc.terminate()
            self.xvfb_proc.wait()
            log_node("Xvfb 已停止", level="INFO")

    async def dismiss_popup(self, page):
        """关闭弹窗（复用 session.py 的逻辑 + 视觉兜底）"""
        log_node("检查弹窗...", level="INFO")
        await page.wait_for_timeout(2000)

        popup_selectors = [
            "button:has-text('Start Now')",
            "button:has-text('start now')",
            "button:has-text('立即开始')",
            "button:has-text('知道了')",
            "button:has-text('确定')",
            "button:has-text('OK')",
            "button:has-text('Close')",
            "[class*='modal'] button:last-child",
            "[class*='dialog'] button:last-child",
        ]

        for sel in popup_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=5_000)
                    log_node("弹窗已关闭", level="INFO", selector=sel)
                    await page.wait_for_timeout(2_000)
                    await self.screenshot(page, "popup_closed")
                    return True
            except Exception:
                continue

        log_node("未检测到弹窗", level="INFO")
        return False

    async def click_by_text(self, page, text: str, desc: str = "") -> bool:
        """通过文字点击元素（多种选择器策略）"""
        log_node(f"尝试点击: {text}", level="INFO", desc=desc)

        selectors = [
            f":text-is('{text}')",
            f"text={text}",
            f"button:has-text('{text}')",
            f"a:has-text('{text}')",
            f"span:has-text('{text}')",
            f"div:has-text('{text}')",
            f"li:has-text('{text}')",
            # 侧边栏特定
            f"[class*='sidebar'] :text-is('{text}')",
            f"[class*='menu'] :text-is('{text}')",
            f"nav :text-is('{text}')",
        ]

        for i, sel in enumerate(selectors):
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue

                if not await loc.is_visible():
                    continue

                # 滚动到元素
                try:
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass

                await loc.click(timeout=8_000)
                log_node(f"✅ 点击成功: {text}", level="INFO",
                         selector=sel[:60], attempt=f"{i+1}/{len(selectors)}")
                await page.wait_for_timeout(1_500)
                return True

            except Exception as e:
                if i < 3:
                    log_node(f"尝试 {i+1} 失败", level="DEBUG",
                             selector=sel[:50], error=str(e)[:60])
                continue

        log_node(f"❌ 点击失败: {text}", level="WARN", desc=desc)
        return False

    async def navigate_to_new_products(self, page):
        """导航到新品榜页面"""
        log_node("=" * 60, level="INFO")
        log_node("开始导航到新品榜", level="INFO")
        log_node("=" * 60, level="INFO")

        # 截图初始状态
        await self.screenshot(page, "01_initial_state")

        # 关闭可能的弹窗
        await self.dismiss_popup(page)

        # 步骤1：点击"Products"一级菜单（纯侧边栏点击，避免 URL 跳转风险）
        log_node("步骤1: 点击一级菜单 Products", level="INFO")
        await self.screenshot(page, "02_before_click_sourcing")

        # 策略1：尝试文字点击
        success = False
        for text in ["Products", "选品"]:
            if await self.click_by_text(page, text, desc="一级菜单 Products"):
                success = True
                break

        # 策略2：文字点击失败，用 Arco Design 展开箭头选择器
        if not success:
            log_node("文字点击失败，尝试 Arco 展开箭头", level="WARN")
            try:
                # Products 是第3个菜单项（从 tasks.yaml 确认）
                arrow = page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                await arrow.click(timeout=5_000)
                log_node("展开箭头已点击", level="INFO")
                await page.wait_for_timeout(1_500)
                success = True
            except Exception as e:
                log_node("展开箭头点击失败", level="ERROR", error=str(e)[:80])
                await self.screenshot(page, "03_products_menu_fail")
                raise RuntimeError("无法打开 Products 菜单")

        if not success:
            raise RuntimeError("所有策略均失败，无法打开 Products 菜单")

        await self.screenshot(page, "03_sourcing_menu_opened")

        # 步骤2：点击"新品榜"或"New Products"
        log_node("步骤2: 点击二级菜单（新品榜/New Products）", level="INFO")
        await page.wait_for_timeout(1_000)
        await self.screenshot(page, "04_before_click_new_products")

        success = False
        for text in ["新品榜", "New Products", "New Arrivals", "Trending New"]:
            if await self.click_by_text(page, text, desc="新品榜"):
                success = True
                break

        if not success:
            log_node("新品榜点击失败", level="ERROR")
            await self.screenshot(page, "05_new_products_fail")
            raise RuntimeError("无法找到新品榜菜单项")

        # 等待页面加载
        log_node("等待页面数据加载...", level="INFO")
        await page.wait_for_timeout(5_000)
        await self.screenshot(page, "05_new_products_loaded")

        # 等待表格数据出现
        for sel in ["table tbody tr", "[class*='rank-item']", "[class*='list-item']"]:
            try:
                await page.locator(sel).first.wait_for(state="visible", timeout=10_000)
                log_node("数据已加载", level="INFO", signal=sel)
                break
            except Exception:
                continue

        await self.screenshot(page, "06_data_ready")
        log_node("新品榜页面加载完成", level="INFO")

    async def export_data(self, page):
        """导出 200 条数据"""
        log_node("=" * 60, level="INFO")
        log_node("开始导出流程", level="INFO")
        log_node("=" * 60, level="INFO")

        # 注册下载监听
        downloads = []
        page.on("download", lambda d: downloads.append(d))

        # 步骤1：悬停/点击导出下拉箭头
        log_node("步骤1: 悬停导出下拉箭头", level="INFO")
        await self.screenshot(page, "07_before_export_dropdown")

        try:
            # 尝试第3个按钮（参考 downloader.py）
            dropdown_btn = page.get_by_role("button").nth(2)
            await dropdown_btn.hover(timeout=8_000)
            log_node("下拉箭头已悬停", level="INFO")
            await page.wait_for_timeout(1_500)
        except Exception as e:
            log_node("悬停失败，尝试点击", level="WARN", error=str(e)[:60])
            try:
                await dropdown_btn.click(timeout=5_000)
            except Exception:
                pass

        await self.screenshot(page, "08_dropdown_opened")

        # 步骤2：选择 200 条
        log_node("步骤2: 选择 200 Records", level="INFO")
        await page.wait_for_timeout(800)

        success = False
        for text in ["200 Records", "200条", "200"]:
            try:
                await page.get_by_text(text).click(timeout=5_000)
                log_node(f"已选择: {text}", level="INFO")
                success = True
                await page.wait_for_timeout(1_500)
                break
            except Exception:
                continue

        if not success:
            log_node("选择条数失败", level="WARN")

        await self.screenshot(page, "09_count_selected")

        # 等待看是否直接触发了 popup
        await page.wait_for_timeout(3_000)

        # 步骤3：点击 Export 按钮（如果还没触发）
        log_node("步骤3: 点击 Export 按钮", level="INFO")
        try:
            await page.get_by_role("button", name="Export").click(timeout=8_000)
            log_node("Export 按钮已点击", level="INFO")
            await page.wait_for_timeout(2_000)
        except Exception as e:
            log_node("Export 按钮点击失败（可能已触发）", level="WARN",
                     error=str(e)[:60])

        await self.screenshot(page, "10_export_clicked")

        # 等待下载事件（最多 20 秒）
        log_node("等待下载事件...", level="INFO")
        for i in range(10):
            await page.wait_for_timeout(2_000)
            if downloads:
                break
            if i % 2 == 0:
                await self.screenshot(page, f"11_waiting_download_{i*2}s")

        if not downloads:
            log_node("下载事件未触发", level="ERROR")
            await self.screenshot(page, "12_download_timeout")
            raise RuntimeError("下载超时，未检测到下载事件")

        # 保存文件
        download_obj = downloads[0]
        filename = download_obj.suggested_filename or "new_products.xlsx"
        save_path = TEST_DIR / filename

        log_node("保存文件...", level="INFO", filename=filename)
        await download_obj.save_as(str(save_path))

        size_kb = save_path.stat().st_size / 1024
        log_node("✅ 文件已保存", level="INFO",
                 path=str(save_path), size=f"{size_kb:.1f}KB")

        await self.screenshot(page, "13_download_complete")
        return save_path

    async def run(self):
        """主流程"""
        try:
            # 启动 Xvfb
            self.start_xvfb()

            # 创建目录
            TEST_DIR.mkdir(exist_ok=True)
            LOG_DIR.mkdir(exist_ok=True)

            # 读取账号密码
            account = os.getenv("ECHOTIK_ACCOUNTS", "").split(",")[0].strip()
            password = os.getenv("ECHOTIK_PASSWORDS", "").split(",")[0].strip()

            if not account or not password:
                raise RuntimeError("未配置账号密码，请检查 .env")

            log_node("账号信息", level="INFO",
                     account=account[:5] + "***",
                     password_len=len(password))

            # 启动 Playwright
            async with async_playwright() as p:
                # 配置代理（如果有）
                proxy = None
                proxy_port = os.getenv("PROXY_PORT")
                if proxy_port:
                    # 获取宿主机 IP（WSL2）
                    try:
                        import socket
                        host_ip = socket.gethostbyname(socket.gethostname())
                        proxy = {"server": f"http://{host_ip}:{proxy_port}"}
                        log_node("代理配置", level="INFO", proxy=proxy["server"])
                    except Exception:
                        pass

                browser = await p.chromium.launch(
                    headless=False,  # Xvfb 模式下用 False 可以截图
                    proxy=proxy
                )

                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080}
                )

                page = await context.new_page()

                # 登录
                log_node("=" * 60, level="INFO")
                log_node("开始登录流程", level="INFO")
                log_node("=" * 60, level="INFO")

                session = BrowserSession(context)
                session.set_single_account(account, password)
                await session.ensure_login(page)

                log_node("登录成功", level="INFO")
                await self.screenshot(page, "00_login_success")

                # 导航到新品榜
                await self.navigate_to_new_products(page)

                # 导出数据
                save_path = await self.export_data(page)

                # 完成
                log_node("=" * 60, level="INFO")
                log_node("🎉 任务完成", level="INFO", file=str(save_path))
                log_node("=" * 60, level="INFO")

                await browser.close()

        finally:
            self.stop_xvfb()


if __name__ == "__main__":
    exporter = VisualExporter()
    asyncio.run(exporter.run())
