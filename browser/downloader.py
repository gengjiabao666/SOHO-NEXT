#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser/downloader.py
页面导航与文件下载模块

操作流程（完整模拟真实用户点击顺序）：

商品榜日榜：
    侧边栏「选品」→「热销榜」→ 等页面加载 →
    展开导出下拉 → 选「200条」→ 点「导出」

商品榜周/月榜：
    侧边栏「选品」→「热销榜」→ 等页面加载 →
    点「周榜」/「月榜」Tab → 等数据刷新 →
    展开导出下拉 → 选「200条」→ 点「导出」

小店榜：
    侧边栏「小店」→「最佳跨境卖家」→ 等页面加载 →
    （周/月榜需点Tab）→ 展开导出下拉 → 选「200条」→ 点「导出」
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv
from playwright.async_api import Page

from browser.anomaly import check_page_anomaly
from browser.session import BrowserSession
from utils.freshness import is_fresh
from utils.logger import log_node
from utils.notifier import _notify
from utils.quota import record_export, check_quota_warning
from utils.retry import async_retry

load_dotenv()

REPO_ROOT    = os.getenv("REPO_ROOT", "/mnt/g/SOHO_repo")
BASE_EXPORTS = Path(REPO_ROOT) / "03_data_sources" / "echotik" / "exports"
TMP_DIR      = Path(REPO_ROOT) / "03_data_sources" / "echotik" / "inbox" / "_tmp"
TASKS_YAML   = os.getenv("TASKS_YAML", "config/tasks.yaml")
LOG_DIR      = Path("logs")


@dataclass
class DownloadResult:
    module:   str
    win:      str
    status:   str           # "success" / "stale" / "failed"
    tmp_path: Optional[Path] = None
    reason:   str = ""
    category: str = ""      # 品类（空字符串表示全品类）


def _load_tasks() -> list:
    with open(TASKS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)["modules"]


async def _screenshot(page: Page, label: str):
    """截图归档到 logs/"""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        ts   = datetime.now().strftime("%H%M%S")
        path = LOG_DIR / f"page_{label}_{ts}.png"
        await page.screenshot(path=str(path), full_page=False)
        log_node("截图已归档", level="INFO", path=str(path))
    except Exception as e:
        log_node("截图失败", level="WARN", error=str(e)[:60])


async def _check_subscription_expired(page: Page):
    """
    检测 Echotik 账号订阅到期弹窗。

    弹窗特征：包含 "Current free version" 或 "Upgrade for more privileges" 文本。
    只要账号过期，页面上任何操作都会弹出此提示。

    检测到后：截图存证 → 发送告警 → 终止进程。
    """
    try:
        body_text = await page.inner_text("body", timeout=2_000)
    except Exception:
        return

    if "Current free version" in body_text or "Upgrade for more privileges" in body_text:
        await _screenshot(page, "subscription_expired")
        log_node("Echotik账号到期，请检查账号池", level="ERROR")
        _notify(
            "❌ Echotik 账号到期",
            "检测到订阅到期弹窗（Current free version），本次采集终止。\n"
            "请更换账号或续费后重试。",
        )
        sys.exit(1)


async def _debug_screenshot_sequence(page: Page, label: str,
                                      interval: int = 2, count: int = 6):
    """
    调试截图序列：每 interval 秒截一张图，共 count 张。
    用于观察操作后页面的变化过程，确认稳定时间后可缩减。
    """
    for i in range(1, count + 1):
        await page.wait_for_timeout(interval * 1000)
        await _screenshot(page, f"{label}_{i * interval}s")


async def _click_by_text(page: Page, text: str, timeout: int = 8_000,
                         desc: str = "") -> bool:
    """
    点击页面上包含指定文字的元素，多种选择器依次尝试。
    返回 True=成功，False=失败。
    """
    for sel in [
        f"text={text}",
        f":text-is('{text}')",
        f"button:has-text('{text}')",
        f"a:has-text('{text}')",
        f"span:has-text('{text}')",
        f"li:has-text('{text}')",
    ]:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click(timeout=timeout)
            log_node(f"点击成功: {desc or text}", level="INFO",
                     text=text, selector=sel)
            await page.wait_for_timeout(1_000)
            await _check_subscription_expired(page)
            return True
        except Exception:
            continue
    log_node(f"点击失败: {desc or text}", level="WARN", text=text)
    return False


