#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/notifier.py
通知模块（成功 + 失败 + 等待重试 + pipeline失败 + 验证码风控）

支持：
    - 企业微信机器人 Webhook（Markdown 格式）
    - 飞书机器人 Webhook（富文本格式）
    未配置对应 Webhook 时静默跳过，不影响主流程。
"""

import os
from datetime import datetime, date
from pathlib import Path
from typing import List

import requests
from dotenv import load_dotenv
from utils.logger import log_node

load_dotenv()

WECOM_WEBHOOK  = os.getenv("WECOM_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK_URL", "").strip()

# 飞书应用 API（用于发文件到群）
FEISHU_APP_ID     = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
FEISHU_CHAT_ID    = os.getenv("FEISHU_CHAT_ID", "").strip()


def _account_usage_days() -> str:
    """计算 Echotik 账号使用天数（从 ECHOTIK_ACCOUNTS_SINCE 起算）"""
    since = os.getenv("ECHOTIK_ACCOUNTS_SINCE", "").strip()
    if not since:
        return ""
    try:
        start = datetime.strptime(since, "%Y-%m-%d").date()
        n = (date.today() - start).days + 1  # 变动当天记为第1天
        accounts = os.getenv("ECHOTIK_ACCOUNTS", "").strip()
        count = len(accounts.split(",")) if accounts else 0
        return f"Echotik 账号（{count}个）已使用 {n} 天（自 {since}）"
    except ValueError:
        return ""


def _send_wecom(content: str):
    if not WECOM_WEBHOOK:
        return
    try:
        resp = requests.post(
            WECOM_WEBHOOK,
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=10,
        )
        resp.raise_for_status()
        log_node("企微通知已发送", level="INFO")
    except Exception as e:
        log_node("企微通知发送失败", level="WARN", error=str(e)[:80])


def _send_feishu(title: str, content: str):
    if not FEISHU_WEBHOOK:
        return
    try:
        resp = requests.post(
            FEISHU_WEBHOOK,
            json={
                "msg_type": "post",
                "content": {"post": {"zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": content}]],
                }}},
            },
            timeout=10,
        )
        resp.raise_for_status()
        log_node("飞书通知已发送", level="INFO")
    except Exception as e:
        log_node("飞书通知发送失败", level="WARN", error=str(e)[:80])


def _notify(title: str, content: str):
    """同时向企微和飞书发送通知"""
    _send_wecom(f"**{title}**\n{content}")
    _send_feishu(title, content)


# ============================================================
# 成功通知
# ============================================================

def notify_success(captured: str, success: list, attempt: int):
    """采集全部成功通知"""
    ts = datetime.now().strftime("%H:%M")
    usage = _account_usage_days()
    content = (
        f"采集日期：{captured}\n"
        f"完成时间：{ts}（第 {attempt} 次尝试）\n"
        f"成功模块：{', '.join(success)}\n"
        f"数据清洗流水线已触发。"
    )
    if usage:
        content += f"\n{usage}"
    log_node("发送采集成功通知", level="INFO")
    _notify("✅ Echotik 采集完成", content)


# ============================================================
# 等待重试通知
# ============================================================

def notify_retry_wait(captured: str, stale: list, failed: list,
                      retry_wait_hours: float, retry_time: str):
    """第一次尝试未完成，进入等待重试状态"""
    ts = datetime.now().strftime("%H:%M")
    parts = []
    if stale:
        parts.append(f"数据未更新：{', '.join(stale)}")
    if failed:
        parts.append(f"下载失败：{', '.join(failed)}")
    content = (
        f"采集日期：{captured}\n"
        f"检测时间：{ts}\n"
        f"原因：{'；'.join(parts)}\n"
        f"将于 {retry_time} 自动重试（约 {retry_wait_hours:.0f} 小时后）。"
    )
    log_node("发送等待重试通知", level="INFO")
    _notify("⏳ Echotik 数据未就绪，等待重试", content)


# ============================================================
# 最终失败通知
# ============================================================

def notify_final_failure(captured: str, stale: list, failed: list):
    """所有重试耗尽，最终采集失败通知"""
    ts = datetime.now().strftime("%H:%M")
    content = (
        f"采集日期：{captured}\n"
        f"时间：{ts}\n"
        f"未更新（stale）：{', '.join(stale) or '无'}\n"
        f"失败（failed）：{', '.join(failed) or '无'}\n"
        f"已重试2次，请人工检查 Echotik 平台。"
    )
    log_node("发送最终失败通知", level="ERROR")
    _notify("❌ Echotik 采集最终失败", content)


# ============================================================
# Pipeline 失败通知
# ============================================================

def notify_pipeline_failure(captured: str, stderr: str):
    """Pipeline 执行失败通知"""
    content = (
        f"采集日期：{captured}\n"
        f"错误信息（前200字）：\n{stderr[:200]}"
    )
    log_node("发送 pipeline 失败通知", level="ERROR")
    _notify("❌ Echotik Pipeline 执行失败", content)


# ============================================================
# 验证码 / 风控通知
# ============================================================

def notify_captcha(module: str, win: str, account: str):
    """验证码/风控通知"""
    content = (
        f"模块：{module}  粒度：{win}\n"
        f"账号：{account}\n"
        f"请手动登录 Echotik 完成验证后，脚本将在下次重试时恢复。"
    )
    log_node("发送验证码通知", level="WARN")
    _notify("⚠️ Echotik 检测到验证码/风控", content)


# ============================================================
# 飞书应用 API：发送文件到群
# ============================================================

def _get_feishu_tenant_token() -> str:
    """获取飞书 tenant_access_token"""
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return ""
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            return data["tenant_access_token"]
        log_node("飞书 token 获取失败", level="WARN", msg=data.get("msg"))
    except Exception as e:
        log_node("飞书 token 请求异常", level="WARN", error=str(e)[:80])
    return ""


def _feishu_upload_file(token: str, file_path: Path) -> str:
    """上传文件到飞书，返回 file_key"""
    try:
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={"file_type": "stream", "file_name": file_path.name},
                files={"file": (file_path.name, f)},
                timeout=30,
            )
        data = resp.json()
        if data.get("code") == 0:
            return data["data"]["file_key"]
        log_node("飞书文件上传失败", level="WARN",
                 file=file_path.name, msg=data.get("msg"))
    except Exception as e:
        log_node("飞书文件上传异常", level="WARN",
                 file=file_path.name, error=str(e)[:80])
    return ""


def _feishu_send_file(token: str, chat_id: str, file_key: str):
    """发送文件消息到飞书群"""
    import json
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "file",
                "content": json.dumps({"file_key": file_key}),
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            log_node("飞书文件发送失败", level="WARN", msg=data.get("msg"))
    except Exception as e:
        log_node("飞书文件发送异常", level="WARN", error=str(e)[:80])


def _feishu_send_text(token: str, chat_id: str, text: str):
    """发送文本消息到飞书群"""
    import json
    try:
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=10,
        )
    except Exception:
        pass


def send_files_to_feishu(captured: str, exports_dir: str):
    """
    Pipeline 完成后，将 raw + clean + candidates 文件发送到飞书群

    参数：
        captured: 采集日期（YYYY-MM-DD）
        exports_dir: exports/captured=YYYY-MM-DD 目录路径
    """
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET or not FEISHU_CHAT_ID:
        log_node("飞书文件推送未配置，跳过", level="INFO",
                 hint="需在 .env 中配置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_CHAT_ID")
        return

    token = _get_feishu_tenant_token()
    if not token:
        return

    exports_path = Path(exports_dir)
    if not exports_path.exists():
        log_node("exports 目录不存在，跳过飞书推送", level="WARN", path=exports_dir)
        return

    # 收集要发送的文件：raw/*.xlsx + clean/*.csv + candidates/*.csv
    files_to_send: List[Path] = []
    for sub in ["raw", "clean", "candidates"]:
        sub_dir = exports_path / sub
        if sub_dir.exists():
            files_to_send.extend(sorted(sub_dir.iterdir()))

    if not files_to_send:
        log_node("没有找到要推送的文件", level="WARN", path=exports_dir)
        return

    # 先发一条文字摘要
    _feishu_send_text(token, FEISHU_CHAT_ID,
                      f"📦 Echotik 数据推送（{captured}）\n"
                      f"共 {len(files_to_send)} 个文件，正在逐个发送...")

    sent = 0
    for fp in files_to_send:
        if not fp.is_file():
            continue
        file_key = _feishu_upload_file(token, fp)
        if file_key:
            _feishu_send_file(token, FEISHU_CHAT_ID, file_key)
            sent += 1
            log_node("飞书文件已发送", level="INFO", file=fp.name)

    log_node("飞书文件推送完成", level="INFO", total=len(files_to_send), sent=sent)