#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/freshness.py
文件新鲜度检测模块

功能：
    下载完成后，将新文件与历史 raw 目录中的同类文件进行 MD5 对比。
    MD5 相同说明数据未更新（stale），需要等待后重试。
    MD5 不同或无历史文件说明数据已更新（fresh），可以继续处理。
"""

import hashlib
from pathlib import Path
from typing import Optional

from utils.logger import log_node


def file_md5(path: Path) -> str:
    """计算文件 MD5 哈希值"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def find_latest_raw_dir(base_exports: Path, exclude_captured: str) -> Optional[Path]:
    """在 exports/ 中找到最近一个（排除当前 captured）的 raw 目录"""
    candidates = sorted(
        [d for d in base_exports.glob("captured=*") if d.is_dir()],
        reverse=True,
    )
    for d in candidates:
        if d.name == f"captured={exclude_captured}":
            continue
        raw_dir = d / "raw"
        if raw_dir.exists():
            return raw_dir
    return None


def is_fresh(
    new_file: Path,
    base_exports: Path,
    current_captured: str,
    win: str,
    ds: str,
) -> bool:
    """
    判断下载的文件是否包含新数据

    参数：
        new_file:         新下载的文件路径
        base_exports:     exports/ 根目录
        current_captured: 当前采集日期（YYYY-MM-DD）
        win:              粒度（d/w/m）
        ds:               数据源标识（p=商品榜，s=小店榜）

    返回：
        True  = 数据已更新，可继续处理
        False = 数据未变化，需等待重试
    """
    prev_raw = find_latest_raw_dir(base_exports, current_captured)

    if prev_raw is None:
        log_node("未找到历史文件，视为首次采集", level="INFO", win=win, ds=ds)
        return True

    new_md5 = file_md5(new_file)
    log_node("开始新鲜度对比", level="INFO",
             win=win, ds=ds, prev_raw=str(prev_raw))

    pattern = f"et_{ds}_{win}_*.xlsx"
    for prev_file in prev_raw.glob(pattern):
        if file_md5(prev_file) == new_md5:
            log_node("MD5相同，数据未更新", level="WARN",
                     win=win, new=new_file.name, matched=prev_file.name)
            return False

    log_node("MD5不同，数据已更新", level="INFO", win=win, ds=ds)
    return True
