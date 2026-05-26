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

# 加载 .env 环境变量
load_dotenv()

# 企业微信机器人 Webhook 地址（从环境变量读取，未配置则为空字符串）
WECOM_WEBHOOK  = os.getenv("WECOM_WEBHOOK_URL", "").strip()
# 飞书机器人 Webhook 地址（从环境变量读取，未配置则为空字符串）
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK_URL", "").strip()

# 飞书应用 API 凭证（用于通过飞书开放平台 API 发送文件到群）
FEISHU_APP_ID     = os.getenv("FEISHU_APP_ID", "").strip()      # 飞书应用 App ID
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()   # 飞书应用 App Secret
FEISHU_CHAT_ID    = os.getenv("FEISHU_CHAT_ID", "").strip()      # 飞书群聊 Chat ID


def _account_usage_days() -> str:
    """
    计算 Echotik 账号使用天数（从 ECHOTIK_ACCOUNTS_SINCE 起算）

    返回：
        描述账号使用情况的字符串，如 "Echotik 账号（2个）已使用 15 天（自 2026-03-05）"
        未配置起始日期或格式错误时返回空字符串

    说明：
        用于在通知消息中附加账号使用状态信息
    """
    # 从环境变量获取账号启用起始日期
    since = os.getenv("ECHOTIK_ACCOUNTS_SINCE", "").strip()
    # 未配置起始日期，返回空字符串
    if not since:
        return ""
    try:
        # 解析起始日期字符串为 date 对象
        start = datetime.strptime(since, "%Y-%m-%d").date()
        # 计算使用天数：变动当天记为第1天
        n = (date.today() - start).days + 1
        # 获取账号列表并统计数量
        accounts = os.getenv("ECHOTIK_ACCOUNTS", "").strip()
        count = len(accounts.split(",")) if accounts else 0
        return f"Echotik 账号（{count}个）已使用 {n} 天（自 {since}）"
    except ValueError:
        # 日期格式错误，静默返回空字符串
        return ""


def _send_wecom(content: str):
    """
    发送企业微信机器人通知

    参数：
        content: Markdown 格式的消息内容

    说明：
        - 通过企业微信群机器人 Webhook 发送 Markdown 消息
        - 未配置 WECOM_WEBHOOK 时静默跳过
        - 发送失败仅记录警告，不影响主流程
        - 超时时间 10 秒
    """
    # 未配置企微 Webhook，静默跳过
    if not WECOM_WEBHOOK:
        return
    try:
        # 发送 POST 请求，消息类型为 markdown
        resp = requests.post(
            WECOM_WEBHOOK,
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=10,
        )
        # 检查 HTTP 响应状态码，非 2xx 会抛出异常
        resp.raise_for_status()
        log_node("企微通知已发送", level="INFO")
    except Exception as e:
        # 发送失败仅记录警告，截取错误信息前80字符
        log_node("企微通知发送失败", level="WARN", error=str(e)[:80])


def _send_feishu(title: str, content: str):
    """
    发送飞书机器人通知

    参数：
        title:   消息标题
        content: 消息正文内容

    说明：
        - 通过飞书群机器人 Webhook 发送富文本（post）格式消息
        - 未配置 FEISHU_WEBHOOK 时静默跳过
        - 发送失败仅记录警告，不影响主流程
        - 超时时间 10 秒
    """
    # 未配置飞书 Webhook，静默跳过
    if not FEISHU_WEBHOOK:
        return
    try:
        # 发送 POST 请求，消息类型为富文本（post），使用中文格式
        resp = requests.post(
            FEISHU_WEBHOOK,
            json={
                "msg_type": "post",
                "content": {"post": {"zh_cn": {
                    "title": title,
                    # 富文本内容为二维数组，每个元素是一个段落，段落内是文本节点
                    "content": [[{"tag": "text", "text": content}]],
                }}},
            },
            timeout=10,
        )
        # 检查 HTTP 响应状态码
        resp.raise_for_status()
        log_node("飞书通知已发送", level="INFO")
    except Exception as e:
        # 发送失败仅记录警告
        log_node("飞书通知发送失败", level="WARN", error=str(e)[:80])


def _notify(title: str, content: str):
    """
    同时向企微和飞书发送通知

    参数：
        title:   通知标题
        content: 通知正文内容

    说明：
        企微消息将标题加粗后与正文拼接为 Markdown 格式
        飞书消息使用原生的标题+正文结构
    """
    # 企微：标题加粗（Markdown 语法）+ 换行 + 正文
    _send_wecom(f"**{title}**\n{content}")
    # 飞书：标题和正文分开传递
    _send_feishu(title, content)


