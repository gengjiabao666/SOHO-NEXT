#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_router.py
文件路由模块

功能：
    将 inbox/_tmp/ 中下载成功的文件，
    按粒度（d/w/m）移动到 inbox/d/ inbox/w/ inbox/m/
    同名文件自动加时间戳后缀，避免覆盖历史文件。
    status 为 stale 或 failed 的文件留在 _tmp/ 供排查。
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from utils.logger import log_node

load_dotenv()

REPO_ROOT  = os.getenv("REPO_ROOT", "/mnt/g/SOHO_repo")
INBOX_ROOT = Path(REPO_ROOT) / "03_data_sources" / "echotik" / "inbox"


def route_files(results: list) -> dict:
    """
    根据下载结果将文件路由到对应子目录

    参数：
        results: DownloadResult 对象列表，每个包含：
                 .status   "success" / "stale" / "failed"
                 .win      "d" / "w" / "m"
                 .tmp_path 下载到的临时文件路径（Path 对象）
                 .module   模块名称

    返回：
        {"success": [...], "skipped": [...], "failed": [...]}
    """
    summary = {"success": [], "skipped": [], "failed": []}

    for r in results:
        if r.status != "success":
            log_node("跳过路由（非success状态）", level="SKIP",
                     module=r.module, win=r.win, status=r.status)
            summary["skipped"].append(str(r.tmp_path))
            continue

        dest_dir = INBOX_ROOT / r.win
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest_path = dest_dir / r.tmp_path.name
        if dest_path.exists():
            ts = datetime.now().strftime("%H%M%S")
            dest_path = dest_dir / f"{r.tmp_path.stem}_{ts}{r.tmp_path.suffix}"

        try:
            shutil.move(str(r.tmp_path), str(dest_path))
            log_node("文件已路由到 inbox", level="INFO",
                     module=r.module, win=r.win, dest=str(dest_path))
            summary["success"].append(str(dest_path))
        except Exception as e:
            log_node("文件路由失败", level="ERROR",
                     module=r.module, win=r.win, error=str(e))
            summary["failed"].append(str(r.tmp_path))

    return summary
