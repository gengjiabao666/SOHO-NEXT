#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py
Echotik 采集器运行状态监控

检查项：
  - 最近一次采集日志状态
  - 数据文件更新时间
  - 磁盘空间使用
  - 进程运行状态
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
PROJECT_ROOT = Path(__file__).parent
SOHO_REPO = Path(os.getenv("SOHO_REPO", "/home/ubuntu/soho_repo"))
ECHOTIK_EXPORTS = SOHO_REPO / "03_data_sources" / "echotik" / "exports"
ANALYST_LOG = PROJECT_ROOT / "logs" / "analyst.log"


def check_last_run():
    """检查最近一次运行日志"""
    log_file = PROJECT_ROOT / "logs" / "cron.log"
    if not log_file.exists():
        return {"status": "⚠️", "msg": "日志文件不存在"}
    
    try:
        # 读取最后 100 行
        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()[-100:]
        
        # 查找最近的成功/失败标记
        last_success = None
        last_error = None
        
        for line in reversed(lines):
            if "采集完成" in line or "SUCCESS" in line:
                last_success = line.strip()
                break
            if "失败" in line or "ERROR" in line or "FAILED" in line:
                last_error = line.strip()
        
        if last_success:
            return {"status": "✅", "msg": f"最近成功: {last_success[:80]}"}
        elif last_error:
            return {"status": "❌", "msg": f"最近错误: {last_error[:80]}"}
        else:
            return {"status": "⚠️", "msg": "未找到明确的运行状态"}
    
    except Exception as e:
        return {"status": "❌", "msg": f"读取日志失败: {str(e)[:50]}"}


def check_data_freshness():
    """检查数据文件新鲜度"""
    exports_dir = ECHOTIK_EXPORTS
    if not exports_dir.exists():
        return {"status": "⚠️", "msg": f"exports 目录不存在: {exports_dir}"}
    
    try:
        # 找最新的 captured= 目录
        captured_dirs = sorted([d for d in exports_dir.iterdir() if d.is_dir() and d.name.startswith("captured=")])
        if not captured_dirs:
            return {"status": "⚠️", "msg": "没有找到数据目录"}
        
        latest_dir = captured_dirs[-1]
        latest_date = latest_dir.name.replace("captured=", "")
        
        # 检查是否是今天或昨天的数据
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        
        try:
            data_date = datetime.strptime(latest_date, "%Y-%m-%d").date()
            age_days = (today - data_date).days
            
            if age_days == 0:
                status = "✅"
                msg = f"数据最新 (今天 {latest_date})"
            elif age_days == 1:
                status = "✅"
                msg = f"数据较新 (昨天 {latest_date})"
            elif age_days <= 3:
                status = "⚠️"
                msg = f"数据有点旧 ({age_days} 天前: {latest_date})"
            else:
                status = "❌"
                msg = f"数据过旧 ({age_days} 天前: {latest_date})"
            
            return {"status": status, "msg": msg}
        
        except ValueError:
            return {"status": "⚠️", "msg": f"无法解析日期: {latest_date}"}
    
    except Exception as e:
        return {"status": "❌", "msg": f"检查失败: {str(e)[:50]}"}


def check_disk_space():
    """检查磁盘空间"""
    try:
        import shutil
        total, used, free = shutil.disk_usage(SOHO_REPO)
        
        free_gb = free / (1024**3)
        used_percent = (used / total) * 100
        
        if free_gb < 5:
            status = "❌"
            msg = f"磁盘空间不足: 剩余 {free_gb:.1f}GB ({used_percent:.1f}% 已用)"
        elif free_gb < 10:
            status = "⚠️"
            msg = f"磁盘空间偏低: 剩余 {free_gb:.1f}GB ({used_percent:.1f}% 已用)"
        else:
            status = "✅"
            msg = f"磁盘空间充足: 剩余 {free_gb:.1f}GB ({used_percent:.1f}% 已用)"
        
        return {"status": status, "msg": msg}
    
    except Exception as e:
        return {"status": "❌", "msg": f"检查失败: {str(e)[:50]}"}


