#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser/anomaly.py
页面异常检测模块（多 AI 提供商版）

支持的 AI 提供商：
    rule    - 纯规则模式（无需 API，默认）
    qwen    - 阿里云千问视觉模型
    claude  - Anthropic Claude
    openai  - OpenAI GPT-4o / GPT-4o-mini
    gemini  - Google Gemini
    kimi    - Moonshot Kimi

每个提供商都支持自定义 API 地址（用于第三方转发服务）：
    QWEN_BASE_URL   / ANTHROPIC_BASE_URL / OPENAI_BASE_URL
    GEMINI_BASE_URL / KIMI_BASE_URL
    未配置时使用各提供商官方默认地址。

切换方式：.env 中设置 ANOMALY_PROVIDER=qwen（默认 rule）
容错设计：AI 调用失败时自动降级为规则模式，不阻断主流程
"""

import os
import base64
from playwright.async_api import Page
from utils.logger import log_node


# ============================================================
# 统一提示词（所有 AI 提供商共用）
# ============================================================
ANOMALY_PROMPT = """你是一个网页状态识别助手。我会给你一张网页截图，请判断当前页面状态。

请根据截图，只返回以下4种状态之一，不要返回其他任何内容：
- normal   （正常数据页面，可以正常操作）
- captcha  （出现滑块验证码、图形验证码、拼图等人机验证组件）
- blocked  （账号被封禁、风控拦截、需要重新认证、提示账号异常等）
- error    （页面空白、加载失败、404/500错误页面、网络超时）

