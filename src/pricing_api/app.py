"""FastAPI 应用装配。

用工厂函数 `create_app(settings)` 而不是模块级 `app = FastAPI()`，原因很实际：
测试需要注入临时数据库和测试密钥，而模块级单例只能靠改环境变量 + reload 模块，
既慢又容易互相污染。工厂函数让每个测试拿到干净的应用实例。

启动顺序也刻意固定：配置 → 日志 → 数据库 → 路由。
配错了在第一步就崩，不会出现"服务起来了但审计表不存在"这种半死状态。

注意：本文件**刻意不使用** `from __future__ import annotations`。
原因：FastAPI 需要在运行时真正拿到 `Annotated[..., Depends(...)]` 对象，
而延迟注解会把它们变成字符串，FastAPI 只会在**模块全局命名空间**里求值；
`require_principal` / `bearer` 是 create_app 的闭包变量，全局里根本不存在，
结果依赖被当成普通查询参数（报 `loc: ["query", "principal"]` 的 422）。
Python 3.13 原生支持 `X | None`，不需要这个 future 导入。
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pricing.core import PricingError, calculate_breakdown
from pricing_api.audit import AuditRecord, AuditStore
from pricing_api.logging_setup import configure_logging
from pricing_api.models import HealthResponse, QuoteRequest, QuoteResponse
from pricing_api.security import Principal, TokenError, decode_token
from pricing_api.settings import Settings
from pricing_api.trace import current_trace_id, reset_trace_id, resolve_trace_id, set_trace_id

logger = logging.getLogger("pricing_api")

_TRACE_HEADER = "X-Request-Id"


def format_money(value: Decimal) -> str:
    """金额出站：定点十进制字符串，永不出现科学计数法，永不经过 float。

    `f"{value:f}"` 对 Decimal 是"定点格式"；`calculate_total` 返回的 total 已经
    quantize 到分，所以这里天然就是 2 位小数（`"41500.00"` 而不是 `41500` 或 `4.15E+4`）。
    """
    return f"{value:f}"


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配应用。传入 settings 便于测试；不传则从环境变量读（生产路径）。"""
    config = settings or Settings.from_env()

    # 1. 日志：先配好，后面任何一步出错都能被结构化记录。
    configure_logging(config.log_level)
    logger.info("app.starting", extra={"version": config.app_version})

    # 2. 数据库：建表是幂等的，容器重启不会丢数据。
    store = AuditStore(config.database_url)
    store.init()
    logger.info("audit.ready", extra={"database_path": str(store.path)})

    app = FastAPI(
        title="Pricing Quote API",
        version=config.app_version,
        docs_url="/docs",
        redoc_url=None,
    )
    # 测试要拿 store 查审计行，也方便运维在 shell 里排查。
    app.state.settings = config
    app.state.store = store

    bearer = HTTPBearer(auto_error=False)

    async def require_principal(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        """认证依赖。`auto_error=False` 是为了自己决定 401 的错误文案与响应头。"""
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return decode_token(credentials.credentials, config)
        except TokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @app.middleware("http")
    async def trace_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """给每个请求建立独立的 trace 上下文，并回显 X-Request-Id。

        contextvar 的 set/reset 必须成对，且 reset 放 finally：中间件跑完后如果忘了还原，
        这个 worker task 复用时会把上一个请求的 trace_id 带进下一个请求。
        """
        trace_id = resolve_trace_id(request.headers.get(_TRACE_HEADER))
        token = set_trace_id(trace_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # 兜底：未预期异常也必须带 trace_id 返回，否则客户报障时我们无法定位。
            # 注意不把堆栈放进响应体，只写日志。
            logger.exception("http.error", extra={"http_path": request.url.path})
            response = JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "internal error", "traceId": trace_id},
            )
        else:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.info(
                "http.request",
                extra={
                    "http_method": request.method,
                    "http_path": request.url.path,
                    "http_status": response.status_code,
                    "duration_ms": elapsed_ms,
                },
            )
        finally:
            reset_trace_id(token)

        # 回显放在 finally 之后：用局部变量 trace_id，不依赖 contextvar 是否已还原。
        response.headers[_TRACE_HEADER] = trace_id
        return response

    @app.get("/health")
    async def health() -> HealthResponse:
        """存活探针。故意不查数据库：探针抖动会引发编排系统反复重启容器。"""
        return HealthResponse(status="ok", version=config.app_version)

    @app.post("/v1/quote", response_model=QuoteResponse)
    async def create_quote(
        payload: QuoteRequest,
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> QuoteResponse:
        order = payload.to_order()

        # 业务错误 → 400，透传计价库原文（那句话就是给经销商看的）。
        try:
            breakdown = calculate_breakdown(order)
        except PricingError as exc:
            logger.info(
                "quote.rejected",
                extra={"order_id": order.order_id, "reason": str(exc)},
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        trace_id = current_trace_id()
        record = AuditRecord(
            order_id=order.order_id,
            tier=payload.tier.value,
            subtotal=format_money(breakdown.subtotal),
            total=format_money(breakdown.total),
            trace_id=trace_id,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

        # 审计是客户的硬要求（"出了问题要能查到哪一单"），所以它是**强一致**的：
        # 写不进去就返回 500，绝不能"报价成功但没留痕"。
        try:
            await asyncio.to_thread(store.insert, record)
        except sqlite3.Error as exc:
            logger.exception("audit.write_failed", extra={"order_id": order.order_id})
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="internal error",
            ) from exc

        logger.info(
            "quote.calculated",
            extra={
                "order_id": order.order_id,
                "tier": record.tier,
                "subtotal": record.subtotal,
                "total": record.total,
                "principal": principal.subject,
            },
        )
        return QuoteResponse(order_id=order.order_id, total=record.total, trace_id=trace_id)

    return app
