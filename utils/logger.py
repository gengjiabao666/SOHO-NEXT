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

# 日志级别与 emoji 图标的映射表
# 每个级别对应一个直观的 emoji，方便在终端中快速识别日志类型
_ICONS = {
    "START": "🚀",    # 启动
    "INFO":  "✅",    # 正常信息
    "WARN":  "⚠️ ",   # 警告
    "ERROR": "❌",    # 错误
    "WAIT":  "⏳",    # 等待中
    "DONE":  "🎉",    # 完成
    "SKIP":  "⏭️ ",   # 跳过
    "DEBUG": "🔧",    # 调试
}

# 全局标志位，确保日志系统只初始化一次，避免重复添加 handler
_logger_initialized = False


def setup_logger(captured: str = ""):
    """
    初始化日志系统（每次运行调用一次）

    参数：
        captured: 采集日期字符串（如 "2026-03-03"），用于日志文件命名。
                  若为空则使用 loguru 的日期占位符自动按天命名。

    说明：
        - 移除 loguru 默认的 handler，重新配置终端和文件双输出
        - 终端输出带颜色高亮，文件输出为纯文本
        - 日志文件按天轮转，最多保留 30 天
    """
    # 使用全局变量控制只初始化一次
    global _logger_initialized
    if _logger_initialized:
        return
    _logger_initialized = True

    # 确保 logs 目录存在
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # 移除 loguru 默认的 stderr handler，避免重复输出
    logger.remove()

    # 终端输出：带绿色时间戳，显示所有 DEBUG 及以上级别，启用颜色
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | {message}",
        level="DEBUG",
        colorize=True,
    )

    # 文件输出：按天轮转，保留30天历史日志，UTF-8 编码
    # 如果传入了 captured 日期，则日志文件名包含该日期；否则使用 loguru 日期占位符
    log_file = log_dir / f"echotik_{captured or '{time:YYYY-MM-DD}'}.log"
    logger.add(
        str(log_file),
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        level="DEBUG",
        rotation="00:00",       # 每天午夜轮转
        retention="30 days",    # 保留最近 30 天的日志文件
        encoding="utf-8",
    )


def log_node(message: str, level: str = "INFO", **kwargs):
    """
    输出一条结构化日志节点

    参数：
        message: 日志主体消息文本
        level:   日志级别，支持 START/INFO/WARN/ERROR/WAIT/DONE/SKIP/DEBUG
        **kwargs: 附加的键值对信息，会以 "key=value" 格式追加到消息末尾

    示例输出:
        🚀 Echotik 自动采集器启动  captured=2026-03-03
        ✅ 下载完成  module=商品榜  win=d  size=128.3KB
    """
    # 根据级别获取对应的 emoji 图标，未匹配则使用空格占位
    icon    = _ICONS.get(level.upper(), "  ")
    # 将所有附加键值对拼接为 "k1=v1  k2=v2" 格式的字符串
    kv      = "  ".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    # 组装完整日志消息：图标 + 消息 + 键值对
    full    = f"{icon} {message}"
    if kv:
        full += f"  {kv}"

    # 将自定义级别映射到 loguru 支持的标准级别
    # START/DONE/SKIP/WAIT 都映射为 INFO 级别
    log_level = level.upper()
    if log_level in ("START", "DONE", "SKIP", "WAIT"):
        log_level = "INFO"

    # 根据映射后的级别调用对应的 loguru 方法输出日志
    if log_level == "DEBUG":
        logger.debug(full)
    elif log_level == "WARN":
        logger.warning(full)
    elif log_level == "ERROR":
        logger.error(full)
    else:
        # 默认使用 info 级别（包括 START/DONE/SKIP/WAIT 映射后的情况）
        logger.info(full)
