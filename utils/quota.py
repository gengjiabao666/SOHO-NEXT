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

CONFIG_DIR = Path(__file__).parent.parent / "config"
QUOTA_FILE = CONFIG_DIR / "export_quota.json"

# 默认阈值
DEFAULT_THRESHOLD = 1600


def _load_quota_data() -> dict:
    """加载配额数据"""
    if not QUOTA_FILE.exists():
        return {}
    try:
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_node("配额文件读取失败", level="WARN", error=str(e)[:60])
        return {}


def _save_quota_data(data: dict):
    """保存配额数据"""
    CONFIG_DIR.mkdir(exist_ok=True)
    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_node("配额文件保存失败", level="WARN", error=str(e)[:60])


def record_export(task: str, count: int):
    """
    记录单次导出

    参数：
        task: 任务名称（如 "商品榜_d"）
        count: 导出条数
    """
    today = date.today().isoformat()
    now = datetime.now().strftime("%H:%M:%S")

    data = _load_quota_data()

    if today not in data:
        data[today] = {"total": 0, "records": [], "warned": False}

    data[today]["total"] += count
    data[today]["records"].append({
        "time": now,
        "task": task,
        "count": count
    })

    _save_quota_data(data)

    log_node("导出条数已记录", level="INFO",
             task=task, count=count, today_total=data[today]["total"])


def get_today_total() -> int:
    """获取今日已导出总条数"""
    today = date.today().isoformat()
    data = _load_quota_data()
    return data.get(today, {}).get("total", 0)


def check_quota_warning(threshold: int = DEFAULT_THRESHOLD) -> bool:
    """
    检查是否超过阈值，超过则发送警报

    参数：
        threshold: 阈值（默认 1600）

    返回：
        是否超过阈值
    """
    today = date.today().isoformat()
    data = _load_quota_data()

    if today not in data:
        return False

    total = data[today]["total"]
    warned = data[today].get("warned", False)

    if total > threshold and not warned:
        # 首次超过阈值，发送警报
        log_node("今日导出条数超过阈值", level="WARN",
                 total=total, threshold=threshold)
        notify_quota_warning(total, threshold)

        # 标记已警报，避免重复
        data[today]["warned"] = True
        _save_quota_data(data)
        return True

    return total > threshold