# ============================================================
# 成功通知
# ============================================================

def notify_success(captured: str, success: list, attempt: int):
    """
    采集全部成功通知

    参数：
        captured: 采集日期（YYYY-MM-DD）
        success:  成功的模块名称列表，如 ["商品榜_d", "小店榜_w"]
        attempt:  第几次尝试成功

    说明：
        通知内容包含采集日期、完成时间、成功模块列表和账号使用状态
    """
    # 获取当前时间（时:分）
    ts = datetime.now().strftime("%H:%M")
    # 获取账号使用天数信息
    usage = _account_usage_days()
    # 组装通知正文
    content = (
        f"采集日期：{captured}\n"
        f"完成时间：{ts}（第 {attempt} 次尝试）\n"
        f"成功模块：{', '.join(success)}\n"
        f"数据清洗流水线已触发。"
    )
    # 如果有账号使用信息，追加到正文末尾
    if usage:
        content += f"\n{usage}"
    log_node("发送采集成功通知", level="INFO")
    _notify("✅ Echotik 采集完成", content)


# ============================================================
# 等待重试通知
# ============================================================

def notify_retry_wait(captured: str, stale: list, failed: list,
                      retry_wait_hours: float, retry_time: str):
    """
    第一次尝试未完成，进入等待重试状态

    参数：
        captured:         采集日期（YYYY-MM-DD）
        stale:            数据未更新的模块列表
        failed:           下载失败的模块列表
        retry_wait_hours: 距离下次重试的小时数
        retry_time:       下次重试的具体时间字符串

    说明：
        当首次采集发现数据未更新（stale）或下载失败（failed）时发送此通知，
        告知用户将在指定时间自动重试
    """
    # 获取当前时间
    ts = datetime.now().strftime("%H:%M")
    # 组装原因描述
    parts = []
    if stale:
        parts.append(f"数据未更新：{', '.join(stale)}")
    if failed:
        parts.append(f"下载失败：{', '.join(failed)}")
    # 组装通知正文
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
    """
    所有重试耗尽，最终采集失败通知

    参数：
        captured: 采集日期（YYYY-MM-DD）
        stale:    数据未更新的模块列表
        failed:   下载失败的模块列表

    说明：
        已重试2次仍未成功，需要人工介入检查 Echotik 平台
    """
    # 获取当前时间
    ts = datetime.now().strftime("%H:%M")
    # 组装通知正文，stale 和 failed 为空时显示"无"
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
    """
    Pipeline 执行失败通知

    参数：
        captured: 采集日期（YYYY-MM-DD）
        stderr:   Pipeline 的标准错误输出

    说明：
        数据清洗流水线执行出错时发送，错误信息截取前200字符避免消息过长
    """
    # 组装通知正文，截取错误信息前200字符
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
    """
    验证码/风控通知

    参数：
        module:  触发风控的模块名称
        win:     粒度（d/w/m）
        account: 触发风控的账号

    说明：
        当 Echotik 页面出现验证码或风控拦截时发送此通知，
        需要用户手动登录完成验证后，脚本在下次重试时自动恢复
    """
    # 组装通知正文
    content = (
        f"模块：{module}  粒度：{win}\n"
        f"账号：{account}\n"
        f"请手动登录 Echotik 完成验证后，脚本将在下次重试时恢复。"
    )
    log_node("发送验证码通知", level="WARN")
    _notify("⚠️ Echotik 检测到验证码/风控", content)


# ============================================================
# 订阅到期通知
# ============================================================

def notify_subscription_expired(account: str):
    """
    账号订阅到期通知

    参数：
        account: 订阅到期的账号

    说明：
        当检测到 Echotik 账号订阅已到期（Free 状态）时发送此通知
    """
    # 组装通知正文
    content = (
        f"账号：{account}\n"
        f"订阅已到期，请更换账号或续费。"
    )
    log_node("发送订阅到期通知", level="ERROR")
    _notify("❌ Echotik 账号订阅到期", content)


# ============================================================
# 导出配额警报
# ============================================================

