#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py
Echotik 自动采集器主入口

使用方式：
    python main.py                          # 正常运行（今天）
    python main.py --captured 2026-03-01    # 指定采集日期
    python main.py --wins d,w               # 强制指定粒度
    python main.py --dry-run                # 演练模式（不实际下载）

定时任务（crontab）：
    # 直接使用 conda env 的完整 python 路径，无需 conda activate
    30 7 * * * /home/gjb/miniconda3/envs/echotik_exporter/bin/python /path/to/main.py >> logs/cron.log 2>&1
"""

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

from browser.session import BrowserSession
from browser.downloader import download_all, download_all_v2
from file_router import route_files
from pipeline_runner import run_pipeline
from scheduler.trigger import get_tasks_for_today, get_detailed_tasks_for_today, run_with_retry, run_with_retry_v2
from utils.logger import log_node, setup_logger

load_dotenv()


def proxy_on():
    """
    脚本启动时自动执行代理设置，等价于手动在终端运行 proxy_on。
    只要 v2rayN 在 Windows 侧是开着的，此函数会自动找到宿主机 IP 并设置代理。
    逻辑完整移植自 ~/.bashrc 的 proxy_on / wsl_win_host_ip 函数。

    优先级：
      1. 终端已 export https_proxy（手动 proxy_on 过）→ 直接复用，不覆盖
      2. .env 中已配置 PLAYWRIGHT_PROXY（写死完整地址）→ 直接复用，不覆盖
      3. 自动探测：/proc/net/route 读取网关 IP（同 wsl_win_host_ip 方法1）
                   → 失败则用 /etc/resolv.conf nameserver（方法2）
                   → 检测端口可达性（同 _proxy_tcp_ok）
                   → 设置全部代理环境变量（同 proxy_on 的 export 语句）
      4. 端口不可达 → 打印警告，提示确认 v2rayN 已运行并开启允许局域网
    """
    import socket

    # 优先级1：终端已手动 proxy_on
    if os.getenv("https_proxy") or os.getenv("HTTPS_PROXY"):
        log_node("检测到已有代理环境变量，跳过自动设置", level="INFO",
                 proxy=os.getenv("https_proxy") or os.getenv("HTTPS_PROXY"))
        return

    # 优先级2：.env 中写死了完整地址
    if os.getenv("PLAYWRIGHT_PROXY"):
        log_node("使用 .env 中的 PLAYWRIGHT_PROXY", level="INFO",
                 proxy=os.getenv("PLAYWRIGHT_PROXY"))
        return

    # 读取端口（.env 中的 PROXY_PORT，默认 10808）
    port_str = os.getenv("PROXY_PORT", "10808").strip()
    try:
        port = int(port_str)
    except ValueError:
        log_node("PROXY_PORT 格式错误，应为纯数字", level="WARN", value=port_str)
        return

    # 优先级3：自动探测 Windows 宿主机 IP
    # 方法1：/proc/net/route（同 wsl_win_host_ip，最准确）
    host_ip = ""
    try:
        with open("/proc/net/route") as f:
            for line in f:
                parts = line.strip().split()
                if (len(parts) >= 3
                        and parts[1] == "00000000"
                        and parts[2] != "00000000"):
                    gw_hex = parts[2]
                    host_ip = ".".join(
                        str(int(gw_hex[i:i+2], 16)) for i in (6, 4, 2, 0)
                    )
                    break
    except Exception:
        pass

    # 方法2：/etc/resolv.conf nameserver（兜底）
    if not host_ip:
        try:
            with open("/etc/resolv.conf") as f:
                for line in f:
                    p = line.strip().split()
                    if p and p[0] == "nameserver":
                        host_ip = p[1]
                        break
        except Exception:
            pass

    if not host_ip:
        log_node("无法获取 Windows 宿主机 IP，跳过代理设置", level="WARN",
                 hint="请手动运行 proxy_on 后再启动脚本")
        return

    # 检测端口是否可达（同 _proxy_tcp_ok）
    try:
        with socket.create_connection((host_ip, port), timeout=2):
            pass
    except OSError:
        log_node("代理端口不可达，请确认 v2rayN 正在运行并已开启「允许局域网」",
                 level="WARN", windows_ip=host_ip, port=port)
        return

    # 设置全部代理环境变量（同 proxy_on 的 export 语句）
    proxy_url = f"http://{host_ip}:{port}"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "PLAYWRIGHT_PROXY"):
        os.environ[key] = proxy_url
    no_proxy = "localhost,127.0.0.1,::1,.local"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy

    log_node("代理已自动设置（等价于 proxy_on）",
             level="INFO", windows_ip=host_ip, port=port, proxy=proxy_url)


def parse_args():
    parser = argparse.ArgumentParser(description="Echotik 自动采集器")
    parser.add_argument(
        "--captured",
        type=str,
        default=date.today().isoformat(),
        help="采集日期 YYYY-MM-DD（默认今天）",
    )
    parser.add_argument(
        "--wins",
        type=str,
        default=None,
        help="强制指定粒度，逗号分隔：d,w,m（默认按日期自动判断）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练模式：打印将要执行的任务，不实际下载",
    )
    parser.add_argument(
        "--module",
        type=str,
        default="",
        help="只运行指定模块（如 '小店榜'），默认全部",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    captured = args.captured
    dry_run = args.dry_run

    setup_logger(captured)

    # 最先执行：自动设置代理（等价于手动 proxy_on），必须在浏览器启动前完成
    proxy_on()

    log_node(
        "Echotik 自动采集器启动",
        level="START",
        captured=captured,
        dry_run=dry_run,
        python=sys.executable,
    )

    # 确定本次需要下载的任务
    if args.wins:
        # 手动指定粒度，使用旧接口（全品类）
        wins = [w.strip() for w in args.wins.split(",") if w.strip() in ("d", "w", "m")]
        if not wins:
            log_node("今天没有需要执行的任务", level="INFO")
            return
        log_node("本次任务（手动指定）", level="INFO", wins=wins, captured=captured)
        use_v2 = False
    else:
        # 自动调度，使用新接口（支持品类筛选）
        tasks = get_detailed_tasks_for_today(captured)
        if not tasks:
            log_node("今天没有需要执行的任务", level="INFO")
            return
        use_v2 = True

    # 演练模式
    if dry_run:
        if use_v2:
            for t in tasks:
                cat_label = t["category"] if t["category"] else "全品类"
                log_node("[DRY-RUN] 将下载", level="INFO",
                         module=t["module"], win=t["win"], category=cat_label)
        else:
            tasks_yaml = os.getenv("TASKS_YAML", "config/tasks.yaml")
            with open(tasks_yaml, encoding="utf-8") as f:
                config = yaml.safe_load(f)
            for module in config["modules"]:
                for win in wins:
                    if win in module.get("wins", []):
                        log_node("[DRY-RUN] 将下载", level="INFO",
                                 module=module["name"], win=win)
        log_node("演练完成，退出", level="DONE")
        return

    # 正式运行
    if use_v2:
        success = await run_with_retry_v2(
            tasks=tasks,
            captured=captured,
            download_fn=download_all_v2,
            route_fn=route_files,
            pipeline_fn=run_pipeline,
        )
    else:
        success = await run_with_retry(
            wins=wins,
            captured=captured,
            download_fn=download_all,
            route_fn=route_files,
            pipeline_fn=run_pipeline,
            module_filter=args.module,
        )

    if success:
        log_node("本日采集全部完成", level="DONE", captured=captured)
    else:
        log_node("本日采集存在失败，已发送报警通知", level="ERROR", captured=captured)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
