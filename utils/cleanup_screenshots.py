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


LOG_DIR   = Path("logs")
TRASH_DIR = LOG_DIR / "_trash"

# 季度首日：1月1日、4月1日、7月1日、10月1日
QUARTER_STARTS = {(1, 1), (4, 1), (7, 1), (10, 1)}


def move_to_trash():
    """将 logs/*.png 转移到 logs/_trash/"""
    pngs = list(LOG_DIR.glob("*.png"))
    if not pngs:
        print("无截图需要转移")
        return
    TRASH_DIR.mkdir(exist_ok=True)
    for p in pngs:
        dest = TRASH_DIR / p.name
        # 同名文件直接覆盖（都是调试截图，不需要保留多份）
        shutil.move(str(p), str(dest))
    print(f"已转移 {len(pngs)} 张截图到 {TRASH_DIR}")


def purge_trash():
    """清空 logs/_trash/ 中的 png"""
    if not TRASH_DIR.exists():
        print("垃圾桶不存在，跳过")
        return
    pngs = list(TRASH_DIR.glob("*.png"))
    if not pngs:
        print("垃圾桶为空")
        return
    for p in pngs:
        p.unlink()
    print(f"已删除垃圾桶中 {len(pngs)} 张截图")


def main():
    parser = argparse.ArgumentParser(description="截图清理工具")
    parser.add_argument("--move", action="store_true", help="强制转移截图到垃圾桶")
    parser.add_argument("--purge", action="store_true", help="强制清空垃圾桶")
    args = parser.parse_args()

    if args.move:
        move_to_trash()
        return
    if args.purge:
        purge_trash()
        return

    # 自动判断
    today = date.today()

    # 季度首日：先清空垃圾桶
    if (today.month, today.day) in QUARTER_STARTS:
        print(f"季度首日 {today}，清空垃圾桶")
        purge_trash()

    # 周日：转移截图
    if today.weekday() == 6:  # 0=周一, 6=周日
        print(f"周日 {today}，转移截图到垃圾桶")
        move_to_trash()

    if today.weekday() != 6 and (today.month, today.day) not in QUARTER_STARTS:
        print(f"今天 {today} 无需清理")


if __name__ == "__main__":
    main()