def notify_quota_warning(total: int, threshold: int):
    """
    导出配额超限警报

    参数：
        total:     今日已导出总条数
        threshold: 警报阈值

    说明：
        Echotik 每日导出限额为 2000 条，超过阈值时提前预警
    """
    # 组装通知正文
    content = (
        f"今日已导出：{total} 条\n"
        f"阈值：{threshold} 条\n"
        f"请注意规划，避免超出每日限额（2000条）。"
    )
    log_node("发送配额警报", level="WARN")
    _notify("⚠️ Echotik 今日导出条数超限", content)


# ============================================================
# 账号到期警报
# ============================================================

def notify_account_expiring(account: str, days: int):
    """
    账号即将到期提醒（第3天）

    参数：
        account: 即将到期的账号
        days:    已使用天数

    说明：
        在账号使用第3天（到期前1天）发送提醒，提示用户添加新账号到号池
    """
    # 账号脱敏处理：长账号保留前3位和部分尾部，短账号保留前2位
    masked = account[:3] + "***" + account[-6:-4] if len(account) > 6 else account[:2] + "***"
    # 组装通知正文
    content = (
        f"账号：{masked}\n"
        f"已使用：{days} 天\n"
        f"该账号明天到期，请添加新账号到号池。"
    )
    log_node("发送账号即将到期提醒", level="WARN", account=masked)
    _notify("⏰ Echotik 账号明天到期", content)


def notify_account_expired(account: str, days: int):
    """
    账号已过期并从号池移除通知

    参数：
        account: 已过期的账号（也可能是旧接口传入的日期字符串）
        days:    已使用天数

    说明：
        当账号达到最大使用天数后，自动从号池移除并发送此通知
        兼容旧接口：account 参数可能是字符串而非邮箱格式
    """
    # 账号脱敏处理：判断是否为邮箱格式
    if isinstance(account, str) and "@" in account:
        # 邮箱格式的账号进行脱敏
        masked = account[:3] + "***" + account[-6:-4] if len(account) > 6 else account[:2] + "***"
    else:
        # 兼容旧调用（可能传入 today 日期字符串），直接转为字符串
        masked = str(account)
    # 组装通知正文
    content = (
        f"账号：{masked}\n"
        f"已使用：{days} 天\n"
        f"该账号已过期并从号池移除。\n"
        f"请确保号池中还有可用账号。"
    )
    log_node("发送账号过期移除通知", level="WARN", account=masked)
    _notify("❌ Echotik 账号已过期移除", content)


# ============================================================
# 飞书应用 API：发送文件到群
# ============================================================

def _get_feishu_tenant_token() -> str:
    """
    获取飞书 tenant_access_token

    返回：
        飞书租户访问令牌字符串，获取失败时返回空字符串

    说明：
        - 通过飞书开放平台的内部应用鉴权接口获取 token
        - 需要配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET
        - token 有效期约 2 小时，每次调用重新获取（简化实现，不做缓存）
    """
    # 未配置飞书应用凭证，返回空字符串
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        return ""
    try:
        # 调用飞书内部应用鉴权接口获取 tenant_access_token
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
            timeout=10,
        )
        data = resp.json()
        # code 为 0 表示成功
        if data.get("code") == 0:
            return data["tenant_access_token"]
        # 获取失败，记录错误信息
        log_node("飞书 token 获取失败", level="WARN", msg=data.get("msg"))
    except Exception as e:
        log_node("飞书 token 请求异常", level="WARN", error=str(e)[:80])
    return ""


def _feishu_upload_file(token: str, file_path: Path) -> str:
    """
    上传文件到飞书，返回 file_key

    参数：
        token:     飞书 tenant_access_token
        file_path: 要上传的本地文件路径

    返回：
        飞书文件的 file_key，上传失败时返回空字符串

    说明：
        - 使用飞书 IM 文件上传接口（/im/v1/files）
        - 文件类型设为 stream（通用二进制流）
        - 超时时间 30 秒（文件可能较大）
    """
    try:
        # 以二进制模式打开文件并上传
        with open(file_path, "rb") as f:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                # data 参数传递表单字段
                data={"file_type": "stream", "file_name": file_path.name},
                # files 参数传递文件内容
                files={"file": (file_path.name, f)},
                timeout=30,
            )
        data = resp.json()
        # code 为 0 表示上传成功，提取 file_key
        if data.get("code") == 0:
            return data["data"]["file_key"]
        # 上传失败，记录文件名和错误信息
        log_node("飞书文件上传失败", level="WARN",
                 file=file_path.name, msg=data.get("msg"))
    except Exception as e:
        log_node("飞书文件上传异常", level="WARN",
                 file=file_path.name, error=str(e)[:80])
    return ""


