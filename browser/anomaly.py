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

# 标准库：操作系统接口（读取环境变量）
import os
# 标准库：Base64 编解码（用于将截图二进制数据转为字符串，方便传给 AI API）
import base64
# Playwright 异步 API 中的 Page 对象，代表一个浏览器页面标签
from playwright.async_api import Page
# 项目自定义的结构化日志工具，支持带上下文字段的日志输出
from utils.logger import log_node


# ============================================================
# 统一提示词（所有 AI 提供商共用）
# 这段提示词会随截图一起发送给各 AI 视觉模型，
# 要求 AI 只返回 4 种状态之一，格式为 "状态码|原因"
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
# 当用户未在 .env 中配置自定义地址时，使用这些默认值
# ============================================================
DEFAULTS = {
    "qwen":   "https://dashscope.aliyuncs.com/compatible-mode/v1",   # 阿里云千问 API
    "claude": "https://api.anthropic.com",                            # Anthropic Claude API
    "openai": "https://api.openai.com/v1",                            # OpenAI API
    "gemini": "https://generativelanguage.googleapis.com/v1beta",     # Google Gemini API
    "kimi":   "https://api.moonshot.cn/v1",                           # Moonshot Kimi API
}


def _base_url(provider: str) -> str:
    """
    读取提供商的 API 地址
    优先使用 .env 中配置的自定义地址，未配置时使用官方默认地址

    例如使用第三方转发：
        OPENAI_BASE_URL=https://api.third-party.com/v1
    """
    # 根据提供商名称拼接环境变量名，如 provider="openai" -> "OPENAI_BASE_URL"
    env_key = f"{provider.upper()}_BASE_URL"
    # 从环境变量读取自定义地址，去除首尾空白和末尾斜杠
    url = os.getenv(env_key, "").strip().rstrip("/")
    # 如果用户配置了自定义地址，记录日志并返回
    if url:
        log_node(f"使用自定义 API 地址", level="INFO",
                 provider=provider, base_url=url)
        return url
    # 未配置自定义地址，返回 DEFAULTS 字典中的官方默认地址
    return DEFAULTS.get(provider, "").rstrip("/")


# ============================================================
# 纯规则模式 —— 基于关键词匹配的页面状态检测
# 不依赖任何 AI API，通过扫描页面文本中的关键词来判断状态
# ============================================================

# 验证码相关关键词列表：命中任意一个则判定为 captcha 状态
CAPTCHA_KEYWORDS  = ["验证码", "captcha", "slider", "滑块",
                     "人机验证", "verify", "robot", "recaptcha"]
# 风控/封禁相关关键词列表：命中任意一个则判定为 blocked 状态
BLOCKED_KEYWORDS  = ["账号异常", "账号被封", "封禁", "banned", "风控",
                     "异常登录", "account suspended", "access denied"]
# 页面错误相关关键词列表：命中任意一个则判定为 error 状态
ERROR_KEYWORDS    = ["404 not found", "500 internal", "502 bad gateway",
                     "503 service", "服务器错误",
                     "page not found", "server error", "网络错误", "加载失败"]
# 正常页面关键词列表：命中任意一个则判定为 normal 状态（Echotik 业务页面特征词）
NORMAL_KEYWORDS   = ["echotik", "rank", "products", "shop", "seller",
                     "hot promoted", "cross-border", "export",
                     "gmv", "sales", "daily", "weekly", "monthly"]
# CSS 选择器检测已禁用：Echotik 页面上存在 class 含 captcha 的非验证码元素，会误判
# 如需重新启用，请先用开发者工具确认目标元素
CAPTCHA_SELECTORS = []


