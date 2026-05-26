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
    """
    计算文件 MD5 哈希值

    参数：
        path: 文件路径

    返回：
        文件内容的 MD5 十六进制摘要字符串

    说明：
        采用分块读取方式（每次 8192 字节），避免大文件一次性加载到内存
    """
    # 创建 MD5 哈希对象
    h = hashlib.md5()
    # 以二进制模式打开文件
    with open(path, "rb") as f:
        # 分块读取文件内容，每次读取 8192 字节，直到读完（返回空字节串 b""）
        for chunk in iter(lambda: f.read(8192), b""):
            # 将每个数据块更新到哈希对象中
            h.update(chunk)
    # 返回最终的 MD5 十六进制摘要
    return h.hexdigest()


def find_latest_raw_dir(base_exports: Path, exclude_captured: str) -> Optional[Path]:
    """
    在 exports/ 中找到最近一个（排除当前 captured）的 raw 目录

    参数：
        base_exports:     exports/ 根目录路径
        exclude_captured: 当前采集日期（YYYY-MM-DD），需要排除的目录

    返回：
        最近一次历史采集的 raw 目录路径，若不存在则返回 None

    说明：
        exports 目录结构为 exports/captured=YYYY-MM-DD/raw/
        按目录名倒序排列，找到第一个不是当前采集日期且包含 raw 子目录的即可
    """
    # 查找所有 captured=* 格式的子目录，按名称倒序排列（最新日期在前）
    candidates = sorted(
        [d for d in base_exports.glob("captured=*") if d.is_dir()],
        reverse=True,
    )
    # 遍历候选目录，跳过当前采集日期的目录
    for d in candidates:
        if d.name == f"captured={exclude_captured}":
            # 跳过当前采集日期对应的目录
            continue
        raw_dir = d / "raw"
        # 检查该历史目录下是否存在 raw 子目录
        if raw_dir.exists():
            return raw_dir
    # 没有找到任何历史 raw 目录
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
        win:              粒度（d=日榜, w=周榜, m=月榜）
        ds:               数据源标识（p=商品榜，s=小店榜）

    返回：
        True  = 数据已更新，可继续处理
        False = 数据未变化，需等待重试

    逻辑：
        1. 查找最近一次历史采集的 raw 目录
        2. 若无历史目录，视为首次采集，直接返回 True
        3. 计算新文件的 MD5，与历史同类文件逐一对比
        4. 若找到 MD5 相同的文件，说明数据未更新，返回 False
        5. 所有历史文件 MD5 均不同，说明数据已更新，返回 True
    """
    # 查找最近一次历史采集的 raw 目录（排除当前采集日期）
    prev_raw = find_latest_raw_dir(base_exports, current_captured)

    # 没有历史文件，视为首次采集，数据一定是"新鲜"的
    if prev_raw is None:
        log_node("未找到历史文件，视为首次采集", level="INFO", win=win, ds=ds)
        return True

    # 计算新下载文件的 MD5 值
    new_md5 = file_md5(new_file)
    log_node("开始新鲜度对比", level="INFO",
             win=win, ds=ds, prev_raw=str(prev_raw))

    # 构造文件名匹配模式，如 "et_p_d_*.xlsx"，匹配同数据源、同粒度的历史文件
    pattern = f"et_{ds}_{win}_*.xlsx"
    # 遍历历史 raw 目录中匹配的文件，逐一对比 MD5
    for prev_file in prev_raw.glob(pattern):
        if file_md5(prev_file) == new_md5:
            # MD5 相同，说明 Echotik 平台数据尚未更新，需要等待后重试
            log_node("MD5相同，数据未更新", level="WARN",
                     win=win, new=new_file.name, matched=prev_file.name)
            return False

    # 所有历史文件的 MD5 均不同，说明数据已更新
    log_node("MD5不同，数据已更新", level="INFO", win=win, ds=ds)
    return True
