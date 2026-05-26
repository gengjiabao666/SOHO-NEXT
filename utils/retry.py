#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils/retry.py
异步重试装饰器

功能：
    指数退避重试，适用于网络请求和页面操作。
    第1次失败等 base_delay 秒，第2次等 2*base_delay，以此类推。
    支持指定不重试的异常类型（如订阅到期异常）。
"""

import asyncio
import functools
from typing import Tuple, Type
from utils.logger import log_node


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 30.0,
    no_retry_exceptions: Tuple[Type[Exception], ...] = ()
):
    """
    异步函数重试装饰器

    参数：
        max_attempts: 最大尝试次数（含首次）
        base_delay:   首次重试等待秒数（后续倍增）
        no_retry_exceptions: 不重试的异常类型元组，遇到这些异常直接抛出

    用法：
        @async_retry(max_attempts=3, base_delay=30.0)
        async def my_func():
            ...

        # 指定不重试的异常
        @async_retry(max_attempts=3, no_retry_exceptions=(SubscriptionExpiredError,))
        async def my_func():
            ...
    """
    # 外层：接收装饰器参数，返回真正的装饰器函数
    def decorator(func):
        # 使用 functools.wraps 保留被装饰函数的元信息（函数名、文档字符串等）
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # 记录最后一次异常，用于在所有重试耗尽后抛出
            last_exc = None
            # 从第 1 次到第 max_attempts 次逐一尝试
            for attempt in range(1, max_attempts + 1):
                try:
                    # 尝试执行被装饰的异步函数，成功则直接返回结果
                    return await func(*args, **kwargs)
                except no_retry_exceptions:
                    # 命中不重试的异常类型（如订阅到期），直接向上抛出，不进行重试
                    raise
                except Exception as e:
                    # 其他异常：记录异常，判断是否还有重试机会
                    last_exc = e
                    if attempt < max_attempts:
                        # 还有重试机会：计算指数退避等待时间
                        # 第1次失败等 base_delay 秒，第2次等 2*base_delay，第3次等 4*base_delay...
                        wait = base_delay * (2 ** (attempt - 1))
                        # 记录警告日志，包含函数名、当前尝试次数和错误摘要
                        log_node(
                            f"执行失败，{wait:.0f}秒后重试",
                            level="WARN",
                            func=func.__name__,
                            attempt=f"{attempt}/{max_attempts}",
                            error=str(e)[:80],
                        )
                        # 异步等待指定秒数后再进行下一次尝试
                        await asyncio.sleep(wait)
                    else:
                        # 已达最大重试次数，记录错误日志
                        log_node(
                            "已达最大重试次数，放弃",
                            level="ERROR",
                            func=func.__name__,
                            max_attempts=max_attempts,
                            error=str(e)[:80],
                        )
            # 所有重试均失败，抛出最后一次捕获的异常
            raise last_exc
        return wrapper
    return decorator
