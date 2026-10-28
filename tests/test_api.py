"""HTTP 层测试 —— 契约（docs/api-contract.md）的可执行形式。

覆盖 TASK-02 验收清单：
- /health 免认证 200
- 无 token / 过期 token → 401
- README 第 8 条（Gold，10 × 5000）→ "41500.00"，且**是字符串**
- 空 lines / 数量 ≤ 0 / 负价 → 400 且 detail 可读
- 字段缺失、类型错、金额传数字 → 422
- X-Request-Id 回显；审计落库；并发不串单；日志每行带 trace_id
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pricing_api.app import create_app
from pricing_api.logging_setup import JsonFormatter
from pricing_api.security import create_token
from pricing_api.settings import Settings

# 测试密钥：长度 ≥ 32（服务要求），只存在于测试代码里，生产从环境变量读。
TEST_SECRET = "test-secret-0123456789abcdef-0001"
DEALER = "dealer-001"
TRACE_HEADER = "X-Request-Id"


@pytest.fixture()
def api(tmp_path: Path) -> Iterator[tuple[FastAPI, TestClient]]:
    """每个测试一个独立应用 + 独立 SQLite 文件，互不污染。"""
    settings = Settings(
        jwt_secret=TEST_SECRET,
        database_url=f"sqlite:///{tmp_path / 'audit.db'}",
        log_level="INFO",
        app_version="0.2.0",
    )
    application = create_app(settings)
    with TestClient(application) as client:
        yield application, client


def _token(ttl: int = 600, subject: str = DEALER, secret: str = TEST_SECRET) -> str:
    return create_token(subject=subject, secret=secret, ttl_seconds=ttl)


def _auth(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token if token is not None else _token()}"}


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "orderId": "SO-20261027-001",
        "tier": "Gold",
        "lines": [{"sku": "A-100", "quantity": 10, "unitPrice": "5000"}],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------- 健康检查


def test_health_is_public_and_reports_version(api: tuple[FastAPI, TestClient]) -> None:
    """探针不能要求认证，否则负载均衡探不到。"""
    _, client = api
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.2.0"}
    assert response.headers[TRACE_HEADER]


# ---------------------------------------------------------------- 认证


def test_quote_without_token_returns_401(api: tuple[FastAPI, TestClient]) -> None:
    _, client = api
    response = client.post("/v1/quote", json=_payload())

    assert response.status_code == 401
    assert response.json()["detail"] == "missing bearer token"
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.headers[TRACE_HEADER]


def test_quote_with_expired_token_returns_401(api: tuple[FastAPI, TestClient]) -> None:
    _, client = api
    response = client.post("/v1/quote", json=_payload(), headers=_auth(_token(ttl=-60)))

    assert response.status_code == 401
    assert response.json()["detail"] == "token expired"


def test_quote_with_wrong_signature_returns_401(api: tuple[FastAPI, TestClient]) -> None:
    """用别的密钥签发的 token 必须被拒——否则密钥就形同虚设。"""
    _, client = api
    forged = _token(secret="another-secret-0123456789abcdef-9999")
    response = client.post("/v1/quote", json=_payload(), headers=_auth(forged))

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"


def test_token_without_exp_claim_is_rejected(api: tuple[FastAPI, TestClient]) -> None:
    """缺 exp 就是永久 token，必须拒。"""
    _, client = api
    permanent = jwt.encode({"sub": DEALER}, TEST_SECRET, algorithm="HS256")
    response = client.post("/v1/quote", json=_payload(), headers=_auth(permanent))

    assert response.status_code == 401


# ---------------------------------------------------------------- 正确计价


def test_quote_matches_readme_case8(api: tuple[FastAPI, TestClient]) -> None:
    """验收清单第 3 条：Gold，10 × 5000 → "41500.00"，与本地库完全一致。"""
    _, client = api
    response = client.post("/v1/quote", json=_payload(), headers=_auth())

    assert response.status_code == 200
    body = response.json()
    assert body["orderId"] == "SO-20261027-001"
    assert body["total"] == "41500.00"
    # 关键：类型是字符串。若这里变成 float，说明坑 1 没防住。
    assert isinstance(body["total"], str)
    assert body["traceId"] == response.headers[TRACE_HEADER]


def test_promotional_line_halves_price(api: tuple[FastAPI, TestClient]) -> None:
    """README 第 2 条走 HTTP 也一致：1 × 100 促销 → 50.00。"""
    _, client = api
    body = _payload(
        tier="Normal",
        orderId="SO-PROMO",
        lines=[{"sku": "A", "quantity": 1, "unitPrice": "100", "isPromotional": True}],
    )
    response = client.post("/v1/quote", json=body, headers=_auth())

    assert response.status_code == 200
    assert response.json()["total"] == "50.00"


def test_min_chargeable_floor(api: tuple[FastAPI, TestClient]) -> None:
    """README 第 11 条：1 × 0.5 → 1.00（最低收费）。"""
    _, client = api
    body = _payload(
        tier="Normal", orderId="SO-FLOOR", lines=[{"sku": "A", "quantity": 1, "unitPrice": "0.5"}]
    )
    response = client.post("/v1/quote", json=body, headers=_auth())

    assert response.status_code == 200
    assert response.json()["total"] == "1.00"


# ---------------------------------------------------------------- 业务错误 → 400


@pytest.mark.parametrize(
    ("body", "expected_detail"),
    [
        (_payload(lines=[]), "has no lines"),
        (_payload(lines=[{"sku": "A", "quantity": 0, "unitPrice": "100"}]), "Invalid quantity 0"),
        (_payload(lines=[{"sku": "A", "quantity": 1, "unitPrice": "-1"}]), "Negative price"),
    ],
)
def test_pricing_rule_violations_return_400(
    api: tuple[FastAPI, TestClient],
    body: dict[str, Any],
    expected_detail: str,
) -> None:
    """这三类都是"JSON 合法但算不了"，必须是 400 而不是 422。"""
    _, client = api
    response = client.post("/v1/quote", json=body, headers=_auth())

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert expected_detail in detail
    assert response.headers[TRACE_HEADER]


# ---------------------------------------------------------------- 语法错误 → 422


def test_missing_order_id_returns_422(api: tuple[FastAPI, TestClient]) -> None:
    _, client = api
    body = _payload()
    del body["orderId"]
    response = client.post("/v1/quote", json=body, headers=_auth())

    assert response.status_code == 422


def test_numeric_unit_price_is_rejected(api: tuple[FastAPI, TestClient]) -> None:
    """坑 1 的防线：金额传数字必须 422，并提示改成字符串。"""
    _, client = api
    body = _payload(lines=[{"sku": "A", "quantity": 1, "unitPrice": 100}])
    response = client.post("/v1/quote", json=body, headers=_auth())

    assert response.status_code == 422
    assert "字符串" in json.dumps(response.json(), ensure_ascii=False)


def test_unknown_tier_returns_422(api: tuple[FastAPI, TestClient]) -> None:
    _, client = api
    response = client.post("/v1/quote", json=_payload(tier="Diamond"), headers=_auth())

    assert response.status_code == 422


def test_string_quantity_is_rejected(api: tuple[FastAPI, TestClient]) -> None:
    _, client = api
    body = _payload(lines=[{"sku": "A", "quantity": "10", "unitPrice": "100"}])
    response = client.post("/v1/quote", json=body, headers=_auth())

    assert response.status_code == 422


def test_unknown_field_is_rejected(api: tuple[FastAPI, TestClient]) -> None:
    """extra="forbid"：拼错的字段名不会静默丢弃（否则 discount 拼错就白丢一个折扣）。"""
    _, client = api
    response = client.post("/v1/quote", json=_payload(discount="0.5"), headers=_auth())

    assert response.status_code == 422


# ---------------------------------------------------------------- trace / 审计


def test_incoming_request_id_is_echoed(api: tuple[FastAPI, TestClient]) -> None:
    _, client = api
    response = client.post(
        "/v1/quote", json=_payload(), headers={**_auth(), TRACE_HEADER: "dealer-corr-0001"}
    )

    assert response.status_code == 200
    assert response.headers[TRACE_HEADER] == "dealer-corr-0001"
    assert response.json()["traceId"] == "dealer-corr-0001"


def test_unsafe_request_id_is_replaced(api: tuple[FastAPI, TestClient]) -> None:
    """伪造的 header 不能进日志（换行注入），一律丢弃后重新生成。"""
    _, client = api
    response = client.post(
        "/v1/quote", json=_payload(), headers={**_auth(), TRACE_HEADER: 'bad"id\nFAKE log'}
    )

    assert response.status_code == 200
    assert response.headers[TRACE_HEADER] != 'bad"id\nFAKE log'
    assert len(response.headers[TRACE_HEADER]) == 32  # uuid4().hex


def test_successful_quote_is_audited(api: tuple[FastAPI, TestClient]) -> None:
    """审计要能查到"哪一单、"哪一次请求。"""
    application, client = api
    response = client.post(
        "/v1/quote", json=_payload(), headers={**_auth(), TRACE_HEADER: "audit-0001"}
    )
    assert response.status_code == 200

    store = application.state.store
    assert store.count() == 1

    rows = store.fetch_by_trace_id("audit-0001")
    assert len(rows) == 1
    row = rows[0]
    assert row.order_id == "SO-20261027-001"
    assert row.tier == "Gold"
    assert row.subtotal == "50000"  # 折扣前小计，字符串
    assert row.total == "41500.00"
    assert row.trace_id == "audit-0001"
    assert row.created_at.endswith("+00:00")


def test_rejected_quote_is_not_audited(api: tuple[FastAPI, TestClient]) -> None:
    """400 没有可对账的金额，不入库；失败原因看日志。"""
    application, client = api
    response = client.post("/v1/quote", json=_payload(lines=[]), headers=_auth())

    assert response.status_code == 400
    assert application.state.store.count() == 0


# ---------------------------------------------------------------- 并发与日志


class _LogCollector(logging.Handler):
    """在 emit 时立刻用 JsonFormatter 序列化。

    关键点：格式化发生在**请求处理期间**（同一 context），此时 contextvar 里的
    trace_id 才是这次请求的；若等到测试结束再读，上下文早被 reset 了。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


