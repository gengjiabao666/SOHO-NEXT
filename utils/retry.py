#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/retry.py
异步重试装饰器

功能：
    指数退避重试，适用于网络请求和页面操作。
    第1次失败等 base_delay 秒，第2次等 2*base_delay，以此类推。
"""

import asyncio
import functools
from utils.logger import log_node


def async_retry(max_attempts: int = 3, base_delay: float = 30.0):
    """
    异步函数重试装饰器

    参数：
        max_attempts: 最大尝试次数（含首次）
        base_delay:   首次重试等待秒数（后续倍增）

    用法：
        @async_retry(max_attempts=3, base_delay=30.0)
        async def my_func():
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_attempts:
                        wait = base_delay * (2 ** (attempt - 1))
                        log_node(
                            f"执行失败，{wait:.0f}秒后重试",
                            level="WARN",
                            func=func.__name__,
                            attempt=f"{attempt}/{max_attempts}",
                            error=str(e)[:80],
                        )
                        await asyncio.sleep(wait)
                    else:
                        log_node(
                            "已达最大重试次数，放弃",
                            level="ERROR",
                            func=func.__name__,
                            max_attempts=max_attempts,
                            error=str(e)[:80],
                        )
            raise last_exc
        return wrapper
    return decorator