async def _click_by_text_enhanced(page: Page, text: str, timeout: int = 10_000,
                                   desc: str = "") -> bool:
    """
    增强版文字点击，使用更多策略和调试信息
    """
    log_node(f"尝试点击: {text}", level="INFO", desc=desc)

    # 策略1: 精确匹配
    selectors = [
        f":text-is('{text}')",
        f"text={text}",
    ]

    # 策略2: 包含匹配 + 常见容器
    for container in ["div", "span", "a", "button", "li", "p"]:
        selectors.append(f"{container}:has-text('{text}')")

    # 策略3: 侧边栏特定选择器
    for sidebar_prefix in ["[class*='sidebar']", "[class*='menu']", "nav", "aside"]:
        selectors.append(f"{sidebar_prefix} :text-is('{text}')")
        selectors.append(f"{sidebar_prefix} :has-text('{text}')")

    for i, sel in enumerate(selectors):
        try:
            loc = page.locator(sel).first
            count = await loc.count()
            if count == 0:
                continue

            # 检查元素是否可见
            is_visible = await loc.is_visible()
            if not is_visible:
                log_node(f"元素存在但不可见", level="WARN",
                        selector=sel, attempt=f"{i+1}/{len(selectors)}")
                continue

            # 尝试滚动到元素
            try:
                await loc.scroll_into_view_if_needed(timeout=3_000)
            except Exception:
                pass

            # 等待元素可点击
            await loc.wait_for(state="visible", timeout=timeout)

            # 点击
            await loc.click(timeout=timeout, force=False)
            log_node(f"点击成功: {desc or text}", level="INFO",
                     text=text, selector=sel, attempt=f"{i+1}/{len(selectors)}")
            await page.wait_for_timeout(1_000)
            await _check_subscription_expired(page)
            return True

        except Exception as e:
            if i < 5:  # 只记录前几次尝试的详细错误
                log_node(f"尝试 {i+1} 失败", level="DEBUG",
                        selector=sel[:60], error=str(e)[:80])
            continue

    # 所有策略都失败，打印调试信息
    log_node(f"点击失败（尝试了{len(selectors)}种选择器）: {desc or text}",
             level="WARN", text=text)

    # 尝试获取页面上所有可见文本，帮助调试
    try:
        body_text = await page.inner_text("body", timeout=3_000)
        if text in body_text:
            log_node(f"文字存在于页面中，但无法定位元素", level="WARN",
                    text=text, hint="可能被遮挡或在iframe中")
        else:
            log_node(f"文字不存在于页面中", level="WARN",
                    text=text, hint="页面可能未完全加载或文字已变更")
    except Exception:
        pass

    return False


_ERROR_POPUP_KEYWORDS = [
    "something went wrong",
    "feature is limited",
    "Current free version",
    "Upgrade for more privileges",
]


def _is_error_popup(text: str) -> bool:
    """检测 popup 页面文本是否为错误/限额提示"""
    if not text:
        return False
    for kw in _ERROR_POPUP_KEYWORDS:
        if kw.lower() in text.lower():
            log_node("检测到导出限额/错误提示", level="ERROR", keyword=kw,
                     text=text[:120])
            return True
    return False


async def _wait_for_data(page: Page, module_name: str, win: str):
    """等待页面数据加载完成，截图归档"""
    # 等待表格行出现作为加载完成的信号，最多等30秒
    log_node("等待页面数据加载...", level="INFO", module=module_name)
    for data_sel in ["table tbody tr", "[class*='rank-item']",
                     "[class*='list-item']", "[class*='table-row']"]:
        try:
            await page.locator(data_sel).first.wait_for(
                state="visible", timeout=60_000)
            log_node("页面数据已加载", level="INFO",
                     module=module_name, signal=data_sel)
            break
        except Exception:
            continue
    else:
        log_node("未检测到数据加载信号，等待5秒后继续",
                 level="WARN", module=module_name)
        await page.wait_for_timeout(5_000)

    await _screenshot(page, f"{module_name}_{win}_loaded")


