#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser/popup_handler.py
统一弹窗处理模块 —— 三层递进检测

第1层：硬编码选择器（已知弹窗，快速命中）
第2层：通用 DOM overlay 检测（未知弹窗，基于结构特征）
第3层：AI 视觉兜底（操作超时后调用，识别未知遮挡物）

对外接口：
    dismiss_all_popups(page, stage, account) -> bool
    dismiss_with_retry(page, action_fn, stage, account) -> None
"""

import os
from playwright.async_api import Page
from utils.logger import log_node

# AI 调用计数器（每个 session 最多 3 次）
_ai_call_count = 0
_AI_MAX_CALLS = 3


def reset_ai_counter():
    """每次 session 开始时重置 AI 调用计数。"""
    global _ai_call_count
    _ai_call_count = 0


# ============================================================
# 第1层：硬编码选择器（已知弹窗）
# ============================================================

KNOWN_POPUP_SELECTORS = [
    # 文案类按钮
    "button:has-text('Start Now')",
    "button:has-text('start now')",
    "button:has-text('Got it')",
    "button:has-text('got it')",
    "button:has-text('Close')",
    "button:has-text('close')",
    "button:has-text('Continue')",
    "button:has-text('OK')",
    "button:has-text('Ok')",
    "button:has-text('Dismiss')",
    "button:has-text('Not now')",
    "button:has-text('Later')",
    "button:has-text('Skip')",
    "button:has-text('No thanks')",
    "button:has-text('Maybe later')",
    "button:has-text('继续')",
    "button:has-text('立即开始')",
    "button:has-text('知道了')",
    "button:has-text('确定')",
    "button:has-text('我知道了')",
    "button:has-text('关闭')",
    # aria / class 类
    "[aria-label='Close']",
    "[aria-label='close']",
    "[aria-label='Dismiss']",
    "[class*='close-btn']",
    "[class*='closeBtn']",
    "[class*='close-icon']",
    "[class*='modal-close']",
    "[class*='dialog-close']",
    # 结构类（最后尝试）
    "[class*='modal'] button:last-child",
    "[class*='dialog'] button:last-child",
    "[class*='popup'] button:last-child",
    "[class*='overlay'] button",
]


async def _dismiss_known_popup(page: Page, stage: str) -> bool:
    """第1层：尝试已知弹窗选择器列表。"""
    for sel in KNOWN_POPUP_SELECTORS:
        try:
            loc = page.locator(sel)
            if await loc.count() <= 0:
                continue
            if await loc.first.is_visible(timeout=500):
                await loc.first.click(timeout=3000)
                log_node("第1层：已知弹窗已关闭", level="INFO",
                         stage=stage, selector=sel[:50])
                await page.wait_for_timeout(1000)
                return True
        except Exception:
            continue
    return False


# ============================================================
# 第2层：通用 DOM overlay 检测
# ============================================================

_OVERLAY_SCAN_JS = """
() => {
    const results = [];
    const all = document.querySelectorAll('*');
    for (const el of all) {
        const style = window.getComputedStyle(el);
        const zIndex = parseInt(style.zIndex) || 0;
        const position = style.position;
        const display = style.display;
        const visibility = style.visibility;
        const opacity = parseFloat(style.opacity);
        const rect = el.getBoundingClientRect();
        
        if (zIndex > 999 
            && (position === 'fixed' || position === 'absolute')
            && display !== 'none'
            && visibility !== 'hidden'
            && opacity > 0.1
            && rect.width > 80 
            && rect.height > 80) {
            results.push({
                tag: el.tagName.toLowerCase(),
                id: el.id || '',
                className: (el.className && el.className.toString) ? el.className.toString().slice(0, 100) : '',
                zIndex: zIndex,
                rect: {x: rect.x, y: rect.y, w: rect.width, h: rect.height}
            });
        }
    }
    return results.sort((a, b) => b.zIndex - a.zIndex).slice(0, 5);
}
"""

# 在 overlay 内部寻找关闭按钮的策略（按优先级）
_CLOSE_STRATEGIES = [
    # A: aria-label/title 含 close
    "[aria-label*='close' i], [aria-label*='Close'], [title*='close' i], [title*='关闭']",
    # B: class 含 close/dismiss
    "[class*='close' i], [class*='dismiss' i], [class*='Close']",
    # C: SVG 图标按钮（常见 X 按钮）
    "button:has(svg), [role='button']:has(svg)",
    # D: 小尺寸按钮（通常是关闭按钮，而非主操作按钮）
    "button",
]


async def _dismiss_generic_overlay(page: Page, stage: str) -> bool:
    """第2层：扫描高 z-index overlay，在其内部寻找关闭按钮。"""
    try:
        overlays = await page.evaluate(_OVERLAY_SCAN_JS)
    except Exception as e:
        log_node("第2层：DOM 扫描失败", level="DEBUG", error=str(e)[:60])
        return False

    if not overlays:
        return False

    log_node("第2层：检测到 overlay", level="INFO",
             count=len(overlays),
             top_z=overlays[0]["zIndex"],
             top_class=overlays[0]["className"][:40])

    for overlay_info in overlays[:3]:
        # 构建定位这个 overlay 的选择器
        overlay_loc = _locate_overlay(page, overlay_info)
        if overlay_loc is None:
            continue

        for strategy in _CLOSE_STRATEGIES:
            try:
                close_btn = overlay_loc.locator(strategy).first
                if not await close_btn.is_visible(timeout=500):
                    continue
                # 额外检查：如果按钮文案含 "upgrade"/"subscribe"/"付费" 则不点
                btn_text = (await close_btn.inner_text(timeout=1000)).lower()
                if any(kw in btn_text for kw in ("upgrade", "subscribe", "付费", "购买", "buy")):
                    log_node("第2层：跳过付费按钮", level="DEBUG", text=btn_text[:30])
                    continue
                await close_btn.click(timeout=3000)
                await page.wait_for_timeout(1000)
                # 验证：关闭后不应跳转到登录页
                if "/login" in page.url.lower():
                    log_node("第2层：关闭后跳转到登录页，可能误操作", level="WARN")
                    return False
                log_node("第2层：通用 overlay 已关闭", level="INFO",
                         stage=stage,
                         overlay_class=overlay_info["className"][:40],
                         strategy=strategy[:30])
                return True
            except Exception:
                continue

    return False


def _locate_overlay(page: Page, info: dict):
    """根据 overlay 信息构建 Playwright locator。"""
    # 优先用 id
    if info["id"]:
        return page.locator(f"#{info['id']}")
    # 用 class 的第一个有效 token
    class_name = info["className"].strip()
    if class_name:
        first_class = class_name.split()[0]
        # 过滤掉太短或太通用的 class
        if len(first_class) > 3 and first_class not in ("el", "ant", "css"):
            return page.locator(f".{first_class}").first
    # 兜底：用 tag + z-index 范围（不太精确，但聊胜于无）
    return None


# ============================================================
# 第3层：AI 视觉兜底
# ============================================================

async def _ai_dismiss_popup(page: Page, stage: str) -> bool:
    """
    第3层：AI 视觉识别弹窗并返回关闭坐标。
    仅在第1、2层失败且操作超时后调用。
    每 session 最多调用 3 次。
    """
    global _ai_call_count
    if _ai_call_count >= _AI_MAX_CALLS:
        log_node("第3层：AI 调用次数已达上限，跳过", level="WARN")
        return False

    provider = os.getenv("ANOMALY_PROVIDER", "rule").strip().lower()
    if provider == "rule":
        # 没配 AI provider，无法使用第3层
        return False

    _ai_call_count += 1
    log_node("第3层：调用 AI 视觉识别弹窗", level="INFO",
             stage=stage, call_count=_ai_call_count)

    try:
        from browser.anomaly import check_page_anomaly
        status, reason = await check_page_anomaly(page, context_desc=f"popup_check_{stage}")
        if status == "normal":
            log_node("第3层：AI 判断页面正常，无弹窗", level="INFO")
            return False
        # AI 检测到异常但我们无法精确定位关闭按钮
        # 尝试按 Escape 键作为通用关闭手段
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1000)
        log_node("第3层：AI 检测到异常，已按 Escape 尝试关闭",
                 level="INFO", ai_status=status, ai_reason=reason)
        return True
    except Exception as e:
        log_node("第3层：AI 调用失败", level="WARN", error=str(e)[:80])
        return False


# ============================================================
# 对外主接口
# ============================================================

async def dismiss_all_popups(page: Page, stage: str = "", account: str = "") -> bool:
    """
    统一弹窗处理入口，三层递进。
    
    返回 True 表示检测到并关闭了弹窗，False 表示未检测到弹窗。
    """
    await page.wait_for_timeout(800)

    # 第1层
    if await _dismiss_known_popup(page, stage):
        return True
    # 第2层
    if await _dismiss_generic_overlay(page, stage):
        return True
    # 第3层不在这里自动触发，由 dismiss_with_retry 在超时后调用
    return False


async def dismiss_with_retry(page: Page, action_fn, stage: str = "", account: str = "", timeout: int = 8000):
    """
    执行操作，如果超时则尝试清理弹窗后重试一次。
    
    用法：
        await dismiss_with_retry(page, 
            lambda: element.click(timeout=8000),
            stage="sidebar_nav")
    
    参数：
        page: Playwright 页面对象
        action_fn: 异步可调用对象（要执行的操作）
        stage: 当前阶段描述
        account: 当前账号
        timeout: 不使用（保留接口兼容）
    """
    try:
        await action_fn()
        return
    except Exception as first_error:
        log_node("操作超时/失败，尝试清理弹窗后重试", level="WARN",
                 stage=stage, error=str(first_error)[:80])

    # 尝试三层清理
    cleared = await dismiss_all_popups(page, stage=stage, account=account)
    if not cleared:
        # 前两层没找到弹窗，尝试第3层 AI
        cleared = await _ai_dismiss_popup(page, stage=stage)
    if not cleared:
        # 最后手段：按 Escape
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)

    # 重试操作
    try:
        await action_fn()
        log_node("弹窗清理后重试成功", level="INFO", stage=stage)
    except Exception as retry_error:
        log_node("重试仍然失败", level="ERROR",
                 stage=stage, error=str(retry_error)[:80])
        raise retry_error
