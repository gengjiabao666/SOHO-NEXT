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

import asyncio
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))

from browser.session import BrowserSession
from utils.logger import log_node
from utils.notifier import notify_subscription_expired
from utils.quota import record_export, check_quota_warning


class SubscriptionExpiredError(RuntimeError):
    """账号订阅到期异常"""
    pass

load_dotenv()

TEST_DIR = Path(__file__).parent / "test"
LOG_DIR = Path(__file__).parent / "logs"

# 榜单配置
RANKING_CONFIG = {
    "top_sold": {
        "name": "热销榜",
        "menu_parent": "Products",
        "menu_child": "Top Sold",
        "has_category_filter": True,
        "time_tabs": {"d": "", "w": "Weekly", "m": "Monthly"},
    },
    "new_products": {
        "name": "新品榜",
        "menu_parent": "Products",
        "menu_child": "New Products",
        "has_category_filter": True,
        "time_tabs": {"d": ""},  # 新品榜只有日榜
    },
    "shops": {
        "name": "小店榜",
        "menu_parent": "Shop",
        "menu_child": "Best Cross-border Seller",
        "has_category_filter": True,  # 小店榜也筛选品类
        "time_tabs": {"d": "", "w": "Weekly", "m": "Monthly"},
    },
}


class BatchExporter:
    def __init__(self):
        self.screenshot_count = 0
        self.total_downloaded = 0

    async def screenshot(self, page, label: str):
        """每步截图存证"""
        self.screenshot_count += 1
        try:
            LOG_DIR.mkdir(exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            path = LOG_DIR / f"batch_{self.screenshot_count:03d}_{label}_{ts}.png"
            await page.screenshot(path=str(path), full_page=False)
            log_node(f"截图 #{self.screenshot_count}", level="INFO",
                     label=label, path=str(path))
            return str(path)
        except Exception as e:
            log_node("截图失败", level="WARN", error=str(e)[:60])
            return None

    async def check_subscription_expired(self, page, account: str = ""):
        """检测订阅到期"""
        try:
            body_text = await page.inner_text("body", timeout=2_000)
        except Exception:
            return
        if "Current free version" in body_text or "Upgrade for more privileges" in body_text:
            await self.screenshot(page, "subscription_expired")
            log_node("账号订阅到期", level="ERROR", account=account)
            notify_subscription_expired(account)
            raise SubscriptionExpiredError(f"账号订阅已到期: {account}")

    async def dismiss_popup(self, page):
        """关闭弹窗"""
        await page.wait_for_timeout(1000)
        popup_selectors = [
            "button:has-text('Start Now')",
            "button:has-text('知道了')",
            "button:has-text('确定')",
            "[class*='modal'] button:last-child",
        ]
        for sel in popup_selectors:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    await loc.first.click(timeout=3_000)
                    log_node("弹窗已关闭", level="INFO", selector=sel)
                    await page.wait_for_timeout(1_000)
                    return True
            except Exception:
                continue
        return False

    async def click_by_text(self, page, text: str, timeout: int = 8000) -> bool:
        """通过文字点击元素"""
        selectors = [
            f":text-is('{text}')",
            f"text={text}",
            f"button:has-text('{text}')",
            f"a:has-text('{text}')",
            f"span:has-text('{text}')",
            f"div:has-text('{text}')",
        ]

        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                if not await loc.is_visible():
                    continue
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=timeout)
                log_node(f"点击成功: {text}", level="INFO", selector=sel[:60])
                await page.wait_for_timeout(1_500)
                return True
            except Exception:
                continue
        return False

    async def navigate_to_ranking(self, page, ranking_type: str, account: str = ""):
        """导航到指定榜单页面"""
        config = RANKING_CONFIG[ranking_type]
        log_node("=" * 60, level="INFO")
        log_node(f"导航到 {config['name']}", level="INFO")
        log_node("=" * 60, level="INFO")

        await self.dismiss_popup(page)
        await self.check_subscription_expired(page, account)

        # 点击一级菜单
        menu_parent = config["menu_parent"]
        success = await self.click_by_text(page, menu_parent)
        if not success:
            try:
                if menu_parent == "Products":
                    arrow = page.locator("div:nth-child(3) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                else:
                    arrow = page.locator("div:nth-child(4) > .arco-menu-inline-header > .arco-menu-icon-suffix > .arco-icon")
                await arrow.click(timeout=5_000)
                await page.wait_for_timeout(1_500)
            except Exception as e:
                raise RuntimeError(f"无法打开 {menu_parent} 菜单: {e}")

        await self.check_subscription_expired(page, account)

        # 点击二级菜单
        menu_child = config["menu_child"]
        success = await self.click_by_text(page, menu_child)
        if not success:
            raise RuntimeError(f"无法找到 {menu_child} 菜单项")

        await page.wait_for_timeout(5_000)
        for sel in ["table tbody tr", "[class*='rank-item']"]:
            try:
                await page.locator(sel).first.wait_for(state="visible", timeout=10_000)
                break
            except Exception:
                continue

        await self.check_subscription_expired(page, account)
        log_node(f"{config['name']} 页面加载完成", level="INFO")

    async def select_category(self, page, category: str, account: str = ""):
        """选择商品品类"""
        log_node(f"选择品类: {category}", level="INFO")

        try:
            await page.wait_for_selector("text=Product Category", state="visible", timeout=10_000)
        except Exception:
            log_node("未找到 Product Category 筛选器（可能是小店榜）", level="WARN")
            return

        await page.wait_for_timeout(2_000)

        more_selectors = [
            ":has-text('Product Category') >> text=More",
            "text=/More\\s*[∨▼]/",
        ]
        for sel in more_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    await loc.click(timeout=5_000)
                    log_node("More 按钮已点击", level="INFO")
                    await page.wait_for_timeout(2_000)
                    break
            except Exception:
                continue

        await self.check_subscription_expired(page, account)

        category_selectors = [
            f"button:has-text('{category}')",
            f":has-text('Product Category') >> :has-text('{category}')",
        ]
        for sel in category_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible():
                    await loc.scroll_into_view_if_needed(timeout=3_000)
                    await loc.click(timeout=5_000)
                    log_node(f"品类已选择: {category}", level="INFO")
                    await page.wait_for_timeout(3_000)
                    return
            except Exception:
                continue

        raise RuntimeError(f"无法选择品类: {category}")

    async def select_time_window(self, page, time_tab: str, account: str = ""):
        """选择时间窗口（日/周/月）"""
        if not time_tab:
            log_node("日榜为默认选中，跳过 Tab 点击", level="INFO")
            return

        log_node(f"切换到时间窗口: {time_tab}", level="INFO")
        success = await self.click_by_text(page, time_tab)
        if success:
            await page.wait_for_timeout(3_000)
            await self.check_subscription_expired(page, account)
            log_node(f"时间窗口已切换: {time_tab}", level="INFO")
        else:
            log_node(f"时间窗口切换失败: {time_tab}", level="WARN")

    async def export_data(self, page, label: str, account: str = "", export_count: int = 50):
        """导出数据（支持 main download + popup download + API fallback）"""
        log_node(f"开始导出: {label}", level="INFO", export_count=export_count)

        # 等待页面稳定（解决品类选择后页面重新渲染导致元素失效的问题）
        log_node("等待页面稳定...", level="INFO")
        await page.wait_for_timeout(2_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except Exception:
            pass  # 超时也继续

        # 注册 download / popup 监听（全程生效）
        main_downloads = []
        popup_list = []
        popup_downloads = []
        download_obj = None
        popup_page = None

        def _on_main_download(d):
            main_downloads.append(d)

        def _on_popup(p):
            popup_list.append(p)

        page.on("download", _on_main_download)
        page.on("popup", _on_popup)

        try:
            # 步骤1：hover 触发下拉菜单
            log_node("步骤1: hover 导出下拉箭头", level="INFO")
            await self.screenshot(page, f"{label}_before_dropdown")

            # 多策略定位下拉箭头
            dropdown_btn = None
            dropdown_selectors = [
                ".arco-btn-group button:last-child",
                ".arco-dropdown-button button:last-child",
                "button:has-text('Export') + button",
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

            if dropdown_btn is None:
                dropdown_btn = page.get_by_role("button").nth(2)
                log_node("使用 fallback 选择器 nth(2)", level="WARN")

            try:
                await dropdown_btn.hover(timeout=8_000)
                log_node("下拉箭头已悬停", level="INFO")
                await page.wait_for_timeout(1_500)
            except Exception as e:
                log_node("hover 失败，尝试 click", level="WARN", error=str(e)[:60])
                try:
                    await dropdown_btn.click(timeout=5_000)
                except Exception:
                    pass

            await self.screenshot(page, f"{label}_dropdown_opened")
            await self.check_subscription_expired(page, account)

            # 步骤2：选择条数
            log_node("步骤2: 选择条数", level="INFO")
            await page.wait_for_timeout(800)

            count_selected = False
            count_texts = [f"{export_count} Records", f"{export_count}条", str(export_count)]
            for text in count_texts:
                try:
                    await page.get_by_text(text).click(timeout=5_000)
                    log_node(f"已选择条数: {text}", level="INFO")
                    count_selected = True
                    await page.wait_for_timeout(1_500)
                    break
                except Exception:
                    continue

            if not count_selected:
                log_node("条数选择失败", level="WARN")

            await self.screenshot(page, f"{label}_count_selected")
            await self.check_subscription_expired(page, account)

            # 等待 5 秒，看选条数是否已经触发了 popup
            await page.wait_for_timeout(5_000)

            if popup_list:
                # 选条数后直接触发了 popup
                popup_page = popup_list[0]
                log_node("选条数后直接触发了 popup", level="INFO",
                         url=popup_page.url[:120] if popup_page.url else "")
                # 在 popup 上也注册 download 监听
                def _on_popup_download(d):
                    popup_downloads.append(d)
                popup_page.on("download", _on_popup_download)
            else:
                # 步骤3：选条数没触发 popup，点 Export 按钮
                log_node("步骤3: 选条数未触发 popup，点击 Export 按钮", level="INFO")
                try:
                    async with page.expect_popup(timeout=30_000) as popup_info:
                        await page.get_by_role("button", name="Export").click(timeout=8_000)
                        log_node("Export 按钮已点击", level="INFO")
                        await page.wait_for_timeout(1_000)
                    popup_page = await popup_info.value
                    log_node("popup 已打开", level="INFO",
                             url=popup_page.url[:120] if popup_page.url else "")
                    # 在 popup 上也注册 download 监听
                    def _on_popup_download(d):
                        popup_downloads.append(d)
                    popup_page.on("download", _on_popup_download)
                except Exception as e:
                    log_node("Export 按钮点击或 popup 等待失败", level="WARN",
                             error=str(e)[:80])
                    await self.screenshot(page, f"{label}_export_fail")

            await self.screenshot(page, f"{label}_export_clicked")
            await self.check_subscription_expired(page, account)

            # 等待下载事件（最多 16s，每 2s 检查 main 和 popup）
            for _ in range(8):
                await page.wait_for_timeout(2_000)
                if main_downloads:
                    download_obj = main_downloads[0]
                    log_node("下载事件来源: main_download", level="INFO")
                    break
                if popup_downloads:
                    download_obj = popup_downloads[0]
                    log_node("下载事件来源: popup_download", level="INFO")
                    break

            # Fallback: 用 popup URL 直接请求下载
            if not download_obj and popup_page and popup_page.url:
                log_node("download 事件未触发，改用 popup URL 直接请求", level="WARN",
                         url=popup_page.url[:120])
                try:
                    api = page.context.request
                    resp = await api.get(popup_page.url)
                    if resp.status == 200:
                        body = await resp.body()
                        # 校验是否为 xlsx（以 PK 魔数开头）
                        if body[:4] == b"PK\x03\x04":
                            save_path = TEST_DIR / f"{label}.xlsx"
                            save_path.write_bytes(body)
                            size_kb = len(body) / 1024
                            log_node("文件已保存（API直接请求）", level="INFO",
                                     path=str(save_path), size=f"{size_kb:.1f}KB")
                            self.total_downloaded += 1
                            # 记录配额
                            record_export(task=label, count=export_count)
                            check_quota_warning()
                            return save_path
                        else:
                            log_node("API 返回非 xlsx 内容", level="ERROR",
                                     content_preview=body[:200].decode("utf-8", errors="replace"))
                    else:
                        log_node("API 请求失败", level="ERROR", status=resp.status)
                except Exception as e:
                    log_node("API 直接请求异常", level="ERROR", error=str(e)[:80])

            # 关闭 popup
            if popup_page:
                try:
                    await popup_page.close()
                except Exception:
                    pass

            if not download_obj:
                raise RuntimeError("下载超时：main/popup download 均未触发，API fallback 也失败")

            # 保存文件
            filename = download_obj.suggested_filename or f"{label}.xlsx"
            save_path = TEST_DIR / filename

            await download_obj.save_as(str(save_path))
            size_kb = save_path.stat().st_size / 1024
            log_node("文件已保存", level="INFO",
                     path=str(save_path), size=f"{size_kb:.1f}KB")

            self.total_downloaded += 1
            # 记录配额
            record_export(task=label, count=export_count)
            check_quota_warning()
            return save_path

        finally:
            # 清理监听器
            try:
                page.remove_listener("download", _on_main_download)
                page.remove_listener("popup", _on_popup)
            except Exception:
                pass

    async def run_task(self, page, ranking_type: str, time_window: str,
                       category: str = None, account: str = "", export_count: int = 50):
        """执行单个采集任务"""
        config = RANKING_CONFIG[ranking_type]
        time_tab = config["time_tabs"].get(time_window, "")

        label = f"{config['name']}_{time_window}"
        if category:
            label += f"_{category}"

        log_node("=" * 60, level="INFO")
        log_node(f"任务开始: {label}", level="INFO")
        log_node("=" * 60, level="INFO")

        try:
            await self.navigate_to_ranking(page, ranking_type, account)
            await self.screenshot(page, f"{label}_01_loaded")

            if category and config["has_category_filter"]:
                await self.select_category(page, category, account)
                await self.screenshot(page, f"{label}_02_category")

            await self.select_time_window(page, time_tab, account)
            await self.screenshot(page, f"{label}_03_timewindow")

            save_path = await self.export_data(page, label, account, export_count)
            await self.screenshot(page, f"{label}_04_exported")

            log_node(f"任务完成: {label}", level="INFO", file=str(save_path))
            return True

        except SubscriptionExpiredError:
            raise
        except Exception as e:
            log_node(f"任务失败: {label}", level="ERROR", error=str(e)[:120])
            await self.screenshot(page, f"{label}_99_failed")
            return False

    async def run(self, tasks: list, category: str = None, export_count: int = 50):
        """主流程（多账号轮换）"""
        TEST_DIR.mkdir(exist_ok=True)
        LOG_DIR.mkdir(exist_ok=True)

        # 读取账号列表
        accounts_str = os.getenv("ECHOTIK_ACCOUNTS", "")
        passwords_str = os.getenv("ECHOTIK_PASSWORDS", "")
        accounts = [a.strip().strip('"').strip("'")
                    for a in accounts_str.split(",") if a.strip()]
        passwords = [p.strip().strip('"').strip("'")
                     for p in passwords_str.split(",") if p.strip()]

        if not accounts:
            log_node("未配置账号", level="ERROR")
            return

        total_accounts = len(accounts)

        async with async_playwright() as p:
            for acct_idx in range(total_accounts):
                acct = accounts[acct_idx]
                pwd = passwords[acct_idx] if acct_idx < len(passwords) else ""
                acct_masked = acct[:3] + "***@" + acct.split("@")[-1] if "@" in acct else acct[:3] + "***"

                log_node(f"切换到账号 {acct_idx + 1}/{total_accounts}",
                         level="START", account=acct_masked)

                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                # 登录
                log_node("开始登录", level="INFO")
                session = BrowserSession(context)
                session.set_single_account(acct, pwd)

                try:
                    await session.ensure_login(page)
                    log_node("登录成功", level="INFO")
                except RuntimeError as e:
                    log_node("登录失败，尝试下一个账号",
                             level="WARN", account=acct_masked, error=str(e)[:80])
                    await browser.close()
                    continue

                # 执行任务列表
                success_count = 0
                try:
                    for ranking_type, time_window in tasks:
                        result = await self.run_task(
                            page, ranking_type, time_window, category, acct_masked, export_count
                        )
                        if result:
                            success_count += 1
                        await page.wait_for_timeout(2_000)

                    # 全部成功
                    log_node("=" * 60, level="INFO")
                    log_node(f"批量任务完成", level="INFO",
                             total=len(tasks), success=success_count,
                             failed=len(tasks) - success_count)
                    log_node("=" * 60, level="INFO")
                    await browser.close()
                    return  # 成功退出

                except SubscriptionExpiredError:
                    log_node("账号订阅到期，切换下一个账号",
                             level="WARN", account=acct_masked)
                    await browser.close()
                    continue

            # 所有账号都用完
            log_node("所有账号均已尝试", level="ERROR")


def parse_args():
    parser = argparse.ArgumentParser(description="批量采集 Echotik 榜单")
    parser.add_argument("--category", default="Pet Supplies", help="品类名称")
    parser.add_argument("--tasks", required=True,
                        help="任务列表，格式：top_sold:w,top_sold:m,new_products:d,shops:d")
    parser.add_argument("--count", type=int, default=50,
                        help="每个任务导出条数（默认50）")
    return parser.parse_args()


def validate_tasks(task_str: str) -> list:
    """验证并解析任务列表，拒绝无效组合"""
    tasks = []
    errors = []

    for task in task_str.split(","):
        parts = task.strip().split(":")
        if len(parts) != 2:
            errors.append(f"格式错误: {task}")
            continue

        ranking_type, time_window = parts
        if ranking_type not in RANKING_CONFIG:
            errors.append(f"未知榜单类型: {ranking_type}")
            continue

        config = RANKING_CONFIG[ranking_type]
        if time_window not in config["time_tabs"]:
            valid_wins = list(config["time_tabs"].keys())
            errors.append(f"{ranking_type} 不支持 {time_window}，有效值: {valid_wins}")
            continue

        tasks.append((ranking_type, time_window))

    if errors:
        for err in errors:
            log_node(err, level="ERROR")
        return []

    return tasks


if __name__ == "__main__":
    args = parse_args()

    # 验证任务列表
    tasks = validate_tasks(args.tasks)
    if not tasks:
        log_node("未解析到有效任务", level="ERROR")
        sys.exit(1)

    log_node("任务列表", level="INFO", tasks=tasks, category=args.category, export_count=args.count)

    exporter = BatchExporter()
    asyncio.run(exporter.run(tasks, args.category, args.count))
