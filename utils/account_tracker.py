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

# 加载 .env 环境变量（包含账号、密码等配置）
load_dotenv()

# 配置文件目录路径（项目根目录下的 config/）
CONFIG_DIR = Path(__file__).parent.parent / "config"
# 账号追踪数据文件路径，JSON 格式存储每个账号的激活日期和通知状态
TRACKER_FILE = CONFIG_DIR / "account_tracker.json"
# .env 文件路径，用于动态增删账号
ENV_FILE = Path(__file__).parent.parent / ".env"

# 账号有效期（天）：Echotik 试用账号有效期为 4 天
MAX_USAGE_DAYS = 4
# 提前提醒天数：第 3 天发送提醒（即到期前 1 天）
WARN_ON_DAY = 3


def _load_tracker_data() -> dict:
    """
    加载追踪数据

    返回：
        追踪数据字典，结构为：
        {
            "accounts": { "email@example.com": { "activated": "2026-03-15" }, ... },
            "notified_day3": ["email@example.com", ...]  # 已发送第3天提醒的账号列表
        }
        文件不存在或读取失败时返回初始空结构
    """
    # 文件不存在时返回初始空结构（首次运行）
    if not TRACKER_FILE.exists():
        return {"accounts": {}, "notified_day3": []}
    try:
        with open(TRACKER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 兼容旧格式：如果缺少 "accounts" 键，返回初始空结构
            if "accounts" not in data:
                return {"accounts": {}, "notified_day3": []}
            return data
    except Exception as e:
        # 读取失败（文件损坏等），记录警告并返回空结构
        log_node("账号追踪文件读取失败", level="WARN", error=str(e)[:60])
        return {"accounts": {}, "notified_day3": []}


def _save_tracker_data(data: dict):
    """
    保存追踪数据到 JSON 文件

    参数：
        data: 追踪数据字典

    说明：
        自动创建 config 目录（如不存在），写入失败时仅记录警告
    """
    # 确保 config 目录存在
    CONFIG_DIR.mkdir(exist_ok=True)
    try:
        # 以 UTF-8 编码写入 JSON，保留中文字符，缩进 2 空格
        with open(TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_node("账号追踪文件保存失败", level="WARN", error=str(e)[:60])


def _get_env_accounts() -> List[str]:
    """
    从环境变量获取账号列表

    返回：
        账号字符串列表，如 ["acc1@mail.com", "acc2@mail.com"]
        环境变量未设置或为空时返回空列表

    说明：
        ECHOTIK_ACCOUNTS 环境变量中多个账号以逗号分隔
    """
    # 读取 ECHOTIK_ACCOUNTS 环境变量
    accounts_str = os.getenv("ECHOTIK_ACCOUNTS", "").strip()
    if not accounts_str:
        return []
    # 按逗号分割，去除每个账号的首尾空格，过滤空字符串
    return [a.strip() for a in accounts_str.split(",") if a.strip()]


def _get_env_passwords() -> List[str]:
    """
    从环境变量获取密码列表

    返回：
        密码字符串列表，与账号列表一一对应
        环境变量未设置或为空时返回空列表

    说明：
        ECHOTIK_PASSWORDS 环境变量中多个密码以逗号分隔，顺序与账号对应
    """
    # 读取 ECHOTIK_PASSWORDS 环境变量
    passwords_str = os.getenv("ECHOTIK_PASSWORDS", "").strip()
    if not passwords_str:
        return []
    # 按逗号分割，去除首尾空格，过滤空字符串
    return [p.strip() for p in passwords_str.split(",") if p.strip()]


def _mask_account(account: str) -> str:
    """
    账号脱敏显示

    参数：
        account: 原始账号字符串（通常是邮箱）

    返回：
        脱敏后的账号，如 "abc***ef"

    说明：
        - 短账号（<=6字符）：保留前2位 + "***"
        - 长账号（>6字符）：保留前3位 + "***" + 倒数第6-4位
        用于日志和通知中保护用户隐私
    """
    # 短账号：只显示前两位
    if len(account) <= 6:
        return account[:2] + "***"
    # 长账号：显示前三位和部分尾部
    return account[:3] + "***" + account[-6:-4]


def _remove_account_from_env(account: str):
    """
    从 .env 文件中删除指定账号及其密码

    参数：
        account: 要删除的账号字符串

    说明：
        - 根据账号在列表中的索引，同时删除对应位置的密码
        - 使用正则替换 .env 文件中的 ECHOTIK_ACCOUNTS 和 ECHOTIK_PASSWORDS 行
        - 删除后重新加载环境变量使其立即生效
    """
    # .env 文件不存在，无法操作
    if not ENV_FILE.exists():
        log_node(".env 文件不存在，跳过删除", level="WARN")
        return

    try:
        # 读取 .env 文件全部内容
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            content = f.read()

        # 获取当前的账号和密码列表
        accounts = _get_env_accounts()
        passwords = _get_env_passwords()

        # 账号不在列表中，无需删除
        if account not in accounts:
            log_node("账号不在 .env 中，跳过", level="WARN",
                     account=_mask_account(account))
            return

        # 找到账号在列表中的索引位置
        idx = accounts.index(account)
        # 从账号列表中移除
        accounts.pop(idx)
        # 同步移除对应位置的密码（如果密码列表足够长）
        if idx < len(passwords):
            passwords.pop(idx)

        # 重新拼接为逗号分隔的字符串
        new_accounts = ",".join(accounts)
        new_passwords = ",".join(passwords)

        # 使用正则替换 .env 文件中的账号行（支持多行匹配）
        content = re.sub(
            r'^ECHOTIK_ACCOUNTS=.*$',
            f'ECHOTIK_ACCOUNTS={new_accounts}',
            content,
            flags=re.MULTILINE
        )
        # 使用正则替换 .env 文件中的密码行
        content = re.sub(
            r'^ECHOTIK_PASSWORDS=.*$',
            f'ECHOTIK_PASSWORDS={new_passwords}',
            content,
            flags=re.MULTILINE
        )

        # 将修改后的内容写回 .env 文件
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(content)

        log_node("已从 .env 删除账号", level="INFO",
                 account=_mask_account(account))

        # 重新加载环境变量，使删除立即生效（override=True 覆盖已有值）
        load_dotenv(override=True)

    except Exception as e:
        log_node("从 .env 删除账号失败", level="ERROR", error=str(e)[:80])


def get_account_days(account: str) -> int:
    """
    获取指定账号已使用天数

    参数：
        account: 账号字符串

    返回：
        使用天数（1-n），激活当天算第1天
        未记录或日期格式错误返回 0
    """
    # 加载追踪数据
    data = _load_tracker_data()
    # 获取指定账号的数据
    acc_data = data.get("accounts", {}).get(account, {})
    # 获取激活日期字符串
    activated = acc_data.get("activated", "")

    # 未记录激活日期
    if not activated:
        return 0

    try:
        # 将激活日期字符串解析为 date 对象
        start = datetime.strptime(activated, "%Y-%m-%d").date()
        # 计算使用天数：今天 - 激活日期 + 1（激活当天算第1天）
        days = (date.today() - start).days + 1
        return days
    except ValueError:
        # 日期格式错误，返回 0
        return 0


def sync_accounts():
    """
    同步环境变量中的账号到追踪器

    说明：
        - 新账号自动注册：激活日期设为今天
        - 已存在的账号保持原有激活日期不变
        - 清理追踪器中已不在 .env 的账号（手动删除的账号）
        - 每次采集启动时调用，确保追踪器与 .env 保持一致
    """
    # 获取 .env 中当前的账号列表
    env_accounts = _get_env_accounts()
    # 加载追踪数据
    data = _load_tracker_data()
    # 获取今天的日期（ISO 格式）
    today = date.today().isoformat()

    # 遍历环境变量中的账号，注册新账号
    for acc in env_accounts:
        if acc not in data["accounts"]:
            # 新账号：记录激活日期为今天
            data["accounts"][acc] = {
                "activated": today
            }
            log_node("新账号已自动注册", level="INFO",
                     account=_mask_account(acc), activated=today)

    # 清理 tracker 中已不在 .env 的账号（可能是用户手动删除的）
    to_remove = [acc for acc in data["accounts"] if acc not in env_accounts]
    for acc in to_remove:
        # 从账号记录中删除
        del data["accounts"][acc]
        # 同时从第3天通知列表中移除（避免残留数据）
        if acc in data.get("notified_day3", []):
            data["notified_day3"].remove(acc)

    # 保存同步后的追踪数据
    _save_tracker_data(data)


def mark_account_expired(account: str):
    """
    标记账号为已过期

    参数：
        account: 过期的账号字符串

    操作步骤：
        1. 从 account_tracker.json 中删除该账号记录
        2. 从 .env 文件中删除该账号及密码
        3. 发送飞书/企微通知，告知运维人员
    """
    # 加载追踪数据
    data = _load_tracker_data()
    # 获取该账号已使用天数（用于通知展示）
    days = get_account_days(account)

    # 从 tracker 的账号记录中删除
    if account in data["accounts"]:
        del data["accounts"][account]
    # 从第3天通知列表中移除
    if account in data.get("notified_day3", []):
        data["notified_day3"].remove(account)
    # 保存更新后的追踪数据
    _save_tracker_data(data)

    # 从 .env 文件中删除该账号及对应密码
    _remove_account_from_env(account)

    # 记录日志并发送通知
    log_node("账号已过期并从号池移除", level="WARN",
             account=_mask_account(account), days=days)
    # 通过企微/飞书发送账号过期移除通知
    notify_account_expired(account, days)


def get_active_accounts() -> List[str]:
    """
    获取所有活跃账号

    返回：
        活跃账号列表（同时存在于 .env 和 tracker 中的账号）

    说明：
        只返回既在环境变量中配置、又在追踪器中有记录的账号
        确保返回的账号都是有效且被追踪的
    """
    # 加载追踪数据
    data = _load_tracker_data()
    # 获取 .env 中的账号列表
    env_accounts = _get_env_accounts()

    # 返回既在 .env 又在 tracker 中的账号（取交集）
    return [acc for acc in env_accounts if acc in data["accounts"]]


def check_expiry_warnings():
    """
    检查所有账号的到期状态，发送第3天提醒

    说明：
        - 遍历所有追踪中的账号，检查使用天数
        - 使用到第 3 天（WARN_ON_DAY）时发送提醒：明天到期，请添加新账号
        - 每个账号只提醒一次（通过 notified_day3 列表去重）
    """
    # 加载追踪数据
    data = _load_tracker_data()
    # 获取已发送第3天提醒的账号列表
    notified_day3 = data.get("notified_day3", [])
    # 标记是否有更新，避免无变化时的无效写入
    updated = False

    # 遍历所有追踪中的账号
    for acc, acc_data in data.get("accounts", {}).items():
        # 获取该账号已使用天数
        days = get_account_days(acc)
        if days == 0:
            # 未记录激活日期，跳过
            continue

        # 第3天提醒：明天到期，请添加新账号
        if days == WARN_ON_DAY and acc not in notified_day3:
            log_node("账号明天到期，发送提醒", level="WARN",
                     account=_mask_account(acc), days=days)
            # 发送即将到期通知
            notify_account_expiring(acc, days)
            # 记录已通知，避免重复发送
            notified_day3.append(acc)
            updated = True

    # 有更新时才保存，减少不必要的文件写入
    if updated:
        data["notified_day3"] = notified_day3
        _save_tracker_data(data)


def log_all_accounts_status():
    """
    输出所有账号的使用状态

    说明：
        遍历 .env 中的所有账号，逐个输出使用天数和剩余天数
        用于启动时的状态概览
    """
    # 获取 .env 中的账号列表
    env_accounts = _get_env_accounts()
    # 加载追踪数据
    data = _load_tracker_data()
    # 获取今天的日期（紧凑格式，如 "20260319"）
    today = date.today().strftime("%Y%m%d")

    # 输出账号总数
    log_node(f"账号状态检查 ({today})", level="INFO", count=len(env_accounts))

    # 逐个输出每个账号的使用天数和剩余天数
    for acc in env_accounts:
        days = get_account_days(acc)
        # 计算剩余天数：有效期 - 已用天数，最小为 0；未记录时显示 "?"
        remaining = max(0, MAX_USAGE_DAYS - days) if days > 0 else "?"
        log_node(f"  {_mask_account(acc)}: 第{days}天, 剩余{remaining}天",
                 level="INFO")


def log_account_status():
    """
    兼容旧接口：输出当前账号使用状态

    说明：
        - 先同步账号（确保 tracker 与 .env 一致）
        - 检查到期提醒（第3天发送通知）
        - 输出第一个账号的使用天数和剩余天数
    """
    # 同步账号（新账号注册、旧账号清理）
    sync_accounts()

    # 检查到期提醒
    check_expiry_warnings()

    # 获取第一个账号的状态并输出
    env_accounts = _get_env_accounts()
    if env_accounts:
        acc = env_accounts[0]
        days = get_account_days(acc)
        today = date.today().strftime("%Y%m%d")
        log_node(f"当前日期 {today}，账号使用 {days} 天", level="INFO",
                 remaining=max(0, MAX_USAGE_DAYS - days))


# ============================================================
# 兼容旧接口（供外部模块调用，内部转发到新实现）
# ============================================================

def check_account_change() -> bool:
    """
    兼容旧接口：检测账号变更

    说明：
        旧版本用于检测 .env 中账号是否发生变化
        新版本通过 sync_accounts 自动处理，始终返回 False
    """
    # 执行账号同步
    sync_accounts()
    # 新版本不再需要返回变更状态
    return False


def get_account_usage_days() -> int:
    """
    兼容旧接口：获取使用天数

    返回：
        第一个账号的使用天数，无账号时返回 1
    """
    env_accounts = _get_env_accounts()
    if env_accounts:
        # 返回第一个账号的使用天数
        return get_account_days(env_accounts[0])
    # 无账号时默认返回 1
    return 1


def check_account_expiry() -> bool:
    """
    兼容旧接口：检查是否到期

    返回：
        第一个账号是否已达到最大使用天数（True=已到期）
    """
    # 先检查到期提醒
    check_expiry_warnings()
    env_accounts = _get_env_accounts()
    if env_accounts:
        days = get_account_days(env_accounts[0])
        # 使用天数 >= 最大有效期天数，视为到期
        return days >= MAX_USAGE_DAYS
    # 无账号时返回 False
    return False