返回格式：状态码|原因（一句话，不超过20字）
示例：normal|商品榜排行页面正常显示
示例：captcha|检测到滑块验证码组件
示例：blocked|出现账号异常提示弹窗
示例：error|页面空白，可能正在加载"""


# ============================================================
# 各提供商官方默认 API 地址
# ============================================================
DEFAULTS = {
    "qwen":   "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "claude": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "kimi":   "https://api.moonshot.cn/v1",
}


def _base_url(provider: str) -> str:
    """
    读取提供商的 API 地址
    优先使用 .env 中配置的自定义地址，未配置时使用官方默认地址

    例如使用第三方转发：
        OPENAI_BASE_URL=https://api.third-party.com/v1
    """
    env_key = f"{provider.upper()}_BASE_URL"
    url = os.getenv(env_key, "").strip().rstrip("/")
    if url:
        log_node(f"使用自定义 API 地址", level="INFO",
                 provider=provider, base_url=url)
        return url
    return DEFAULTS.get(provider, "").rstrip("/")


# ============================================================
# 纯规则模式
# ============================================================
CAPTCHA_KEYWORDS  = ["验证码", "captcha", "slider", "滑块",
                     "人机验证", "verify", "robot", "recaptcha"]
BLOCKED_KEYWORDS  = ["账号异常", "账号被封", "封禁", "banned", "风控",
                     "异常登录", "account suspended", "access denied"]
ERROR_KEYWORDS    = ["404 not found", "500 internal", "502 bad gateway",
                     "503 service", "服务器错误",
                     "page not found", "server error", "网络错误", "加载失败"]
NORMAL_KEYWORDS   = ["echotik", "rank", "products", "shop", "seller",
                     "hot promoted", "cross-border", "export",
                     "gmv", "sales", "daily", "weekly", "monthly"]
# CSS 选择器检测已禁用：Echotik 页面上存在 class 含 captcha 的非验证码元素，会误判
# 如需重新启用，请先用开发者工具确认目标元素
CAPTCHA_SELECTORS = []


async def _check_by_rule(page: Page, context_desc: str) -> tuple[str, str]:
    try:
        if "/login" in page.url.lower():
            return "blocked", "URL含登录页标志，Cookie可能失效"
        try:
            text = (await page.inner_text("body", timeout=5_000)).lower()
        except Exception:
            return "error", "页面body读取失败"
        for kw in CAPTCHA_KEYWORDS:
            if kw.lower() in text:
                return "captcha", f"页面含验证码关键词: {kw}"
        for sel in CAPTCHA_SELECTORS:
            if await page.locator(sel).count() > 0:
                return "captcha", f"发现验证码DOM元素: {sel}"
        for kw in BLOCKED_KEYWORDS:
            if kw.lower() in text:
                return "blocked", f"页面含风控关键词: {kw}"
        for kw in ERROR_KEYWORDS:
            if kw.lower() in text:
                return "error", f"页面含错误关键词: {kw}"
        for kw in NORMAL_KEYWORDS:
            if kw.lower() in text:
                return "normal", f"命中正常页面关键词: {kw}"
        return "normal", "未命中异常规则，默认正常"
    except Exception as e:
        return "normal", f"规则检测出错，已跳过: {str(e)[:50]}"


# ============================================================
# 通用工具
# ============================================================
async def _screenshot_b64(page: Page) -> str:
    return base64.standard_b64encode(
        await page.screenshot(full_page=False, type="png")
    ).decode("utf-8")


def _parse_ai_response(raw: str) -> tuple[str, str]:
    raw = raw.strip().lower()
    if "|" in raw:
        parts  = raw.split("|", 1)
        status = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else ""
    else:
        status = "normal"
        reason = raw[:60]
        for kw in ("captcha", "blocked", "error"):
            if kw in raw:
                status = kw
                break
    if status not in {"normal", "captcha", "blocked", "error"}:
        status = "normal"
    return status, reason


# ============================================================
# 提供商1：Qwen（阿里云千问）
# 官方地址：https://dashscope.aliyuncs.com/compatible-mode/v1
# 自定义地址：QWEN_BASE_URL=https://your-proxy.com/v1
# ============================================================
async def _check_by_qwen(page: Page) -> tuple[str, str]:
    import requests
    api_key  = os.getenv("QWEN_API_KEY", "").strip()
    model    = os.getenv("QWEN_MODEL", "qwen3.5-plus").strip()
    base_url = _base_url("qwen")
    if not api_key:
        raise RuntimeError("未配置 QWEN_API_KEY")
    b64 = await _screenshot_b64(page)
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": ANOMALY_PROMPT},
            ]}],
            "max_tokens": 80,
            "extra_body": {"enable_thinking": False},
        },
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ============================================================
# 提供商2：Claude（Anthropic）
# 官方地址：https://api.anthropic.com
# 自定义地址：ANTHROPIC_BASE_URL=https://your-proxy.com
# 需额外安装：pip install anthropic
# ============================================================
async def _check_by_claude(page: Page) -> tuple[str, str]:
    import anthropic
    api_key  = os.getenv("ANTHROPIC_API_KEY", "").strip()
    model    = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5").strip()
    base_url = _base_url("claude")  # anthropic SDK 支持 base_url 参数
    if not api_key:
        raise RuntimeError("未配置 ANTHROPIC_API_KEY")
    b64    = await _screenshot_b64(page)
    # anthropic SDK 支持直接传入 base_url 覆盖官方地址
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url if base_url != DEFAULTS["claude"] else None,
    )
    resp = client.messages.create(
        model=model, max_tokens=80,
        messages=[{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64",
                        "media_type": "image/png", "data": b64}},
            {"type": "text", "text": ANOMALY_PROMPT},
        ]}],
    )
    return _parse_ai_response(resp.content[0].text)


# ============================================================
# 提供商3：OpenAI（GPT-4o / GPT-4o-mini）
# 官方地址：https://api.openai.com/v1
# 自定义地址：OPENAI_BASE_URL=https://your-proxy.com/v1
# ============================================================
async def _check_by_openai(page: Page) -> tuple[str, str]:
    import requests
    api_key  = os.getenv("OPENAI_API_KEY", "").strip()
    model    = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    base_url = _base_url("openai")
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")
    b64  = await _screenshot_b64(page)
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}",
                               "detail": "low"}},
                {"type": "text", "text": ANOMALY_PROMPT},
            ]}],
            "max_tokens": 80,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ============================================================
# 提供商4：Gemini（Google）
# 官方地址：https://generativelanguage.googleapis.com/v1beta
# 自定义地址：GEMINI_BASE_URL=https://your-proxy.com/v1beta
# 注意：Gemini 的 URL 格式与其他提供商不同（Key 在 query string 中）
# 如果第三方代理兼容 OpenAI 格式，建议改用 openai provider 对接
# ============================================================
async def _check_by_gemini(page: Page) -> tuple[str, str]:
    import requests
    api_key  = os.getenv("GEMINI_API_KEY", "").strip()
    model    = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
    base_url = _base_url("gemini")
    if not api_key:
        raise RuntimeError("未配置 GEMINI_API_KEY")
    b64  = await _screenshot_b64(page)
    resp = requests.post(
        f"{base_url}/models/{model}:generateContent?key={api_key}",
        json={
            "contents": [{"parts": [
                {"inline_data": {"mime_type": "image/png", "data": b64}},
                {"text": ANOMALY_PROMPT},
            ]}],
            "generationConfig": {"maxOutputTokens": 80},
        },
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_ai_response(
        resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    )


# ============================================================
# 提供商5：Kimi（Moonshot AI，OpenAI 兼容格式）
# 官方地址：https://api.moonshot.cn/v1
# 自定义地址：KIMI_BASE_URL=https://your-proxy.com/v1
# ============================================================
async def _check_by_kimi(page: Page) -> tuple[str, str]:
    import requests
    api_key  = os.getenv("KIMI_API_KEY", "").strip()
    model    = os.getenv("KIMI_MODEL", "moonshot-v1-8k-vision").strip()
    base_url = _base_url("kimi")
    if not api_key:
        raise RuntimeError("未配置 KIMI_API_KEY")
    b64  = await _screenshot_b64(page)
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": ANOMALY_PROMPT},
            ]}],
            "max_tokens": 80,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ============================================================
# 提供商路由表
# ============================================================
_PROVIDER_MAP = {
    "rule":   None,
    "qwen":   _check_by_qwen,
    "claude": _check_by_claude,
    "openai": _check_by_openai,
    "gemini": _check_by_gemini,
    "kimi":   _check_by_kimi,
}


# ============================================================
# 对外主接口
# ============================================================
async def check_page_anomaly(
    page: Page,
    context_desc: str = "",
) -> tuple[str, str]:
    """
    检测当前页面状态，返回 (status, reason)
    通过 .env 中的 ANOMALY_PROVIDER 选择检测方式（默认 rule）
    AI 调用失败时自动降级为规则模式，不阻断主流程
    """
    provider = os.getenv("ANOMALY_PROVIDER", "rule").strip().lower()
    log_node("开始页面异常检测", level="INFO",
             provider=provider,
             context=context_desc or "未指定",
             url=page.url[:60])

    if provider == "rule":
        status, reason = await _check_by_rule(page, context_desc)
        _log_result(status, reason, context_desc, provider)
        return status, reason

    check_fn = _PROVIDER_MAP.get(provider)
    if check_fn is None:
        log_node(f"未知的 ANOMALY_PROVIDER={provider}，降级为规则模式",
                 level="WARN", valid_options=list(_PROVIDER_MAP.keys()))
        status, reason = await _check_by_rule(page, context_desc)
        _log_result(status, reason, context_desc, "rule（降级）")
        return status, reason

    try:
        status, reason = await check_fn(page)
        _log_result(status, reason, context_desc, provider)
        return status, reason
    except RuntimeError as e:
        log_node("AI检测配置错误，降级为规则模式", level="WARN",
                 provider=provider, error=str(e))
    except Exception as e:
        log_node("AI检测调用失败，降级为规则模式", level="WARN",
                 provider=provider, error=str(e)[:100])

    status, reason = await _check_by_rule(page, context_desc)
    _log_result(status, reason, context_desc, "rule（AI降级）")
    return status, reason


def _log_result(status: str, reason: str, context: str, provider: str):
    level = "INFO" if status == "normal" else "WARN"
    log_node(f"页面检测结果: {status}", level=level,
             reason=reason, context=context, provider=provider)