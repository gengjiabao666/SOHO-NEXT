#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline_runner.py
清洗流水线触发模块

功能：
    下载完成后，以子进程方式调用 echotik_pipeline.py，
    传入 --captured 日期参数，执行 inbox->raw->clean->candidates 全流程。

重要：
    子进程使用 .env 中 PYTHON_BIN 指定的解释器（conda env 内的 python），
    确保 pipeline 能找到 pandas、openpyxl 等依赖库。
    不使用系统 python3，避免依赖缺失报错。
"""

import os
import subprocess
from datetime import date
from pathlib import Path

from utils.logger import log_node
from utils.notifier import notify_pipeline_failure, send_files_to_feishu


def run_pipeline(captured: str = None) -> bool:
    """
    触发 echotik_pipeline.py 执行

    参数：
        captured: 采集日期字符串（YYYY-MM-DD），默认今天

    返回：
        True  = 流水线执行成功（returncode=0）
        False = 流水线执行失败
    """
    captured = captured or date.today().isoformat()

    # conda env 内的 python 解释器完整路径
    # 获取方法：conda activate echotik && which python
    python_bin = os.getenv("PYTHON_BIN", "python3").strip()

    pipeline_script = os.getenv(
        "PIPELINE_SCRIPT",
        "/home/gjb/workspace/echotik_pipeline.py",
    ).strip()

    repo_root = os.getenv("REPO_ROOT", "/mnt/g/SOHO_repo").strip()

    # 前置检查：python 解释器路径
    if python_bin != "python3" and not Path(python_bin).exists():
        log_node("PYTHON_BIN 路径不存在，请检查 .env 配置",
                 level="ERROR", python_bin=python_bin)
        log_node("提示：运行 conda activate echotik && which python 获取正确路径",
                 level="ERROR")
        notify_pipeline_failure(captured=captured,
                                stderr=f"PYTHON_BIN 不存在: {python_bin}")
        return False

    # 前置检查：pipeline 脚本
    script_path = Path(pipeline_script)
    if not script_path.exists():
        log_node("Pipeline 脚本文件不存在，请检查 .env 中的 PIPELINE_SCRIPT",
                 level="ERROR", path=pipeline_script)
        notify_pipeline_failure(captured=captured,
                                stderr=f"脚本不存在: {pipeline_script}")
        return False

    cmd = [
        python_bin,        # conda env 内的 python，而非系统 python3
        pipeline_script,
        "--captured", captured,
        "--root", repo_root,
    ]

    log_node("触发清洗流水线", level="START",
             captured=captured,
             python=Path(python_bin).name,
             script=script_path.name)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(script_path.parent),
    )

    if result.returncode == 0:
        stdout_lines = result.stdout.strip().splitlines()
        preview = stdout_lines[-10:] if len(stdout_lines) > 10 else stdout_lines
        log_node("清洗流水线执行成功", level="INFO",
                 captured=captured, total_lines=len(stdout_lines))
        for line in preview:
            print(f"    [pipeline] {line}")

        # pipeline 完成后，推送文件到飞书群
        exports_dir = os.path.join(
            repo_root, "03_data_sources", "echotik", "exports",
            f"captured={captured}",
        )
        send_files_to_feishu(captured=captured, exports_dir=exports_dir)

        return True
    else:
        log_node("清洗流水线执行失败", level="ERROR",
                 captured=captured,
                 returncode=result.returncode,
                 stderr_preview=result.stderr[:300])
        notify_pipeline_failure(captured=captured, stderr=result.stderr)
        return False