async def _post_many(app: FastAPI, pairs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """并发发出 N 个请求，返回 (orderId, 我们给的 trace, 服务端回显的 trace)。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

        async def one(order_id: str, trace: str) -> tuple[str, str, str]:
            response = await client.post(
                "/v1/quote",
                json=_payload(orderId=order_id),
                headers={**_auth(), TRACE_HEADER: trace},
            )
            assert response.status_code == 200
            return order_id, trace, response.json()["traceId"]

        return list(await asyncio.gather(*(one(o, t) for o, t in pairs)))


def test_concurrent_requests_do_not_cross_trace_ids(
    api: tuple[FastAPI, TestClient],
) -> None:
    """并发不串单 —— 这条专抓"用全局变量存 trace_id"的写法。

    8 个请求同时进行，每个都必须拿回自己的 trace_id，并且审计表里
    order_id ↔ trace_id 必须一一对应。
    """
    application, _ = api
    pairs = [(f"SO-{index:02d}", f"trace-{index:04d}") for index in range(8)]

    results = asyncio.run(_post_many(application, pairs))

    for order_id, trace, echoed in results:
        assert echoed == trace, f"{order_id} 拿回了别人的 trace_id：{echoed} != {trace}"

    assert application.state.store.count() == len(pairs)
    for order_id, trace, _ in results:
        rows = application.state.store.fetch_by_trace_id(trace)
        assert [row.order_id for row in rows] == [order_id]


def test_logs_are_json_lines_with_trace_id(api: tuple[FastAPI, TestClient]) -> None:
    """每行日志都是 JSON，且带 trace_id；order_id 与 trace_id 的对应关系不能错。"""
    application, _ = api
    collector = _LogCollector()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        pairs = [("SO-LOG-A", "trace-log-a"), ("SO-LOG-B", "trace-log-b")]
        asyncio.run(_post_many(application, pairs))
    finally:
        root.removeHandler(collector)

    assert collector.lines, "没有采集到任何日志"
    for line in collector.lines:
        assert "trace_id" in line
        assert "ts" in line
        assert "level" in line

    calculated = [line for line in collector.lines if line["msg"] == "quote.calculated"]
    assert len(calculated) == 2
    mapping = {line["order_id"]: line["trace_id"] for line in calculated}
    assert mapping == {"SO-LOG-A": "trace-log-a", "SO-LOG-B": "trace-log-b"}
