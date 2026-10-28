"""请求追踪上下文。

为什么不用模块级全局变量 `CURRENT_TRACE_ID = "..."`？

FastAPI/Starlette 在**一个事件循环**里并发处理多个请求。请求 A 设置全局变量后 `await`
让出，请求 B 立刻把它改掉，A 恢复执行时读到的就是 B 的 trace_id —— 日志"串单"。
而且这个 bug 只在并发时出现，本地单请求 curl 永远复现不了。

`contextvars.ContextVar` 由 asyncio 的 Task 在创建时**复制上下文**，天然隔离每个请求，
并且能穿透 `await`（同一上下文链上取值一致）。这是官方给异步场景准备的"线程局部变量"。
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from uuid import uuid4

# 默认值 "-" 用于"还没有请求"的场景，比如进程启动日志。
_TRACE_ID: ContextVar[str] = ContextVar("trace_id", default="-")

# 只允许安全字符：防止调用方通过 X-Request-Id 注入换行/引号来伪造日志行。
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def current_trace_id() -> str:
    """读取当前请求的 trace_id（日志 formatter 与响应体都靠它）。"""
    return _TRACE_ID.get()


def set_trace_id(value: str) -> Token[str]:
    """写入当前上下文，返回 token 供 reset 使用。"""
    return _TRACE_ID.set(value)


def reset_trace_id(token: Token[str]) -> None:
    """还原上下文。必须放在 finally 里，否则异常路径会污染复用的 task 上下文。"""
    _TRACE_ID.reset(token)


def resolve_trace_id(header_value: str | None) -> str:
    """确定本次请求的 trace_id：优先沿用调用方带来的合法值，否则生成 UUID4。

    沿用（而不是永远新生成）是客户排查的基础：经销商把他们的请求号给我们，
    我们回显同一个值，两边日志才能对上。
    """
    if header_value is not None:
        candidate = header_value.strip()
        if _SAFE_TRACE_ID.fullmatch(candidate):
            return candidate
    return uuid4().hex
