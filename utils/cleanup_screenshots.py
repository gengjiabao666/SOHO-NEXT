#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/cleanup_screenshots.py
截图清理工具

策略：
    每周日：logs/*.png → logs/_trash/
    每季度首日（1/1, 4/1, 7/1, 10/1）：清空 logs/_trash/

用法：
    python -m utils.cleanup_screenshots          # 自动判断今天该做什么
    python -m utils.cleanup_screenshots --move    # 强制执行转移
    python -m utils.cleanup_screenshots --purge   # 强制执行清空垃圾桶

crontab 示例（每天 00:05 运行，脚本内部判断周几/几号）：
    5 0 * * * /path/to/python -m utils.cleanup_screenshots >> logs/cleanup.log 2>&1
"""

import argparse
import shutil
from datetime import date
from pathlib import Path


# 日志目录路径（截图存放在此目录下）
LOG_DIR   = Path("logs")
# 垃圾桶目录路径（截图转移的中间站，定期清空）
TRASH_DIR = LOG_DIR / "_trash"

# 季度首日集合：1月1日、4月1日、7月1日、10月1日
# 用于判断是否需要执行垃圾桶清空操作
QUARTER_STARTS = {(1, 1), (4, 1), (7, 1), (10, 1)}


def move_to_trash():
    """
    将 logs/*.png 转移到 logs/_trash/

    说明：
        - 扫描 logs 目录下所有 .png 截图文件
        - 将它们移动到 logs/_trash/ 子目录中
        - 同名文件直接覆盖（调试截图不需要保留多份）
        - 若无截图则跳过
    """
    # 收集 logs 目录下所有 .png 文件
    pngs = list(LOG_DIR.glob("*.png"))
    # 没有截图需要转移，直接返回
    if not pngs:
        print("无截图需要转移")
        return
    # 确保垃圾桶目录存在
    TRASH_DIR.mkdir(exist_ok=True)
    # 逐个移动截图到垃圾桶
    for p in pngs:
        dest = TRASH_DIR / p.name
        # 同名文件直接覆盖（都是调试截图，不需要保留多份）
        shutil.move(str(p), str(dest))
    print(f"已转移 {len(pngs)} 张截图到 {TRASH_DIR}")


def purge_trash():
    """
    清空 logs/_trash/ 中的 png 文件

    说明：
        - 仅删除垃圾桶中的 .png 文件
        - 垃圾桶目录不存在或为空时跳过
        - 在季度首日调用，彻底释放磁盘空间
    """
    # 垃圾桶目录不存在，无需清理
    if not TRASH_DIR.exists():
        print("垃圾桶不存在，跳过")
        return
    # 收集垃圾桶中所有 .png 文件
    pngs = list(TRASH_DIR.glob("*.png"))
    # 垃圾桶为空，无需清理
    if not pngs:
        print("垃圾桶为空")
        return
    # 逐个删除截图文件
    for p in pngs:
        p.unlink()
    print(f"已删除垃圾桶中 {len(pngs)} 张截图")


def main():
    """
    主入口函数

    支持两种运行模式：
        1. 手动模式：通过 --move 或 --purge 参数强制执行指定操作
        2. 自动模式：根据当前日期自动判断需要执行的操作
           - 季度首日（1/1, 4/1, 7/1, 10/1）：清空垃圾桶
           - 周日：将截图转移到垃圾桶
           - 其他日期：不执行任何操作
    """
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="截图清理工具")
    parser.add_argument("--move", action="store_true", help="强制转移截图到垃圾桶")
    parser.add_argument("--purge", action="store_true", help="强制清空垃圾桶")
    args = parser.parse_args()

    # 手动模式：--move 强制转移截图
    if args.move:
        move_to_trash()
        return
    # 手动模式：--purge 强制清空垃圾桶
    if args.purge:
        purge_trash()
        return

    # 自动模式：根据当前日期判断操作
    today = date.today()

    # 季度首日：先清空垃圾桶，释放磁盘空间
    if (today.month, today.day) in QUARTER_STARTS:
        print(f"季度首日 {today}，清空垃圾桶")
        purge_trash()

    # 周日：将 logs 目录下的截图转移到垃圾桶
    if today.weekday() == 6:  # 0=周一, 6=周日
        print(f"周日 {today}，转移截图到垃圾桶")
        move_to_trash()

    # 既不是季度首日也不是周日，今天无需清理
    if today.weekday() != 6 and (today.month, today.day) not in QUARTER_STARTS:
        print(f"今天 {today} 无需清理")


# 脚本直接运行时的入口
if __name__ == "__main__":
    main()