async def _select_category(page: Page, category: str, module_name: str, win: str):
    """
    选择商品品类

    流程：
        1. 找到 Product Category 筛选器
        2. 点击 More 展开全部品类
        3. 点击目标品类
    """
    # 等待筛选器出现
    try:
        await page.wait_for_selector("text=Product Category", state="visible", timeout=10_000)
    except Exception:
        log_node("未找到 Product Category 筛选器", level="WARN",
                 module=module_name, category=category)
        return

    await page.wait_for_timeout(2_000)

    # 点击 More 展开全部品类
    more_selectors = [
        ":has-text('Product Category') >> text=More",
        "text=/More\\s*[∨▼]/",
        "button:has-text('More')",
    ]
    for sel in more_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.click(timeout=5_000)
                log_node("More 按钮已点击", level="INFO", module=module_name)
                await page.wait_for_timeout(2_000)
                break
        except Exception:
            continue

    await _check_subscription_expired(page)

    # 点击目标品类
    category_selectors = [
        f"button:has-text('{category}')",
        f":has-text('Product Category') >> :has-text('{category}')",
        f"text={category}",
    ]
    for sel in category_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible():
                await loc.scroll_into_view_if_needed(timeout=3_000)
                await loc.click(timeout=5_000)
                log_node(f"品类已选择: {category}", level="INFO", module=module_name)
                await page.wait_for_timeout(3_000)
                return
        except Exception:
            continue

    log_node(f"品类选择失败: {category}", level="WARN", module=module_name)


async def download_all(
    wins: list[str],
    captured: str,
    session: BrowserSession,
    page: Page,
    module_filter: str = "",
) -> List[DownloadResult]:
    """旧接口：按 wins 列表下载全品类"""
    modules = _load_tasks()
    results = []
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    for module in modules:
        if module_filter and module["name"] != module_filter:
            continue
        for win in wins:
            if win not in module.get("wins", []):
                continue
            result = await _download_one(page, module, win, captured, category="")
            results.append(result)

    return results


async def download_all_v2(
    tasks: list[dict],
    captured: str,
    session: BrowserSession,
    page: Page,
) -> List[DownloadResult]:
    """
    新接口：按详细任务列表下载（支持品类筛选）

    参数：
        tasks: 任务列表，每个任务包含 {module, win, category}
    """
    modules_config = {m["name"]: m for m in _load_tasks()}
    results = []
    TMP_DIR.mkdir(parents=True, exist_ok=True)

    for task in tasks:
        module_name = task["module"]
        win = task["win"]
        category = task.get("category", "")

        if module_name not in modules_config:
            log_node(f"未知模块: {module_name}", level="WARN")
            continue

        module = modules_config[module_name]
        if win not in module.get("wins", []):
            log_node(f"模块 {module_name} 不支持 {win}", level="WARN")
            continue

        result = await _download_one(page, module, win, captured, category=category)
        results.append(result)

    return results


