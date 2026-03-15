#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/account_tracker.py
多账号独立计时追踪模块

功能：
    - 每个账号独立记录激活日期
    - 第3天发送提醒（明天到期，请添加新账号）
    - 检测到 Free/订阅到期时从号池删除该账号
"""

import json
import os
import re
from datetime import datetime, date
from pathlib import Path
from typing import List

from dotenv import load_dotenv

from utils.logger import log_node
from utils.notifier import notify_account_expiring, notify_account_expired

load_dotenv()

CONFIG_DIR = Path(__file__).parent.parent / "config"
TRACKER_FILE = CONFIG_DIR / "account_tracker.json"
ENV_FILE = Path(__file__).parent.parent / ".env"

# 账号有效期（天）
MAX_USAGE_DAYS = 4
# 提前提醒天数（第3天提醒）
WARN_ON_DAY = 3


def _load_tracker_data() -> dict:
    """加载追踪数据"""
    if not TRACKER_FILE.exists():
        return {"accounts": {}, "notified_day3": []}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 兼容旧格式
            if "accounts" not in data:
                return {"accounts": {}, "notified_day3": []}
            return data
    except Exception as e:
        log_node("账号追踪文件读取失败", level="WARN", error=str(e)[:60])
        return {"accounts": {}, "notified_day3": []}


def _save_tracker_data(data: dict):
    """保存追踪数据"""
    CONFIG_DIR.mkdir(exist_ok=True)
    try:
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_node("账号追踪文件保存失败", level="WARN", error=str(e)[:60])


def _get_env_accounts() -> List[str]:
    """从环境变量获取账号列表"""
    accounts_str = os.getenv("ECHOTIK_ACCOUNTS", "").strip()
    if not accounts_str:
        return []
    return [a.strip() for a in accounts_str.split(",") if a.strip()]


def _get_env_passwords() -> List[str]:
    """从环境变量获取密码列表"""
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "").strip()
    if not passwords_str:
        return []
    return [p.strip() for p in passwords_str.split(",") if p.strip()]


def _mask_account(account: str) -> str:
    """账号脱敏显示"""
    if len(account) <= 6:
        return account[:2] + "***"
    return account[:3] + "***" + account[-6:-4]


def _remove_account_from_env(account: str):
    """
    从 .env 文件中删除指定账号及其密码
    """
    if not ENV_FILE.exists():
        log_node(".env 文件不存在，跳过删除", level="WARN")
        return

    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        accounts = _get_env_accounts()
        passwords = _get_env_passwords()

        if account not in accounts:
            log_node("账号不在 .env 中，跳过", level="WARN",
                     account=_mask_account(account))
            return

        idx = accounts.index(account)
        accounts.pop(idx)
        if idx < len(passwords):
            passwords.pop(idx)

        # 更新 .env 内容
        new_accounts = ",".join(accounts)
        new_passwords = ",".join(passwords)

        content = re.sub(
            r'^ECHOTIK_ACCOUNTS=.*$',
            f'ECHOTIK_ACCOUNTS={new_accounts}',
            content,
            flags=re.MULTILINE
        )
        content = re.sub(
            r'^ECHOTIK_PASSWORDS=.*$',
            f'ECHOTIK_PASSWORDS={new_passwords}',
            content,
            flags=re.MULTILINE
        )

        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(content)

        log_node("已从 .env 删除账号", level="INFO",
                 account=_mask_account(account))

        # 重新加载环境变量
        load_dotenv(override=True)

    except Exception as e:
        log_node("从 .env 删除账号失败", level="ERROR", error=str(e)[:80])


def get_account_days(account: str) -> int:
    """
    获取指定账号已使用天数

    返回：
        使用天数（1-n），未记录返回 0
    """
    data = _load_tracker_data()
    acc_data = data.get("accounts", {}).get(account, {})
    activated = acc_data.get("activated", "")

    if not activated:
        return 0

    try:
        start = datetime.strptime(activated, "%Y-%m-%d").date()
        days = (date.today() - start).days + 1
        return days
    except ValueError:
        return 0


def sync_accounts():
    """
    同步环境变量中的账号到追踪器
    - 新账号自动注册（激活日期=今天）
    - 已存在的账号保持原有记录
    """
    env_accounts = _get_env_accounts()
    data = _load_tracker_data()
    today = date.today().isoformat()

    for acc in env_accounts:
        if acc not in data["accounts"]:
            data["accounts"][acc] = {
                "activated": today
            }
            log_node("新账号已自动注册", level="INFO",
                     account=_mask_account(acc), activated=today)

    # 清理 tracker 中已不在 .env 的账号
    to_remove = [acc for acc in data["accounts"] if acc not in env_accounts]
    for acc in to_remove:
        del data["accounts"][acc]
        if acc in data.get("notified_day3", []):
            data["notified_day3"].remove(acc)

    _save_tracker_data(data)


def mark_account_expired(account: str):
    """
    标记账号为已过期：
    1. 从 account_tracker.json 中删除
    2. 从 .env 中删除
    3. 发送飞书通知
    """
    data = _load_tracker_data()
    days = get_account_days(account)

    # 从 tracker 删除
    if account in data["accounts"]:
        del data["accounts"][account]
    if account in data.get("notified_day3", []):
        data["notified_day3"].remove(account)
    _save_tracker_data(data)

    # 从 .env 删除
    _remove_account_from_env(account)

    # 发送通知
    log_node("账号已过期并从号池移除", level="WARN",
             account=_mask_account(account), days=days)
    notify_account_expired(account, days)


def get_active_accounts() -> List[str]:
    """
    获取所有活跃账号

    返回：
        账号列表（tracker 中记录的都是活跃的）
    """
    data = _load_tracker_data()
    env_accounts = _get_env_accounts()

    # 返回既在 .env 又在 tracker 中的账号
    return [acc for acc in env_accounts if acc in data["accounts"]]


def check_expiry_warnings():
    """
    检查所有账号的到期状态，发送第3天提醒
    """
    data = _load_tracker_data()
    notified_day3 = data.get("notified_day3", [])
    updated = False

    for acc, acc_data in data.get("accounts", {}).items():
        days = get_account_days(acc)
        if days == 0:
            continue

        # 第3天提醒
        if days == WARN_ON_DAY and acc not in notified_day3:
            log_node("账号明天到期，发送提醒", level="WARN",
                     account=_mask_account(acc), days=days)
            notify_account_expiring(acc, days)
            notified_day3.append(acc)
            updated = True

    if updated:
        data["notified_day3"] = notified_day3
        _save_tracker_data(data)


def log_all_accounts_status():
    """输出所有账号的使用状态"""
    env_accounts = _get_env_accounts()
    data = _load_tracker_data()
    today = date.today().strftime("%Y%m%d")

    log_node(f"账号状态检查 ({today})", level="INFO", count=len(env_accounts))

    for acc in env_accounts:
        days = get_account_days(acc)
        remaining = max(0, MAX_USAGE_DAYS - days) if days > 0 else "?"
        log_node(f"  {_mask_account(acc)}: 第{days}天, 剩余{remaining}天",
                 level="INFO")


def log_account_status():
    """兼容旧接口：输出当前账号使用状态"""
    # 同步账号
    sync_accounts()

    # 检查到期提醒
    check_expiry_warnings()

    # 获取第一个账号的状态
    env_accounts = _get_env_accounts()
    if env_accounts:
        acc = env_accounts[0]
        days = get_account_days(acc)
        today = date.today().strftime("%Y%m%d")
        log_node(f"当前日期 {today}，账号使用 {days} 天", level="INFO",
                 remaining=max(0, MAX_USAGE_DAYS - days))


# 兼容旧接口
def check_account_change() -> bool:
    """兼容旧接口：检测账号变更"""
    sync_accounts()
    return False


def get_account_usage_days() -> int:
    """兼容旧接口：获取使用天数"""
    env_accounts = _get_env_accounts()
    if env_accounts:
        return get_account_days(env_accounts[0])
    return 1


def check_account_expiry() -> bool:
    """兼容旧接口：检查是否到期"""
    check_expiry_warnings()
    env_accounts = _get_env_accounts()
    if env_accounts:
        days = get_account_days(env_accounts[0])
        return days >= MAX_USAGE_DAYS
    return False
