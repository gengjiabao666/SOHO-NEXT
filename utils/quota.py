#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/quota.py
导出配额管理模块

功能：
    - 记录每次导出的条数
    - 统计当日已导出总条数
    - 超过阈值时发送警报
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from utils.logger import log_node
from utils.notifier import notify_quota_warning

# 配置文件目录路径（项目根目录下的 config/）
CONFIG_DIR = Path(__file__).parent.parent / "config"
# 配额记录文件路径，以 JSON 格式存储每日导出记录
QUOTA_FILE = CONFIG_DIR / "export_quota.json"

# 默认导出条数警报阈值（Echotik 每日限额 2000 条，提前在 1600 条时预警）
DEFAULT_THRESHOLD = 1600


def _load_quota_data() -> dict:
    """
    加载配额数据

    返回：
        配额数据字典，结构为 { "YYYY-MM-DD": { "total": int, "records": [...], "warned": bool } }
        文件不存在或读取失败时返回空字典

    说明：
        配额数据按日期分组，每天独立记录导出条数和明细
    """
    # 文件不存在时返回空字典（首次运行）
    if not QUOTA_FILE.exists():
        return {}
    try:
        # 以 UTF-8 编码读取 JSON 文件
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        # 读取失败（文件损坏等），记录警告并返回空字典，不影响主流程
        log_node("配额文件读取失败", level="WARN", error=str(e)[:60])
        return {}


def _save_quota_data(data: dict):
    """
    保存配额数据到 JSON 文件

    参数：
        data: 配额数据字典

    说明：
        自动创建 config 目录（如不存在），写入失败时仅记录警告
    """
    # 确保 config 目录存在
    CONFIG_DIR.mkdir(exist_ok=True)
    try:
        # 以 UTF-8 编码写入 JSON，保留中文字符，缩进 2 空格便于阅读
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        # 写入失败仅记录警告，不中断主流程
        log_node("配额文件保存失败", level="WARN", error=str(e)[:60])


def record_export(task: str, count: int):
    """
    记录单次导出

    参数：
        task: 任务名称（如 "商品榜_d"、"小店榜_w"）
        count: 本次导出的条数

    说明：
        - 将本次导出追加到当日记录中
        - 累加当日总导出条数
        - 记录导出时间、任务名和条数，便于事后审计
    """
    # 获取今天的日期（ISO 格式，如 "2026-03-19"）
    today = date.today().isoformat()
    # 获取当前时间（仅时分秒，如 "14:30:05"）
    now = datetime.now().strftime("%H:%M:%S")

    # 加载现有配额数据
    data = _load_quota_data()

    # 如果今天还没有记录，初始化当日数据结构
    if today not in data:
        data[today] = {"total": 0, "records": [], "warned": False}

    # 累加当日总导出条数
    data[today]["total"] += count
    # 追加本次导出的详细记录（时间、任务名、条数）
    data[today]["records"].append({
        "time": now,
        "task": task,
        "count": count
    })

    # 保存更新后的配额数据
    _save_quota_data(data)

    # 记录日志，显示本次导出信息和当日累计总数
    log_node("导出条数已记录", level="INFO",
             task=task, count=count, today_total=data[today]["total"])


def get_today_total() -> int:
    """
    获取今日已导出总条数

    返回：
        今日累计导出条数，无记录时返回 0
    """
    # 获取今天的日期
    today = date.today().isoformat()
    # 加载配额数据并提取今日总数
    data = _load_quota_data()
    return data.get(today, {}).get("total", 0)


def check_quota_warning(threshold: int = DEFAULT_THRESHOLD) -> bool:
    """
    检查是否超过阈值，超过则发送警报

    参数：
        threshold: 阈值（默认 1600 条）

    返回：
        是否超过阈值（True=已超过）

    说明：
        - 首次超过阈值时发送通知并标记 warned=True
        - 同一天内不会重复发送警报
        - Echotik 每日导出限额为 2000 条，1600 条预警留出缓冲
    """
    # 获取今天的日期
    today = date.today().isoformat()
    # 加载配额数据
    data = _load_quota_data()

    # 今天没有导出记录，不可能超限
    if today not in data:
        return False

    # 获取当日总导出条数和是否已发送过警报
    total = data[today]["total"]
    warned = data[today].get("warned", False)

    # 首次超过阈值且尚未发送过警报
    if total > threshold and not warned:
        # 首次超过阈值，发送警报通知
        log_node("今日导出条数超过阈值", level="WARN",
                 total=total, threshold=threshold)
        # 通过企微/飞书发送配额超限警报
        notify_quota_warning(total, threshold)

        # 标记已警报，避免同一天内重复发送
        data[today]["warned"] = True
        _save_quota_data(data)
        return True

    # 返回是否超过阈值（可能之前已经警报过）
    return total > threshold