@async_retry(max_attempts=3, base_delay=30.0)
async def _download_one(
    page: Page,
    module: dict,
    win: str,
    captured: str,
    category: str = "",
) -> DownloadResult:
    module_name         = module["name"]
    nav_parent_selector = module["nav_parent_selector"]  # 一级菜单展开箭头
    submenu_id          = module["submenu_id"]           # 子菜单容器 ID
    nav_child           = module["nav_child"]            # 二级菜单链接文字
    time_tab_map        = module.get("time_tab_map", {})
    time_tab            = time_tab_map.get(win, "")      # 日榜为空（默认选中）
    export_count        = module.get("export_count", "200 Records")
    ds                  = module.get("ds", "p")
    has_category_filter = module.get("has_category_filter", False)

    # 任务标签（用于日志和文件命名）
    task_label = f"{module_name}_{win}"
    if category:
        task_label += f"_{category}"

    log_node("开始下载", level="INFO",
             module=module_name, win=win, category=category or "全品类",
             export_count=export_count)

    # ══════════════════════════════════════════
    # 步骤1：侧边栏导航（基于 Playwright 录制的精确选择器）
    # ══════════════════════════════════════════
    log_node(f"侧边栏导航: {nav_child}",
             level="INFO", module=module_name)

    # 截图导航前状态
    await _screenshot(page, f"{module_name}_{win}_before_nav")

    # 点击前先关闭登录后的欢迎弹窗（Start Now）
    try:
        loc = page.locator("button:has-text('Start Now')")
        if await loc.count() > 0:
            await loc.first.click(timeout=3_000)
            log_node("欢迎弹窗已关闭", level="INFO")
            await page.wait_for_timeout(1_000)
            await _check_subscription_expired(page)
    except Exception:
        pass

    # 点一级菜单展开箭头（录制选择器）
    # 先检查子菜单是否已展开（重试时可能已展开，再点会收起）
    submenu_visible = False
    try:
        submenu_visible = await page.locator(f"#{submenu_id}").is_visible()
    except Exception:
        pass

    if submenu_visible:
        log_node("子菜单已展开，跳过一级菜单点击", level="INFO",
                 module=module_name, submenu_id=submenu_id)
    else:
        log_node("点击一级菜单展开箭头", level="INFO", module=module_name,
                 selector=nav_parent_selector)
        try:
            loc = page.locator(nav_parent_selector)
            await loc.scroll_into_view_if_needed(timeout=5_000)
            await loc.click(timeout=8_000)
            log_node("一级菜单已展开", level="INFO", module=module_name)
            await page.wait_for_timeout(1_000)
            await _check_subscription_expired(page)
        except Exception as e:
            await _screenshot(page, f"{module_name}_{win}_nav_fail")
            return DownloadResult(module=module_name, win=win, category=category,
                                  status="failed",
                                  reason=f"一级菜单展开失败: {str(e)[:60]}")

        await page.wait_for_timeout(1_000)  # 等子菜单展开动画

    # 点二级菜单链接（在 submenu 容器内精确匹配）
    log_node(f"点击二级菜单: {nav_child}", level="INFO", module=module_name,
             submenu_id=submenu_id)
    try:
        loc = page.locator(f"#{submenu_id}").get_by_role("link", name=nav_child)
        await loc.wait_for(state="visible", timeout=5_000)
        await loc.click(timeout=8_000)
        log_node("二级菜单已点击", level="INFO", module=module_name,
                 nav_child=nav_child)
        await page.wait_for_timeout(1_000)
        await _check_subscription_expired(page)
    except Exception as e:
        await _screenshot(page, f"{module_name}_{win}_nav_fail")
        return DownloadResult(module=module_name, win=win, category=category,
                              status="failed",
                              reason=f"二级菜单点击失败: {str(e)[:60]}")

    # 导航完成后等待页面响应
    await page.wait_for_timeout(5_000)

    # ══════════════════════════════════════════
    # 步骤2：等待页面数据加载完成
    # ══════════════════════════════════════════
    await _wait_for_data(page, module_name, win)

    # ══════════════════════════════════════════
    # 步骤3：页面异常检测
    # ══════════════════════════════════════════
    status, reason = await check_page_anomaly(
        page, context_desc=f"{module_name}_{win}导航后"
    )
    if status in ("captcha", "blocked"):
        await _screenshot(page, f"{module_name}_{win}_anomaly")
        return DownloadResult(module=module_name, win=win, category=category,
                              status="failed", reason=reason)
    if status == "error":
        log_node("页面错误，标记failed等待重试", level="WARN",
                 module=module_name, reason=reason)
        await _screenshot(page, f"{module_name}_{win}_error")
        return DownloadResult(module=module_name, win=win, category=category,
                              status="failed", reason=reason)

    log_node("页面状态正常", level="INFO", module=module_name)

    # ══════════════════════════════════════════
    # 步骤3.5：品类筛选（如果指定了品类）
    # ══════════════════════════════════════════
    if category and has_category_filter:
        log_node(f"选择品类: {category}", level="INFO", module=module_name)
        await _select_category(page, category, module_name, win)
        await _screenshot(page, f"{task_label}_category_selected")

    # ══════════════════════════════════════════
    # 步骤4：点击时间 Tab（日榜为默认，跳过）
    # ══════════════════════════════════════════
    if time_tab:
        log_node(f"点击时间Tab: {time_tab}", level="INFO",
                 module=module_name, win=win)
        ok = await _click_by_text(page, time_tab,
                                  desc=f"时间Tab: {time_tab}")
        if ok:
            await page.wait_for_timeout(3_000)  # 等待Tab切换后数据刷新
            await _screenshot(page, f"{module_name}_{win}_tab_switched")
        else:
            log_node("Tab点击失败，截图后继续",
                     level="WARN", tab=time_tab, win=win)
            await _screenshot(page, f"{module_name}_{win}_tab_fail")
    else:
        log_node(f"日榜为默认选中，无需点Tab", level="INFO",
                 module=module_name)

    # ══════════════════════════════════════════
    # 步骤5：导出（悬停下拉 → 选条数 → 等 popup → 兜底点 Export）
    # ══════════════════════════════════════════

    # 注册 download / popup 监听（全程生效）
    download_obj = None
    download_from = None
    popup_page = None
    popup_url = None
    main_downloads = []
    popup_list = []
    page.on("download", lambda d: main_downloads.append(d))
    page.on("popup", lambda p: popup_list.append(p))

    # 5-1 悬停导出下拉箭头，触发条数菜单弹出
    log_node("导出1：悬停下拉箭头", level="INFO", module=module_name)
    try:
        dropdown_btn = page.get_by_role("button").nth(2)
        await dropdown_btn.hover(timeout=8_000)
        log_node("下拉菜单已弹出", level="INFO")
        await page.wait_for_timeout(1_000)
        await _check_subscription_expired(page)
    except Exception as e:
        log_node("下拉箭头悬停失败，尝试点击",
                 level="WARN", error=str(e)[:60])
        try:
            await dropdown_btn.click(timeout=5_000)
        except Exception:
            pass

    await page.wait_for_timeout(800)  # 等待下拉菜单动画
    await _screenshot(page, f"{module_name}_{win}_dropdown_hover")

    # 5-2 选择条数（选完可能直接触发导出 popup）
    log_node(f"导出2：选择条数 {export_count}", level="INFO",
             module=module_name)
    try:
        await page.get_by_text(export_count).click(timeout=5_000)
        log_node(f"条数已选择: {export_count}", level="INFO")
        await page.wait_for_timeout(1_000)
        await _check_subscription_expired(page)
    except Exception as e:
        log_node(f"条数选择失败: {export_count}",
                 level="WARN", error=str(e)[:60])
        await _screenshot(page, f"{module_name}_{win}_count_fail")

    # 等待 5 秒，看选条数是否已经触发了 popup
    await page.wait_for_timeout(5_000)

    await _screenshot(page, f"{module_name}_{win}_after_count")

    if popup_list:
        popup_page = popup_list[0]
        popup_url = popup_page.url
        log_node("选条数后直接触发了 popup", level="INFO",
                 url=popup_url[:120])
        # 读取 popup 内容，检测是否为错误/限额页面
        popup_body = ""
        try:
            await popup_page.wait_for_load_state("domcontentloaded", timeout=5_000)
            await _screenshot(popup_page, f"{module_name}_{win}_popup_content")
            popup_body = await popup_page.inner_text("body", timeout=3_000)
            log_node("popup 页面文本", level="INFO",
                     text=popup_body[:200], url=popup_url[:200])
        except Exception as e:
            log_node("popup 截图/读取失败", level="WARN", error=str(e)[:80])
        # 检测限额/错误提示
        if _is_error_popup(popup_body):
            try:
                await popup_page.close()
            except Exception:
                pass
            return DownloadResult(module=module_name, win=win, category=category,
                                  status="failed",
                                  reason=f"导出受限: {popup_body[:100]}")
    else:
        # 5-3 兜底：选条数没触发 popup，点 Export 按钮
        log_node("导出3：选条数未触发 popup，点击 Export 按钮",
                 level="INFO", module=module_name)
        try:
            async with page.expect_popup(timeout=30_000) as popup_info:
                await page.get_by_role("button", name="Export").click(
                    timeout=8_000)
                log_node("Export 按钮已点击", level="INFO")
                await page.wait_for_timeout(1_000)
                await _check_subscription_expired(page)
            popup_page = await popup_info.value
            popup_url = popup_page.url
            log_node("popup 已打开", level="INFO", url=popup_url[:120])
            # 读取 popup 内容，检测是否为错误/限额页面
            popup_body = ""
            try:
                await popup_page.wait_for_load_state("domcontentloaded", timeout=5_000)
                await _screenshot(popup_page, f"{module_name}_{win}_export_popup_content")
                popup_body = await popup_page.inner_text("body", timeout=3_000)
                log_node("Export popup 页面文本", level="INFO",
                         text=popup_body[:200], url=popup_url[:200])
            except Exception as e:
                log_node("Export popup 截图失败", level="WARN", error=str(e)[:80])
            if _is_error_popup(popup_body):
                try:
                    await popup_page.close()
                except Exception:
                    pass
                return DownloadResult(module=module_name, win=win, category=category,
                                      status="failed",
                                      reason=f"导出受限: {popup_body[:100]}")
        except Exception as e:
            await _screenshot(page, f"{module_name}_{win}_export_fail")
            raise RuntimeError(f"导出失败，popup 未弹出: {str(e)[:80]}")

    # 在 popup 上也注册 download 监听
    popup_downloads = []
    popup_page.on("download", lambda d: popup_downloads.append(d))

    # 等 download 事件（最多 15s，每 2s 检查）
    for _ in range(8):
        await page.wait_for_timeout(2_000)
        if main_downloads:
            download_obj = main_downloads[0]
            download_from = "main_download"
            break
        if popup_downloads:
            download_obj = popup_downloads[0]
            download_from = "popup_download"
            break

    # 关闭 popup
    if popup_page:
        try:
            await popup_page.close()
        except Exception:
            pass

    # ══════════════════════════════════════════
    # 步骤6：保存文件
    # ══════════════════════════════════════════
    tmp_path = None
    orig_name = None

    if download_obj:
        # 方式A：通过 download 事件保存
        log_node(f"下载事件来源: {download_from}", level="INFO",
                 module=module_name,
                 filename=download_obj.suggested_filename)
        orig_name = download_obj.suggested_filename or f"{module_name}_{win}.xlsx"
        tmp_path  = TMP_DIR / orig_name
        await download_obj.save_as(str(tmp_path))
    else:
        # 方式B：download 事件未触发，用 popup URL 直接请求下载
        log_node("download 事件未触发，改用 API 直接请求",
                 level="WARN", module=module_name, url=popup_url[:120])
        api = page.context.request
        resp = await api.get(popup_url)
        if resp.status != 200:
            raise RuntimeError(
                f"API 直接请求失败: HTTP {resp.status}, url={popup_url[:80]}")
        body = await resp.body()
        # 校验返回内容是否为 HTML 错误页（xlsx 以 PK 魔数开头）
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or (body[:4] != b"PK\x03\x04" and b"<html" in body[:500].lower()):
            body_text = body[:500].decode("utf-8", errors="replace")
            log_node("API 返回 HTML 错误页而非 xlsx", level="ERROR",
                     module=module_name, content_type=content_type,
                     body=body_text[:200])
            return DownloadResult(module=module_name, win=win, category=category,
                                  status="failed",
                                  reason=f"API返回HTML错误页: {body_text[:100]}")
        # 从 Content-Disposition 提取文件名，兜底用模块名
        cd = resp.headers.get("content-disposition", "")
        if "filename=" in cd:
            orig_name = cd.split("filename=")[-1].strip().strip('"').strip("'")
        else:
            orig_name = f"{module_name}_{win}.xlsx"
        tmp_path = TMP_DIR / orig_name
        tmp_path.write_bytes(body)
        download_from = "api_request"
        log_node(f"API 直接请求成功", level="INFO",
                 module=module_name, filename=orig_name)

    size_kb = tmp_path.stat().st_size / 1024
    log_node("下载完成", level="INFO",
             module=module_name, win=win,
             file=orig_name, size=f"{size_kb:.1f}KB")

    # 记录配额（从 "200 Records" 提取数字）
    try:
        count_num = int(export_count.split()[0])
        record_export(task=f"{module_name}_{win}", count=count_num)
        check_quota_warning()
    except Exception:
        pass  # 解析失败不影响主流程

    # ══════════════════════════════════════════
    # 步骤7：新鲜度检测
    # ══════════════════════════════════════════
    fresh = is_fresh(tmp_path, BASE_EXPORTS, captured, win, ds)
    if not fresh:
        log_node("数据未更新（MD5与历史文件相同）", level="WARN",
                 module=module_name, win=win)
        return DownloadResult(module=module_name, win=win, category=category,
                              status="stale", tmp_path=tmp_path)

    log_node("数据已更新，文件准备就绪", level="INFO",
             module=module_name, win=win)
    return DownloadResult(module=module_name, win=win, category=category,
                          status="success", tmp_path=tmp_path)