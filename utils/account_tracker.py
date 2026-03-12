#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/account_tracker.py
账号使用天数追踪模块

功能：
    - 检测账号是否有新增
    - 计算当前账号已使用天数
    - 到期（n=4）时发送警报
"""

import hashlib
import json
import os
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv

from utils.logger import log_node
from utils.notifier import notify_account_expired

load_dotenv()

CONFIG_DIR = Path(__file__).parent.parent / "config"
TRACKER_FILE = CONFIG_DIR / "account_tracker.json"

# 账号有效期（天）
MAX_USAGE_DAYS = 4


def _get_accounts_hash() -> str:
    """计算当前账号列表的哈希值"""
    accounts_str = os.getenv("ECHOTIK_ACCOUNTS", "").strip()
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "").strip()
    combined = f"{accounts_str}|{passwords_str}"
    return hashlib.md5(combined.encode()).hexdigest()[:8]


def _load_tracker_data() -> dict:
    """加载追踪数据"""
    if not TRACKER_FILE.exists():
        return {}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log_node("账号追踪文件读取失败", level="WARN", error=str(e)[:60])
        return {}


def _save_tracker_data(data: dict):
    """保存追踪数据"""
    CONFIG_DIR.mkdir(exist_ok=True)
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_node("账号追踪文件保存失败", level="WARN", error=str(e)[:60])


def check_account_change() -> bool:
    """
    检测账号是否有新增/变更

    返回：
        是否有变更
    """
    current_hash = _get_accounts_hash()
    data = _load_tracker_data()

    old_hash = data.get("accounts_hash", "")

    if current_hash != old_hash:
        # 账号有变更，更新日期
        today = date.today().isoformat()
        data["accounts_hash"] = current_hash
        data["last_change_date"] = today
        data["notified_expired"] = False  # 重置警报标记
        _save_tracker_data(data)

        log_node("检测到账号变更，重置计数", level="INFO",
                 date=today, hash=current_hash)
        return True

    return False


def get_account_usage_days() -> int:
    """
    计算当前账号已使用天数

    返回：
        使用天数（1-n）
    """
    data = _load_tracker_data()
    last_change = data.get("last_change_date", "")

    if not last_change:
        # 没有记录，初始化为今天
        today = date.today().isoformat()
        data["accounts_hash"] = _get_accounts_hash()
        data["last_change_date"] = today
        data["notified_expired"] = False
        _save_tracker_data(data)
        return 1

    try:
        start = datetime.strptime(last_change, "%Y-%m-%d").date()
        days = (date.today() - start).days + 1  # 变更当天记为第1天
        return days
    except ValueError:
        return 1


def check_account_expiry() -> bool:
    """
    检查账号是否到期（n=4），到期则发送警报

    返回：
        是否到期
    """
    days = get_account_usage_days()
    data = _load_tracker_data()
    notified = data.get("notified_expired", False)

    if days >= MAX_USAGE_DAYS and not notified:
        # 到期且未发送过警报
        today = date.today().strftime("%Y%m%d")
        log_node("账号已到期", level="WARN", date=today, days=days)
        notify_account_expired(today, days)

        # 标记已警报
        data["notified_expired"] = True
        _save_tracker_data(data)
        return True

    return days >= MAX_USAGE_DAYS


def log_account_status():
    """输出当前账号使用状态"""
    today = date.today().strftime("%Y%m%d")
    days = get_account_usage_days()
    log_node(f"当前日期 {today}，账号使用 {days} 天", level="INFO",
             remaining=max(0, MAX_USAGE_DAYS - days))
