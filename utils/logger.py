#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/logger.py
统一日志系统

功能：
    - emoji 图标标记不同级别
    - loguru 自动按天轮转，保留30天
    - 终端 + 文件双输出
    - 结构化键值对格式
"""

import sys
from pathlib import Path
from loguru import logger

_ICONS = {
    "START": "🚀",
    "INFO":  "✅",
    "WARN":  "⚠️ ",
    "ERROR": "❌",
    "WAIT":  "⏳",
    "DONE":  "🎉",
    "SKIP":  "⏭️ ",
    "DEBUG": "🔧",
}

_logger_initialized = False


def setup_logger(captured: str = ""):
    """初始化日志系统（每次运行调用一次）"""
    global _logger_initialized
    if _logger_initialized:
        return
    _logger_initialized = True

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    logger.remove()

    # 终端输出
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | {message}",
        level="DEBUG",
        colorize=True,
    )

    # 文件输出（按天轮转，保留30天）
    log_file = log_dir / f"echotik_{captured or '{time:YYYY-MM-DD}'}.log"
    logger.add(
        str(log_file),
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
    )


def log_node(message: str, level: str = "INFO", **kwargs):
    """
    输出一条结构化日志节点

    示例输出:
        🚀 Echotik 自动采集器启动  captured=2026-03-03
        ✅ 下载完成  module=商品榜  win=d  size=128.3KB
    """
    icon    = _ICONS.get(level.upper(), "  ")
    kv      = "  ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    full    = f"{icon} {message}"
    if kv:
        full += f"  {kv}"

    log_level = level.upper()
    if log_level in ("START", "DONE", "SKIP", "WAIT"):
        log_level = "INFO"

    if log_level == "DEBUG":
        logger.debug(full)
    elif log_level == "WARN":
        logger.warning(full)
    elif log_level == "ERROR":
        logger.error(full)
    else:
        logger.info(full)
