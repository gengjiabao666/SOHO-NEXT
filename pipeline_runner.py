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

# ============================================================
# 标准库导入
# ============================================================
import os            # 用于读取环境变量（PYTHON_BIN、PIPELINE_SCRIPT、REPO_ROOT）
import subprocess    # 用于以子进程方式调用外部 pipeline 脚本
from datetime import date   # 用于获取当前日期作为默认采集日期
from pathlib import Path    # 用于路径存在性检查和路径拼接

# ============================================================
# 项目内部模块导入
# ============================================================
# log_node: 统一日志输出函数，支持 level 和关键字参数
from utils.logger import log_node
# notify_pipeline_failure: 流水线失败时发送飞书告警通知
# send_files_to_feishu: 流水线成功后将导出文件推送到飞书群
from utils.notifier import notify_pipeline_failure, send_files_to_feishu
from utils.events import write_event, STAGE_PIPELINE_TRIGGER


def run_pipeline(captured: str = None) -> bool:
    """
    触发 echotik_pipeline.py 执行

    该函数是清洗流水线的入口，负责：
    1. 确定采集日期（captured 参数或默认今天）
    2. 从环境变量读取 Python 解释器路径、pipeline 脚本路径、数据仓库根目录
    3. 对解释器和脚本路径做前置校验，不存在则提前报错并通知
    4. 以子进程方式执行 pipeline 脚本
    5. 根据子进程返回码判断成功/失败，成功则推送文件到飞书，失败则发送告警

    参数：
        captured (str, 可选): 采集日期字符串，格式为 YYYY-MM-DD。
                              如果未传入或为 None，则自动使用当天日期。

    返回：
        bool: True  = 流水线执行成功（子进程 returncode == 0）
              False = 流水线执行失败（路径校验不通过 或 子进程返回非零退出码）
    """

    # ----------------------------------------------------------
    # 1. 确定采集日期
    #    如果调用方未传入 captured，则使用今天的日期（ISO 格式：YYYY-MM-DD）
    # ----------------------------------------------------------
    captured = captured or date.today().isoformat()

    # ----------------------------------------------------------
    # 2. 从环境变量读取关键配置
    # ----------------------------------------------------------

    # python_bin: conda 虚拟环境内的 python 解释器完整路径
    # 获取方法：在服务器上执行 conda activate echotik && which python
    # 默认值 "python3" 仅作为兜底，生产环境应在 .env 中明确指定
    python_bin = os.getenv("PYTHON_BIN", "python3").strip()

    # pipeline_script: echotik_pipeline.py 脚本的绝对路径
    # 该脚本负责执行 inbox -> raw -> clean -> candidates 的完整数据清洗流程
    pipeline_script = os.getenv(
        "PIPELINE_SCRIPT",
        "/home/gjb/workspace/echotik_pipeline/echotik_pipeline.py",
    ).strip()

    # repo_root: 数据仓库的根目录路径（SOHO_repo）
    # pipeline 脚本会在此目录下读取/写入各阶段的数据文件
    repo_root = os.getenv("REPO_ROOT", "/mnt/g/SOHO_repo").strip()

    # ----------------------------------------------------------
    # 3. 前置检查：验证 Python 解释器路径是否存在
    #    仅当 python_bin 不是默认的 "python3"（即用户显式配置了路径）时才检查
    #    因为 "python3" 依赖 PATH 查找，无法用 Path.exists() 验证
    # ----------------------------------------------------------
    if python_bin != "python3" and not Path(python_bin).exists():
        # 解释器路径不存在，记录错误日志
        log_node("PYTHON_BIN 路径不存在，请检查 .env 配置",
                 level="ERROR", python_bin=python_bin)
        # 给出修复提示，方便运维人员快速定位问题
        log_node("提示：运行 conda activate echotik && which python 获取正确路径",
                 level="ERROR")
        # 发送飞书告警通知，让相关人员及时知晓
        notify_pipeline_failure(captured=captured,
                                stderr=f"PYTHON_BIN 不存在: {python_bin}")
        # 返回 False 表示流水线未能启动
        return False

    # ----------------------------------------------------------
    # 4. 前置检查：验证 pipeline 脚本文件是否存在
    # ----------------------------------------------------------
    script_path = Path(pipeline_script)
    if not script_path.exists():
        # 脚本文件不存在，记录错误日志并发送告警
        log_node("Pipeline 脚本文件不存在，请检查 .env 中的 PIPELINE_SCRIPT",
                 level="ERROR", path=pipeline_script)
        notify_pipeline_failure(captured=captured,
                                stderr=f"脚本不存在: {pipeline_script}")
        # 返回 False 表示流水线未能启动
        return False

    # ----------------------------------------------------------
    # 5. 构建子进程命令行参数列表
    #    等价于在终端执行：
    #    /path/to/conda/python echotik_pipeline.py --captured 2026-03-19 --root /mnt/g/SOHO_repo
    # ----------------------------------------------------------
    cmd = [
        python_bin,        # conda env 内的 python 解释器，而非系统默认的 python3
        pipeline_script,   # 清洗流水线脚本的绝对路径
        "--captured", captured,   # 采集日期参数，pipeline 据此定位对应日期的数据
        "--root", repo_root,      # 数据仓库根目录，pipeline 在此目录下读写文件
    ]

    # ----------------------------------------------------------
    # 6. 记录流水线启动日志
    #    输出采集日期、解释器名称、脚本名称，便于排查问题
    # ----------------------------------------------------------
    log_node("触发清洗流水线", level="START",
             captured=captured,
             python=Path(python_bin).name,    # 仅取文件名，日志更简洁
             script=script_path.name)         # 仅取脚本文件名
    write_event(STAGE_PIPELINE_TRIGGER, "SUCCESS", context={"captured": captured, "python": Path(python_bin).name, "script": script_path.name})

    # ----------------------------------------------------------
    # 7. 以子进程方式执行 pipeline 脚本
    #    capture_output=True: 捕获 stdout 和 stderr，不直接打印到终端
    #    text=True + encoding="utf-8": 以文本模式读取输出，避免中文乱码
    #    cwd=script_path.parent: 将工作目录设为脚本所在目录，
    #        确保脚本内的相对路径引用（如配置文件）能正确解析
    # ----------------------------------------------------------
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(script_path.parent),
    )

    # ----------------------------------------------------------
    # 8. 根据子进程返回码判断执行结果
    # ----------------------------------------------------------
    if result.returncode == 0:
        # ======================================================
        # 8a. 流水线执行成功（returncode == 0）
        # ======================================================

        # 将 stdout 按行分割，用于日志预览
        stdout_lines = result.stdout.strip().splitlines()

        # 只取最后 10 行作为预览，避免日志过长
        # 如果总行数不超过 10 行，则全部显示
        preview = stdout_lines[-10:] if len(stdout_lines) > 10 else stdout_lines

        # 记录成功日志，包含采集日期和输出总行数
        log_node("清洗流水线执行成功", level="INFO",
                 captured=captured, total_lines=len(stdout_lines))

        # 逐行打印预览内容，带 [pipeline] 前缀便于区分
        for line in preview:
            print(f"    [pipeline] {line}")

        # --------------------------------------------------
        # 9. 流水线完成后，将导出文件推送到飞书群
        #    导出目录结构：{repo_root}/03_data_sources/echotik/exports/captured={日期}/
        #    该目录下存放 pipeline 生成的最终结果文件（如 Excel、CSV 等）
        # --------------------------------------------------
        exports_dir = os.path.join(
            repo_root, "03_data_sources", "echotik", "exports",
            f"captured={captured}",   # 按采集日期分目录存放
        )
        # 调用飞书推送函数，将导出文件发送到指定飞书群
        send_files_to_feishu(captured=captured, exports_dir=exports_dir)

        # 返回 True 表示流水线执行成功
        return True
    else:
        # ======================================================
        # 8b. 流水线执行失败（returncode != 0）
        # ======================================================

        # 记录失败日志，包含返回码和 stderr 前 300 字符的预览
        # 截取前 300 字符是为了避免超长错误信息淹没日志
        log_node("清洗流水线执行失败", level="ERROR",
                 captured=captured,
                 returncode=result.returncode,
                 stderr_preview=result.stderr[:300])
        write_event(STAGE_PIPELINE_TRIGGER, "FAILED", context={"captured": captured, "returncode": result.returncode}, detail=result.stderr[:300])

        # 发送飞书告警通知，将完整 stderr 传入，便于排查问题
        notify_pipeline_failure(captured=captured, stderr=result.stderr)

        # 返回 False 表示流水线执行失败
        return False