async def _check_by_rule(page: Page, context_desc: str) -> tuple[str, str]:
    """
    纯规则模式的页面异常检测函数
    按优先级依次检查：URL是否为登录页 -> 验证码关键词 -> 验证码DOM元素
    -> 风控关键词 -> 错误关键词 -> 正常页面关键词
    全部未命中则默认返回 normal

    参数:
        page: Playwright 页面对象
        context_desc: 调用方传入的上下文描述（如"商品榜单页"），用于日志

    返回:
        (status, reason) 元组，status 为 normal/captcha/blocked/error 之一
    """
    try:
        # 首先检查 URL 是否包含 /login，如果是则说明被重定向到登录页，Cookie 可能失效
        if "/login" in page.url.lower():
            return "blocked", "URL含登录页标志，Cookie可能失效"
        try:
            # 获取页面 body 的全部文本内容，转为小写用于关键词匹配
            text = (await page.inner_text("body", timeout=5_000)).lower()
        except Exception:
            # 如果连 body 文本都读取不到，说明页面可能完全空白或崩溃
            return "error", "页面body读取失败"

        # 按优先级逐一检查各类关键词列表
        # 优先级：验证码 > 风控封禁 > 页面错误 > 正常页面

        # 检查验证码关键词
        for kw in CAPTCHA_KEYWORDS:
            if kw.lower() in text:
                return "captcha", f"页面含验证码关键词: {kw}"
        # 检查验证码 CSS 选择器（当前已禁用，CAPTCHA_SELECTORS 为空列表）
        for sel in CAPTCHA_SELECTORS:
            if await page.locator(sel).count() > 0:
                return "captcha", f"发现验证码DOM元素: {sel}"
        # 检查风控/封禁关键词
        for kw in BLOCKED_KEYWORDS:
            if kw.lower() in text:
                return "blocked", f"页面含风控关键词: {kw}"
        # 检查页面错误关键词
        for kw in ERROR_KEYWORDS:
            if kw.lower() in text:
                return "error", f"页面含错误关键词: {kw}"
        # 检查正常页面关键词（命中说明页面内容正常加载）
        for kw in NORMAL_KEYWORDS:
            if kw.lower() in text:
                return "normal", f"命中正常页面关键词: {kw}"

        # 所有关键词都未命中，默认认为页面正常
        return "normal", "未命中异常规则，默认正常"
    except Exception as e:
        # 整个检测过程出错时，不阻断主流程，默认返回正常
        return "normal", f"规则检测出错，已跳过: {str(e)[:50]}"


# ============================================================
# 通用工具函数
# ============================================================
async def _screenshot_b64(page: Page) -> str:
    """
    对当前页面进行截图，并将截图转为 Base64 编码字符串
    所有 AI 提供商都需要将截图以 Base64 格式传入 API

    参数:
        page: Playwright 页面对象

    返回:
        PNG 截图的 Base64 编码字符串
    """
    # 截取当前可视区域（非全页），输出 PNG 格式，然后用标准 Base64 编码
    return base64.standard_b64encode(
        await page.screenshot(full_page=False, type="png")
    ).decode("utf-8")


def _parse_ai_response(raw: str) -> tuple[str, str]:
    """
    解析 AI 模型返回的原始文本，提取状态码和原因

    AI 应返回格式为 "状态码|原因" 的文本，例如 "captcha|检测到滑块验证码"
    如果格式不符合预期，会尝试从文本中提取关键词来推断状态

    参数:
        raw: AI 模型返回的原始文本

    返回:
        (status, reason) 元组
    """
    # 去除首尾空白并转小写，统一处理
    raw = raw.strip().lower()
    if "|" in raw:
        # 标准格式：按 "|" 分割，左边是状态码，右边是原因
        parts  = raw.split("|", 1)
        status = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else ""
    else:
        # 非标准格式：AI 没有按要求返回，尝试从文本中匹配关键词
        status = "normal"
        reason = raw[:60]  # 截取前60个字符作为原因
        for kw in ("captcha", "blocked", "error"):
            if kw in raw:
                status = kw
                break
    # 最终校验：如果状态码不在合法范围内，强制设为 normal
    if status not in {"normal", "captcha", "blocked", "error"}:
        status = "normal"
    return status, reason