def check_cron_schedule():
    """检查 crontab 配置"""
    try:
        import subprocess
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)

        if result.returncode != 0:
            return {"status": "❌", "msg": "无法读取 crontab"}

        cron_lines = [
            line for line in result.stdout.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        echotik_lines = [
            line for line in cron_lines
            if any(k in line for k in ("echotik_collector", "echotik_pipeline", "echotik_analyst.py"))
        ]

        if not echotik_lines:
            return {"status": "❌", "msg": "未找到定时任务配置"}

        collector_count = sum(
            any(k in line for k in (" main.py ", " monitor.py ", "cleanup_screenshots"))
            for line in echotik_lines
        )
        analyst_count = sum("echotik_analyst.py" in line for line in echotik_lines)
        other_count = len(echotik_lines) - collector_count - analyst_count
        msg = f"定时任务已配置（共 {len(echotik_lines)} 条：collector {collector_count} / analyst {analyst_count}"
        if other_count > 0:
            msg += f" / other {other_count}"
        msg += "）"
        return {
            "status": "✅",
            "msg": msg
        }

    except Exception as e:
        return {"status": "❌", "msg": f"检查失败: {str(e)[:50]}"}


def check_unarchived_files():
    """检查未归档的采集文件"""
    exports_dir = ECHOTIK_EXPORTS
    if not exports_dir.exists():
        return {"status": "ℹ️", "msg": f"exports 目录不存在，无需归档: {exports_dir}"}
    
    try:
        # 找所有 captured= 目录
        captured_dirs = sorted([d for d in exports_dir.iterdir() 
                               if d.is_dir() and d.name.startswith("captured=")])
        
        if not captured_dirs:
            return {"status": "ℹ️", "msg": "没有待归档文件"}
        
        # 统计每个目录的文件数
        unarchived = []
        total_files = 0
        
        for captured_dir in captured_dirs:
            date_str = captured_dir.name.replace("captured=", "")
            file_count = 0
            
            # 统计 raw, clean, candidates 目录下的文件
            for sub in ["raw", "clean", "candidates"]:
                sub_dir = captured_dir / sub
                if sub_dir.exists():
                    file_count += len([f for f in sub_dir.iterdir() if f.is_file()])
            
            if file_count > 0:
                unarchived.append({"date": date_str, "files": file_count})
                total_files += file_count
        
        if not unarchived:
            return {"status": "✅", "msg": "所有文件已归档"}
        
        # 构建详细信息
        details = ", ".join([f"{item['date']}({item['files']}个)" for item in unarchived[-3:]])  # 只显示最近3个
        if len(unarchived) > 3:
            details += f" 等{len(unarchived)}批"
        
        return {
            "status": "📦",
            "msg": f"有 {total_files} 个文件未归档: {details}",
            "count": total_files,
            "dates": len(unarchived)
        }
    
    except Exception as e:
        return {"status": "❌", "msg": f"检查失败: {str(e)[:50]}"}


def send_report(checks):
    """发送监控报告到飞书"""
    if not FEISHU_WEBHOOK:
        print("未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return
    
    # 判断整体状态
    has_error = any(c["status"] == "❌" for c in checks.values())
    has_warning = any(c["status"] == "⚠️" for c in checks.values())
    archive = checks.get("文件归档状态", {})
    has_unarchived = archive.get("count", 0) > 0

    if has_error:
        title = "❌ Echotik 采集器监控告警"
    elif has_warning:
        title = "⚠️ Echotik 采集器监控警告"
    elif has_unarchived:
        title = f"📦 Echotik 提醒：{archive['count']} 个文件未归档"
    else:
        title = "✅ Echotik 采集器运行正常"
    
    # 构建报告内容
    content_lines = []
    content_lines.append(f"检查时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    for check_name, result in checks.items():
        content_lines.append(f"{result['status']} {check_name}")
        content_lines.append(f"   {result['msg']}\n")
    
    content = "\n".join(content_lines)
    
    # 如果有错误，@维护agent
    message_content = []
    if has_error:
        message_content.append([
            {"tag": "at", "user_id": "ou_e0638b1240e6fe91e2531ceb6ce79286"},
            {"tag": "text", "text": " 采集失败，请诊断\n\n"}
        ])
    message_content.append([{"tag": "text", "text": content}])
    
    try:
        resp = requests.post(
            FEISHU_WEBHOOK,
            json={
                "msg_type": "post",
                "content": {"post": {"zh_cn": {
                    "title": title,
                    "content": message_content,
                }}},
            },
            timeout=10,
        )
        resp.raise_for_status()
        print("✅ 监控报告已发送到飞书")
    except Exception as e:
        print(f"❌ 发送失败: {str(e)[:100]}")


def check_analyst_status():
    """检查 analyst 定时任务最近输出"""
    if not ANALYST_LOG.exists():
        return {"status": "⚠️", "msg": f"analyst 日志不存在: {ANALYST_LOG}"}

    try:
        with open(ANALYST_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()[-200:]

        last_success = None
        last_error = None
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            if "[OK] daily 报告已写入" in line or "[OK] weekly 报告已写入" in line or "[OK] monthly 报告已写入" in line:
                last_success = line
                break
            if "Traceback" in line or "[ERROR]" in line or "执行失败" in line:
                last_error = line

        if last_success:
            return {"status": "✅", "msg": f"最近成功: {last_success[:100]}"}
        if last_error:
            return {"status": "❌", "msg": f"最近错误: {last_error[:100]}"}
        return {"status": "⚠️", "msg": "未找到明确的 analyst 成功/失败标记"}
    except Exception as e:
        return {"status": "❌", "msg": f"读取 analyst 日志失败: {str(e)[:50]}"}


def main():
    """执行所有检查并发送报告"""
    print("开始监控检查...")

    # 基础检查（早晚都执行）
    checks = {
        "最近运行状态": check_last_run(),
        "Analyst 状态": check_analyst_status(),
        "数据新鲜度": check_data_freshness(),
        "磁盘空间": check_disk_space(),
        "定时任务": check_cron_schedule(),
    }
    
    # 晚间巡检增加归档检查（21:00 执行）
    current_hour = datetime.now().hour
    if current_hour >= 20 or current_hour <= 2:  # 20:00-02:00 之间算晚间
        checks["文件归档状态"] = check_unarchived_files()
    
    # 打印到控制台
    print("\n监控结果:")
    for name, result in checks.items():
        print(f"  {result['status']} {name}: {result['msg']}")
    
    # 发送到飞书
    send_report(checks)


if __name__ == "__main__":
    main()