def _feishu_send_file(token: str, chat_id: str, file_key: str):
    """
    发送文件消息到飞书群

    参数：
        token:    飞书 tenant_access_token
        chat_id:  目标群聊的 Chat ID
        file_key: 已上传文件的 file_key

    说明：
        - 使用飞书 IM 消息发送接口（/im/v1/messages）
        - receive_id_type=chat_id 表示接收者为群聊
        - content 字段需要 JSON 字符串格式
    """
    import json
    try:
        # 发送文件消息到指定群聊
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "file",
                # content 需要是 JSON 字符串（飞书 API 要求）
                "content": json.dumps({"file_key": file_key}),
            },
            timeout=10,
        )
        data = resp.json()
        # code 非 0 表示发送失败
        if data.get("code") != 0:
            log_node("飞书文件发送失败", level="WARN", msg=data.get("msg"))
    except Exception as e:
        log_node("飞书文件发送异常", level="WARN", error=str(e)[:80])


def _feishu_send_text(token: str, chat_id: str, text: str):
    """
    发送文本消息到飞书群

    参数：
        token:   飞书 tenant_access_token
        chat_id: 目标群聊的 Chat ID
        text:    文本消息内容

    说明：
        - 用于在发送文件前先发一条文字摘要
        - 发送失败时静默忽略（不影响后续文件发送）
    """
    import json
    try:
        # 发送文本消息到指定群聊
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                # content 需要是 JSON 字符串
                "content": json.dumps({"text": text}),
            },
            timeout=10,
        )
    except Exception:
        # 文字摘要发送失败，静默忽略
        pass


def send_files_to_feishu(captured: str, exports_dir: str):
    """
    Pipeline 完成后，将 raw + clean + candidates 文件发送到飞书群

    参数：
        captured:    采集日期（YYYY-MM-DD）
        exports_dir: exports/captured=YYYY-MM-DD 目录路径

    说明：
        - 需要配置 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_CHAT_ID 三个环境变量
        - 依次扫描 raw/、clean/、candidates/ 三个子目录中的文件
        - 先发送一条文字摘要，再逐个上传并发送文件
        - 未配置或目录不存在时静默跳过
    """
    # 检查飞书应用 API 凭证是否完整配置
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET or not FEISHU_CHAT_ID:
        log_node("飞书文件推送未配置，跳过", level="INFO",
                 hint="需在 .env 中配置 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_CHAT_ID")
        return

    # 获取飞书 tenant_access_token
    token = _get_feishu_tenant_token()
    # token 获取失败，无法继续
    if not token:
        return

    # 检查 exports 目录是否存在
    exports_path = Path(exports_dir)
    if not exports_path.exists():
        log_node("exports 目录不存在，跳过飞书推送", level="WARN", path=exports_dir)
        return

    # 收集要发送的文件：raw/*.xlsx + clean/*.csv + candidates/*.csv
    files_to_send: List[Path] = []
    for sub in ["raw", "clean", "candidates"]:
        sub_dir = exports_path / sub
        # 子目录存在时，将其中所有文件加入发送列表（按文件名排序）
        if sub_dir.exists():
            files_to_send.extend(sorted(sub_dir.iterdir()))

    # 没有找到任何文件，跳过
    if not files_to_send:
        log_node("没有找到要推送的文件", level="WARN", path=exports_dir)
        return

    # 先发一条文字摘要，告知群成员即将推送文件
    _feishu_send_text(token, FEISHU_CHAT_ID,
                      f"📦 Echotik 数据推送（{captured}）\n"
                      f"共 {len(files_to_send)} 个文件，正在逐个发送...")

    # 逐个上传并发送文件
    sent = 0  # 成功发送的文件计数
    for fp in files_to_send:
        # 跳过非文件项（如子目录）
        if not fp.is_file():
            continue
        # 上传文件到飞书，获取 file_key
        file_key = _feishu_upload_file(token, fp)
        if file_key:
            # 上传成功，发送文件消息到群聊
            _feishu_send_file(token, FEISHU_CHAT_ID, file_key)
            sent += 1
            log_node("飞书文件已发送", level="INFO", file=fp.name)

    # 记录推送完成日志，显示总文件数和成功发送数
    log_node("飞书文件推送完成", level="INFO", total=len(files_to_send), sent=sent)