# ============================================================
# 提供商1：Qwen（阿里云千问）
# 官方地址：https://dashscope.aliyuncs.com/compatible-mode/v1
# 自定义地址：QWEN_BASE_URL=https://your-proxy.com/v1
# 使用 OpenAI 兼容格式的 /chat/completions 接口
# ============================================================
async def _check_by_qwen(page: Page) -> tuple[str, str]:
    """
    使用阿里云千问视觉模型检测页面异常

    通过 OpenAI 兼容格式的 HTTP API 发送截图和提示词，
    获取 AI 对页面状态的判断结果

    环境变量:
        QWEN_API_KEY: 千问 API 密钥（必填）
        QWEN_MODEL: 模型名称（默认 qwen3.5-plus）
        QWEN_BASE_URL: 自定义 API 地址（可选）

    返回:
        (status, reason) 元组
    """
    # 延迟导入 requests，仅在实际调用时才加载
    import requests
    # 从环境变量读取 API 密钥和模型名称
    api_key  = os.getenv("QWEN_API_KEY", "").strip()
    model    = os.getenv("QWEN_MODEL", "qwen3.5-plus").strip()
    # 获取 API 基础地址（优先自定义，否则用默认）
    base_url = _base_url("qwen")
    # API 密钥未配置时抛出异常，由上层 check_page_anomaly 捕获并降级
    if not api_key:
        raise RuntimeError("未配置 QWEN_API_KEY")
    # 截图并转为 Base64 编码
    b64 = await _screenshot_b64(page)
    # 发送 POST 请求到千问 API（OpenAI 兼容格式）
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                # 以 data URI 格式传入截图
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                # 传入统一提示词
                {"type": "text", "text": ANOMALY_PROMPT},
            ]}],
            "max_tokens": 80,  # 限制返回长度，只需要简短的状态判断
            "extra_body": {"enable_thinking": False},  # 关闭千问的思考模式，加快响应
        },
        timeout=20,  # 20秒超时
    )
    # 检查 HTTP 响应状态码，非 2xx 会抛出异常
    resp.raise_for_status()
    # 从响应 JSON 中提取 AI 回复文本，解析为 (status, reason)
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ============================================================
# 提供商2：Claude（Anthropic）
# 官方地址：https://api.anthropic.com
# 自定义地址：ANTHROPIC_BASE_URL=https://your-proxy.com
# 需额外安装：pip install anthropic
# 使用 Anthropic 官方 Python SDK，支持 base_url 参数覆盖地址
# ============================================================
async def _check_by_claude(page: Page) -> tuple[str, str]:
    """
    使用 Anthropic Claude 视觉模型检测页面异常

    通过 anthropic 官方 SDK 发送截图和提示词，
    SDK 原生支持 base_url 参数，方便对接第三方代理

    环境变量:
        ANTHROPIC_API_KEY: Claude API 密钥（必填）
        CLAUDE_MODEL: 模型名称（默认 claude-sonnet-4-5）
        ANTHROPIC_BASE_URL: 自定义 API 地址（可选）

    返回:
        (status, reason) 元组
    """
    # 延迟导入 anthropic SDK
    import anthropic
    # 从环境变量读取 API 密钥和模型名称
    api_key  = os.getenv("ANTHROPIC_API_KEY", "").strip()
    model    = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5").strip()
    # 获取 API 基础地址（anthropic SDK 支持 base_url 参数）
    base_url = _base_url("claude")
    if not api_key:
        raise RuntimeError("未配置 ANTHROPIC_API_KEY")
    # 截图并转为 Base64 编码
    b64    = await _screenshot_b64(page)
    # 创建 Anthropic 客户端实例
    # 如果使用的是官方默认地址，则不传 base_url（让 SDK 使用内置默认值）
    # 如果是自定义地址，则传入 base_url 覆盖
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url if base_url != DEFAULTS["claude"] else None,
    )
    # 调用 Claude Messages API，发送截图和提示词
    resp = client.messages.create(
        model=model, max_tokens=80,  # 限制返回长度
        messages=[{"role": "user", "content": [
            # Claude 使用 "image" 类型 + base64 source 格式传图
            {"type": "image",
             "source": {"type": "base64",
                        "media_type": "image/png", "data": b64}},
            {"type": "text", "text": ANOMALY_PROMPT},
        ]}],
    )
    # 从响应中提取第一个文本块的内容，解析为 (status, reason)
    return _parse_ai_response(resp.content[0].text)


