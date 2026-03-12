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

import asyncio
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))

from browser.session import BrowserSession
from utils.logger import log_node

load_dotenv()

TEST_DIR = Path(__file__).parent / "test"
LOG_DIR = Path(__file__).parent / "logs"


class CategoryExporter:
    def __init__(self):
        self.xvfb_proc = None
        self.screenshot_count = 0

    async def screenshot(self, page, label: str):
        """每步截图存证"""
        self.screenshot_count += 1
        try:
            LOG_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            path = LOG_DIR / f"category_{self.screenshot_count:02d}_{label}_{ts}.png"
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
        """关闭弹窗"""
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

    async def click_by_text(self, page, text: str, desc: str = "", timeout: int = 8000) -> bool:
        """通过文字点击元素"""
        log_node(f"尝试点击: {text}", level="INFO", desc=desc)

        selectors = [
            f":text-is('{text}')",
            f"text={text}",
            f"button:has-text('{text}')",
            f"a:has-text('{text}')",
            f"span:has-text('{text}')",
            f"div:has-text('{text}')",
            f"li:has-text('{text}')",
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

                try:
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                except Exception:
                    pass

                await loc.click(timeout=timeout)
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

    async def navigate_to_top_sold(self, page):
        """导航到热销榜页面"""
        log_node("=" * 60, level="INFO")
        log_node("开始导航到热销榜", level="INFO")
        log_node("=" * 60, level="INFO")

        await self.screenshot(page, "01_initial_state")
        await self.dismiss_popup(page)

        # 步骤1：点击 Products 一级菜单
        log_node("步骤1: 点击一级菜单 Products", level="INFO")
        await self.screenshot(page, "02_before_click_products")

        success = False
        for text in ["Products", "选品"]:
            if await self.click_by_text(page, text, desc="一级菜单 Products"):
                success = True
                break

        if not success:
            log_node("文字点击失败，尝试 Arco 展开箭头", level="WARN")
            try:
                arrow = page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                await arrow.click(timeout=5_000)
                log_node("展开箭头已点击", level="INFO")
                await page.wait_for_timeout(1_500)
                success = True
            except Exception as e:
                log_node("展开箭头点击失败", level="ERROR", error=str(e)[:80])
                await self.screenshot(page, "03_products_menu_fail")
                raise RuntimeError("无法打开 Products 菜单")

        await self.screenshot(page, "03_products_menu_opened")

        # 步骤2：点击 Top Sold
        log_node("步骤2: 点击二级菜单 Top Sold", level="INFO")
        await page.wait_for_timeout(1_000)
        await self.screenshot(page, "04_before_click_top_sold")

        success = False
        for text in ["Top Sold", "热销榜"]:
            if await self.click_by_text(page, text, desc="Top Sold"):
                success = True
                break

        if not success:
            log_node("Top Sold 点击失败", level="ERROR")
            await self.screenshot(page, "05_top_sold_fail")
            raise RuntimeError("无法找到 Top Sold 菜单项")

        # 等待页面加载
        log_node("等待页面数据加载...", level="INFO")
        await page.wait_for_timeout(5_000)
        await self.screenshot(page, "05_top_sold_loaded")

        # 等待表格数据出现
        for sel in ["table tbody tr", "[class*='rank-item']", "[class*='list-item']"]:
            try:
                await page.locator(sel).first.wait_for(state="visible", timeout=10_000)
                log_node("数据已加载", level="INFO", signal=sel)
                break
            except Exception:
                continue

        await self.screenshot(page, "06_data_ready")
        log_node("热销榜页面加载完成", level="INFO")

    async def select_category(self, page, category: str = "Pet Supplies"):
        """选择商品品类"""
        log_node("=" * 60, level="INFO")
        log_node(f"开始选择品类: {category}", level="INFO")
        log_node("=" * 60, level="INFO")

        await self.screenshot(page, "07_before_category_filter")

        # 步骤1：等待 Product Category 行出现
        log_node("步骤1: 等待 Product Category 筛选器加载", level="INFO")
        try:
            await page.wait_for_selector("text=Product Category", state="visible", timeout=10_000)
            log_node("Product Category 筛选器已出现", level="INFO")
        except Exception as e:
            log_node("Product Category 筛选器未出现", level="ERROR", error=str(e)[:80])
            await self.screenshot(page, "07b_no_category_filter")
            raise RuntimeError("页面未加载 Product Category 筛选器")

        await page.wait_for_timeout(2_000)
        await self.screenshot(page, "07c_category_filter_ready")

        # 步骤2：点击 Product Category 行的 "More" 展开全部品类
        log_node("步骤2: 点击 Product Category 的 More 展开品类列表", level="INFO")

        # 策略：找到 "Product Category" 文字后，在同一行找 "More"
        more_selectors = [
            # 方法1：在包含 Product Category 的容器内找 More
            ":has-text('Product Category') >> text=More",
            ":has-text('Product Category') >> button:has-text('More')",
            ":has-text('Product Category') >> span:has-text('More')",
            # 方法2：直接找带箭头的 More（品类筛选器特有）
            "text=/More\\s*[∨▼]/",
            "button:has-text('More ∨')",
            "span:has-text('More ∨')",
        ]

        success = False
        for sel in more_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count == 0:
                    continue

                for i in range(count):
                    elem = loc.nth(i)
                    if await elem.is_visible():
                        await elem.click(timeout=5_000)
                        log_node("More 按钮已点击", level="INFO", selector=sel[:60])
                        await page.wait_for_timeout(2_000)
                        success = True
                        break
                if success:
                    break
            except Exception as e:
                log_node(f"More 按钮尝试失败", level="DEBUG",
                         selector=sel[:50], error=str(e)[:60])
                continue

        if not success:
            log_node("More 按钮点击失败，尝试直接选择品类", level="WARN")

        await self.screenshot(page, "08_more_clicked")

        # 步骤3：选择目标品类
        log_node(f"步骤3: 选择品类 {category}", level="INFO")
        await page.wait_for_timeout(1_000)

        # 尝试多种选择器
        category_selectors = [
            f"button:has-text('{category}')",
            f"span:has-text('{category}')",
            f"div:has-text('{category}')",
            f":text-is('{category}')",
            f"text={category}",
            # 品类标签通常是 pill/tag 样式
            f"[class*='tag']:has-text('{category}')",
            f"[class*='pill']:has-text('{category}')",
            f"[class*='category']:has-text('{category}')",
            # 在 Product Category 容器内找
            f":has-text('Product Category') >> :has-text('{category}')",
        ]

        success = False
        for sel in category_selectors:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count == 0:
                    continue

                # 找到可见的品类标签
                for i in range(count):
                    elem = loc.nth(i)
                    if await elem.is_visible():
                        # 滚动到元素
                        try:
                            await elem.scroll_into_view_if_needed(timeout=3_000)
                        except Exception:
                            pass

                        await elem.click(timeout=5_000)
                        log_node(f"✅ 品类已选择: {category}", level="INFO",
                                 selector=sel[:60])
                        await page.wait_for_timeout(1_500)
                        success = True
                        break
                if success:
                    break
            except Exception as e:
                log_node(f"品类选择尝试失败", level="DEBUG",
                         selector=sel[:50], error=str(e)[:60])
                continue

        if not success:
            log_node(f"❌ 品类选择失败: {category}", level="ERROR")
            await self.screenshot(page, "09_category_select_fail")
            raise RuntimeError(f"无法选择品类: {category}")

        await self.screenshot(page, "09_category_selected")

        # 步骤4：等待表格刷新
        log_node("步骤4: 等待表格刷新...", level="INFO")
        await page.wait_for_timeout(5_000)

        # 等待表格重新加载
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            log_node("networkidle 超时，继续", level="WARN")

        await self.screenshot(page, "10_table_refreshed")
        log_node(f"品类 {category} 筛选完成", level="INFO")

    async def export_data(self, page):
        """导出 200 条数据"""
        log_node("=" * 60, level="INFO")
        log_node("开始导出流程", level="INFO")
        log_node("=" * 60, level="INFO")

        downloads = []
        page.on("download", lambda d: downloads.append(d))

        # 步骤1：悬停/点击导出下拉箭头
        log_node("步骤1: 悬停导出下拉箭头", level="INFO")
        await self.screenshot(page, "11_before_export_dropdown")

        try:
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

        await self.screenshot(page, "12_dropdown_opened")

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

        await self.screenshot(page, "13_count_selected")

        await page.wait_for_timeout(3_000)

        # 步骤3：点击 Export 按钮
        log_node("步骤3: 点击 Export 按钮", level="INFO")
        try:
            await page.get_by_role("button", name="Export").click(timeout=8_000)
            log_node("Export 按钮已点击", level="INFO")
            await page.wait_for_timeout(2_000)
        except Exception as e:
            log_node("Export 按钮点击失败（可能已触发）", level="WARN",
                     error=str(e)[:60])

        await self.screenshot(page, "14_export_clicked")

        # 等待下载事件
        log_node("等待下载事件...", level="INFO")
        for i in range(10):
            await page.wait_for_timeout(2_000)
            if downloads:
                break
            if i % 2 == 0:
                await self.screenshot(page, f"15_waiting_download_{i*2}s")

        if not downloads:
            log_node("下载事件未触发", level="ERROR")
            await self.screenshot(page, "16_download_timeout")
            raise RuntimeError("下载超时，未检测到下载事件")

        # 保存文件
        download_obj = downloads[0]
        filename = download_obj.suggested_filename or "pet_supplies_top_sold.xlsx"
        save_path = TEST_DIR / filename

        log_node("保存文件...", level="INFO", filename=filename)
        await download_obj.save_as(str(save_path))

        size_kb = save_path.stat().st_size / 1024
        log_node("✅ 文件已保存", level="INFO",
                 path=str(save_path), size=f"{size_kb:.1f}KB")

        await self.screenshot(page, "17_download_complete")
        return save_path

    async def run(self):
        """主流程"""
        try:
            self.start_xvfb()

            TEST_DIR.mkdir(exist_ok=True)
            LOG_DIR.mkdir(exist_ok=True)

            account = os.getenv("ECHOTIK_ACCOUNTS", "").split(",")[0].strip()
            password = os.getenv("ECHOTIK_PASSWORDS", "").split(",")[0].strip()

            if not account or not password:
                raise RuntimeError("未配置账号密码，请检查 .env")

            log_node("账号信息", level="INFO",
                     account=account[:5] + "***",
                     password_len=len(password))

            async with async_playwright() as p:
                proxy = None
                proxy_port = os.getenv("PROXY_PORT")
                if proxy_port:
                    try:
                        import socket
                        host_ip = socket.gethostbyname(socket.gethostname())
                        proxy = {"server": f"http://{host_ip}:{proxy_port}"}
                        log_node("代理配置", level="INFO", proxy=proxy["server"])
                    except Exception:
                        pass

                browser = await p.chromium.launch(
                    headless=False,
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

                # 导航到热销榜
                await self.navigate_to_top_sold(page)

                # 选择品类
                await self.select_category(page, "Pet Supplies")

                # 导出数据
                save_path = await self.export_data(page)

                log_node("=" * 60, level="INFO")
                log_node("🎉 任务完成", level="INFO", file=str(save_path))
                log_node("=" * 60, level="INFO")

                await browser.close()

        finally:
            self.stop_xvfb()


if __name__ == "__main__":
    exporter = CategoryExporter()
    asyncio.run(exporter.run())
