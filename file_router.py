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

# ============================================================
# 标准库导入
# ============================================================
import os          # 用于读取环境变量
import shutil      # 用于移动文件（跨文件系统也能工作）
from datetime import datetime  # 用于生成时间戳，防止同名文件覆盖
from pathlib import Path       # 面向对象的路径操作

# ============================================================
# 第三方库导入
# ============================================================
from dotenv import load_dotenv      # 从 .env 文件加载环境变量
from utils.logger import log_node   # 项目自定义的日志工具，按节点记录日志

# 加载 .env 文件中的环境变量到 os.environ，使后续 os.getenv 能读取到
load_dotenv()

# ============================================================
# 全局常量：路径配置
# ============================================================
# REPO_ROOT: 数据仓库根目录，优先从环境变量读取，缺省值为 /mnt/g/SOHO_repo
REPO_ROOT  = os.getenv("REPO_ROOT", "/mnt/g/SOHO_repo")
# INBOX_ROOT: echotik 数据收件箱根目录，所有下载文件最终路由到此目录下的子文件夹
INBOX_ROOT = Path(REPO_ROOT) / "03_data_sources" / "echotik" / "inbox"


def route_files(results: list) -> dict:
    """
    根据下载结果将文件路由到对应子目录。

    遍历所有下载结果，将状态为 "success" 的文件从临时目录移动到
    按时间粒度（日/周/月）和品类组织的目标目录中。
    非 success 状态的文件（stale/failed）保留在临时目录，不做移动。

    参数：
        results: DownloadResult 对象列表，每个包含：
                 .status   "success" / "stale" / "failed"  — 下载状态
                 .win      "d" / "w" / "m"                  — 时间粒度（日/周/月）
                 .tmp_path 下载到的临时文件路径（Path 对象）
                 .module   模块名称（用于日志标识）
                 .category 品类（空字符串表示全品类）

    返回：
        {"success": [...], "skipped": [...], "failed": [...]}
        - success:  成功路由的目标文件路径列表
        - skipped:  因非 success 状态而跳过的临时文件路径列表
        - failed:   路由过程中发生异常的临时文件路径列表

    路由规则：
        全品类：inbox/d/xxx.xlsx          — 直接放在粒度目录下
        Pet Supplies：inbox/d/pet_supplies/xxx.xlsx  — 放在品类子目录下
    """
    # 初始化汇总字典，用于统计本次路由的结果
    summary = {"success": [], "skipped": [], "failed": []}

    # 逐条遍历下载结果
    for r in results:
        # --- 跳过非 success 状态的记录 ---
        # stale（数据过期）或 failed（下载失败）的文件留在 _tmp/ 目录供人工排查
        if r.status != "success":
            log_node("跳过路由（非success状态）", level="SKIP",
                     module=r.module, win=r.win, status=r.status)
            # 将跳过的文件路径记录到 skipped 列表
            summary["skipped"].append(str(r.tmp_path))
            continue  # 跳过本条，处理下一条

        # --- 确定目标目录 ---
        # 基础目标目录：inbox/{粒度}/，例如 inbox/d/、inbox/w/、inbox/m/
        dest_dir = INBOX_ROOT / r.win

        # --- 处理品类子目录 ---
        # 如果下载结果指定了品类（非空字符串），则在粒度目录下再建品类子目录
        category = getattr(r, "category", "")  # 安全获取 category 属性，缺省为空串
        if category:
            # 将品类名标准化为目录名格式：全部小写，空格替换为下划线
            # 例如 "Pet Supplies" -> "pet_supplies"
            category_dir = category.lower().replace(" ", "_")
            dest_dir = dest_dir / category_dir

        # 递归创建目标目录（如果不存在），exist_ok=True 表示目录已存在时不报错
        dest_dir.mkdir(parents=True, exist_ok=True)

        # --- 构造目标文件路径，处理同名冲突 ---
        # 默认使用原始文件名
        dest_path = dest_dir / r.tmp_path.name
        if dest_path.exists():
            # 如果目标路径已存在同名文件，在文件名末尾追加当前时间戳（时分秒）
            # 例如 report.xlsx -> report_143052.xlsx，避免覆盖历史文件
            ts = datetime.now().strftime("%H%M%S")
            dest_path = dest_dir / f"{r.tmp_path.stem}_{ts}{r.tmp_path.suffix}"

        # --- 执行文件移动 ---
        try:
            # 使用 shutil.move 将文件从临时目录移动到目标目录
            # 转为 str 是为了兼容旧版 Python（3.8 以下 shutil.move 不支持 Path 对象）
            shutil.move(str(r.tmp_path), str(dest_path))
            # 移动成功，记录日志
            log_node("文件已路由到 inbox", level="INFO",
                     module=r.module, win=r.win,
                     category=category or "全品类", dest=str(dest_path))
            # 将成功路由的目标路径加入 success 列表
            summary["success"].append(str(dest_path))
        except Exception as e:
            # 移动失败（权限不足、磁盘满、路径不存在等），记录错误日志
            log_node("文件路由失败", level="ERROR",
                     module=r.module, win=r.win, error=str(e))
            # 将失败的临时文件路径加入 failed 列表，便于后续重试或人工处理
            summary["failed"].append(str(r.tmp_path))

    # 返回本次路由的汇总结果
    return summary