# ============================================================
# 提供商3：OpenAI（GPT-4o / GPT-4o-mini）
# 官方地址：https://api.openai.com/v1
# 自定义地址：OPENAI_BASE_URL=https://your-proxy.com/v1
# 使用标准 OpenAI Chat Completions API 格式
# ============================================================
async def _check_by_openai(page: Page) -> tuple[str, str]:
    """
    使用 OpenAI GPT-4o 视觉模型检测页面异常

    通过标准 OpenAI /chat/completions 接口发送截图和提示词

    环境变量:
        OPENAI_API_KEY: OpenAI API 密钥（必填）
        OPENAI_MODEL: 模型名称（默认 gpt-4o-mini）
        OPENAI_BASE_URL: 自定义 API 地址（可选）

    返回:
        (status, reason) 元组
    """
    import requests
    # 从环境变量读取 API 密钥和模型名称
    api_key  = os.getenv("OPENAI_API_KEY", "").strip()
    model    = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    base_url = _base_url("openai")
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")
    # 截图并转为 Base64 编码
    b64  = await _screenshot_b64(page)
    # 发送 POST 请求到 OpenAI API
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                # OpenAI 使用 image_url 类型 + data URI 格式传图
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}",
                               "detail": "low"}},  # low 精度足够判断页面状态，节省 token
                {"type": "text", "text": ANOMALY_PROMPT},
            ]}],
            "max_tokens": 80,
        },
        timeout=20,
    )
    resp.raise_for_status()
    # 解析 OpenAI 标准响应格式
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ============================================================
# 提供商4：Gemini（Google）
# 官方地址：https://generativelanguage.googleapis.com/v1beta
# 自定义地址：GEMINI_BASE_URL=https://your-proxy.com/v1beta
# 注意：Gemini 的 URL 格式与其他提供商不同（Key 在 query string 中）
# 如果第三方代理兼容 OpenAI 格式，建议改用 openai provider 对接
# ============================================================
async def _check_by_gemini(page: Page) -> tuple[str, str]:
    """
    使用 Google Gemini 视觉模型检测页面异常

    Gemini API 格式与 OpenAI 不同：
    - API Key 放在 URL query string 中（而非 Header）
    - 请求体使用 contents/parts 结构（而非 messages/content）
    - 图片使用 inline_data 格式（而非 image_url）

    环境变量:
        GEMINI_API_KEY: Gemini API 密钥（必填）
        GEMINI_MODEL: 模型名称（默认 gemini-1.5-flash）
        GEMINI_BASE_URL: 自定义 API 地址（可选）

    返回:
        (status, reason) 元组
    """
    import requests
    # 从环境变量读取 API 密钥和模型名称
    api_key  = os.getenv("GEMINI_API_KEY", "").strip()
    model    = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
    base_url = _base_url("gemini")
    if not api_key:
        raise RuntimeError("未配置 GEMINI_API_KEY")
    # 截图并转为 Base64 编码
    b64  = await _screenshot_b64(page)
    # 发送 POST 请求到 Gemini API（注意 URL 格式：模型名在路径中，Key 在 query string 中）
    resp = requests.post(
        f"{base_url}/models/{model}:generateContent?key={api_key}",
        json={
            # Gemini 使用 contents -> parts 结构
            "contents": [{"parts": [
                # 图片使用 inline_data 格式，直接传 Base64 数据
                {"inline_data": {"mime_type": "image/png", "data": b64}},
                # 文本提示词
                {"text": ANOMALY_PROMPT},
            ]}],
            # Gemini 的生成配置参数名为 generationConfig
            "generationConfig": {"maxOutputTokens": 80},
        },
        timeout=20,
    )
    resp.raise_for_status()
    # Gemini 响应格式：candidates[0].content.parts[0].text
    return _parse_ai_response(
        resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    )


# ============================================================
# 提供商5：Kimi（Moonshot AI，OpenAI 兼容格式）
# 官方地址：https://api.moonshot.cn/v1
# 自定义地址：KIMI_BASE_URL=https://your-proxy.com/v1
# Kimi 的 API 格式与 OpenAI 完全兼容，使用相同的请求结构
# ============================================================
async def _check_by_kimi(page: Page) -> tuple[str, str]:
    """
    使用 Moonshot Kimi 视觉模型检测页面异常

    Kimi API 兼容 OpenAI 格式，请求结构与 _check_by_openai 基本一致

    环境变量:
        KIMI_API_KEY: Kimi API 密钥（必填）
        KIMI_MODEL: 模型名称（默认 moonshot-v1-8k-vision）
        KIMI_BASE_URL: 自定义 API 地址（可选）

    返回:
        (status, reason) 元组
    """
    import requests
    # 从环境变量读取 API 密钥和模型名称
    api_key  = os.getenv("KIMI_API_KEY", "").strip()
    model    = os.getenv("KIMI_MODEL", "moonshot-v1-8k-vision").strip()
    base_url = _base_url("kimi")
    if not api_key:
        raise RuntimeError("未配置 KIMI_API_KEY")
    # 截图并转为 Base64 编码
    b64  = await _screenshot_b64(page)
    # 发送 POST 请求到 Kimi API（OpenAI 兼容格式）
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": [
                # 与 OpenAI 相同的 image_url 格式
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": ANOMALY_PROMPT},
            ]}],
            "max_tokens": 80,
        },
        timeout=20,
    )
    resp.raise_for_status()
    # 解析 OpenAI 兼容格式的响应
    return _parse_ai_response(resp.json()["choices"][0]["message"]["content"])


