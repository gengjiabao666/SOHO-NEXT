#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/events.py
结构化事件日志模块，供 maintenance agent 读取诊断。

与 utils/logger.py 并行运行，不替代现有日志。
每次采集运行产生一个 logs/events_YYYYMMDD.json 文件。
"""

import json
import uuid
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")
SESSION_FILE = LOG_DIR / ".current_session"


def init_session(captured: str = "") -> str:
    """
    采集器启动时调用一次，生成并持久化 session_id。
    session_id 格式：YYYYMMDD_HHMMSS
    返回 session_id 供调用方使用（可选）。
    在 main.py 的 setup_logger 之后调用。
    """
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"{captured or datetime.now().strftime('%Y%m%d')}_{ts}"
    SESSION_FILE.write_text(session_id)
    return session_id


def _get_session_id() -> str:
    try:
        return SESSION_FILE.read_text().strip()
    except Exception:
        return datetime.now().strftime("%Y%m%d_%H%M%S")


def write_event(
    stage: str,
    result: str,
    context: dict = None,
    detail: str = "",
    screenshot: str = "",
    duration_ms: int = None,
):
    """
    写入一条结构化事件到 logs/events_YYYYMMDD.json。

    参数：
        stage      : 执行阶段名称（见下方常量）
        result     : SUCCESS / FAILED / TIMEOUT / SKIPPED / WARN
        context    : 当前执行上下文，如 {"module": "商品榜", "win": "d", "category": "Pet Supplies"}
        detail     : 错误或补充描述，失败时必填
        screenshot : 截图文件路径（如存在）
        duration_ms: 该阶段耗时（毫秒），可选
    """
    LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    path = LOG_DIR / f"events_{today}.json"

    event = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now().isoformat(),
        "session_id": _get_session_id(),
        "stage": stage,
        "result": result,
        "context": context or {},
        "detail": detail,
        "screenshot": screenshot,
        "duration_ms": duration_ms,
    }

    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except Exception:
        data = []

    data.append(event)

    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        # 写入失败不能阻断主流程
        print(f"[events] 写入失败: {e}")


# ── 阶段常量（维护 agent 的 AGENTS.md 与此保持一致）──
STAGE_SESSION_START      = "session_start"
STAGE_PROXY_SETUP        = "proxy_setup"
STAGE_BROWSER_LAUNCH     = "browser_launch"
STAGE_LOGIN              = "login"
STAGE_POPUP_DISMISS      = "popup_dismiss"
STAGE_SIDEBAR_PARENT     = "sidebar_nav_parent"
STAGE_SIDEBAR_CHILD      = "sidebar_nav_child"
STAGE_DATA_WAIT          = "data_wait"
STAGE_ANOMALY_CHECK      = "anomaly_check"
STAGE_CATEGORY_SELECT    = "category_select"
STAGE_TAB_CLICK          = "tab_click"
STAGE_DROPDOWN_HOVER     = "dropdown_hover"
STAGE_COUNT_SELECT       = "count_select"
STAGE_EXPORT_TRIGGER     = "export_trigger"
STAGE_DOWNLOAD_CAPTURE   = "download_capture"
STAGE_FRESHNESS_CHECK    = "freshness_check"
STAGE_PIPELINE_TRIGGER   = "pipeline_trigger"
STAGE_SESSION_END        = "session_end"