# ============================================================
# 提供商路由表
# 将提供商名称映射到对应的检测函数
# "rule" 映射为 None，表示使用纯规则模式（不走 AI）
# ============================================================
_PROVIDER_MAP = {
    "rule":   None,              # 纯规则模式，无需 AI API
    "qwen":   _check_by_qwen,   # 阿里云千问
    "claude": _check_by_claude,  # Anthropic Claude
    "openai": _check_by_openai,  # OpenAI GPT-4o
    "gemini": _check_by_gemini,  # Google Gemini
    "kimi":   _check_by_kimi,    # Moonshot Kimi
}


# ============================================================
# 对外主接口 —— 其他模块统一调用此函数进行页面异常检测
# ============================================================
async def check_page_anomaly(
    page: Page,
    context_desc: str = "",
) -> tuple[str, str]:
    """
    检测当前页面状态，返回 (status, reason)

    这是本模块的唯一对外接口，其他模块通过调用此函数来检测页面是否异常。
    通过 .env 中的 ANOMALY_PROVIDER 选择检测方式（默认 rule）
    AI 调用失败时自动降级为规则模式，不阻断主流程

    参数:
        page: Playwright 页面对象
        context_desc: 调用上下文描述（如"商品榜单页"），用于日志记录

    返回:
        (status, reason) 元组
        status: "normal" | "captcha" | "blocked" | "error"
        reason: 一句话描述原因
    """
    # 从环境变量读取当前使用的检测提供商，默认为纯规则模式
    provider = os.getenv("ANOMALY_PROVIDER", "rule").strip().lower()
    # 记录检测开始日志，包含提供商、上下文和当前 URL
    log_node("开始页面异常检测", level="INFO",
             provider=provider,
             context=context_desc or "未指定",
             url=page.url[:60])

    # 如果是纯规则模式，直接调用规则检测函数
    if provider == "rule":
        status, reason = await _check_by_rule(page, context_desc)
        _log_result(status, reason, context_desc, provider)
        return status, reason

    # 从路由表中查找对应的 AI 检测函数
    check_fn = _PROVIDER_MAP.get(provider)
    # 如果提供商名称不在路由表中，警告并降级为规则模式
    if check_fn is None:
        log_node(f"未知的 ANOMALY_PROVIDER={provider}，降级为规则模式",
                 level="WARN", valid_options=list(_PROVIDER_MAP.keys()))
        status, reason = await _check_by_rule(page, context_desc)
        _log_result(status, reason, context_desc, "rule（降级）")
        return status, reason

    # 尝试调用 AI 检测函数
    try:
        status, reason = await check_fn(page)
        _log_result(status, reason, context_desc, provider)
        return status, reason
    except RuntimeError as e:
        # RuntimeError 通常是配置错误（如未设置 API Key），记录警告
        log_node("AI检测配置错误，降级为规则模式", level="WARN",
                 provider=provider, error=str(e))
    except Exception as e:
        # 其他异常（如网络超时、API 返回错误等），记录警告
        log_node("AI检测调用失败，降级为规则模式", level="WARN",
                 provider=provider, error=str(e)[:100])

    # AI 检测失败后，降级为规则模式兜底，确保不阻断主流程
    status, reason = await _check_by_rule(page, context_desc)
    _log_result(status, reason, context_desc, "rule（AI降级）")
    return status, reason


def _log_result(status: str, reason: str, context: str, provider: str):
    """
    记录页面检测结果日志

    正常状态用 INFO 级别，异常状态用 WARN 级别，便于日志过滤和告警

    参数:
        status: 检测状态码（normal/captcha/blocked/error）
        reason: 原因描述
        context: 调用上下文描述
        provider: 使用的检测提供商名称
    """
    # 根据状态决定日志级别：正常为 INFO，异常为 WARN
    level = "INFO" if status == "normal" else "WARN"
    log_node(f"页面检测结果: {status}", level=level,
             reason=reason, context=context, provider=provider)