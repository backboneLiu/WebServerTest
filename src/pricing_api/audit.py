"""审计落库（SQLite）。

为什么金额列用 TEXT 而不是 REAL/NUMERIC？

SQLite 是**动态类型**：声明成 `NUMERIC` 的列，写入 `"1.005"` 时会被亲和性规则转成 REAL，
再读出来就是 `1.005` 的浮点近似值。金融数据一旦落库就烂在根上，后面怎么补都补不回来。
存字符串 = 原样保存十进制字面量，和应用层 Decimal 精确对齐。

为什么每次插入都新建连接？

sqlite3.Connection **默认不允许跨线程使用**。我们用 `asyncio.to_thread` 把写操作丢到
工作线程，如果复用同一个连接就会踩 `SQLite objects created in a thread can only be used
in that thread`。SQLite 建连接极快（打开本地文件），每请求新建 + finally 关闭，
比维护线程局部连接池更简单也更安全。W2 换 Postgres 时会换成真正的异步驱动 + 连接池。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SQLITE_PREFIX = "sqlite:///"

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS quote_audit (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id   TEXT NOT NULL,
        tier       TEXT NOT NULL,
        subtotal   TEXT NOT NULL,
        total      TEXT NOT NULL,
        trace_id   TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    # 排查入口就是 trace_id：客户报单号 → 找日志/找这一行。
    "CREATE INDEX IF NOT EXISTS idx_quote_audit_trace_id ON quote_audit (trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_quote_audit_order_id ON quote_audit (order_id)",
)

_INSERT_SQL = """
INSERT INTO quote_audit (order_id, tier, subtotal, total, trace_id, created_at)
VALUES (?, ?, ?, ?, ?, ?)
"""


@dataclass(frozen=True)
class AuditRecord:
    """一次成功报价的留痕。金额是字符串，理由见模块 docstring。"""

    order_id: str
    tier: str
    subtotal: str
    total: str
    trace_id: str
    created_at: str


def sqlite_path(database_url: str) -> Path:
    """把 `DATABASE_URL` 解析成文件路径。

    只支持 `sqlite:///<path>`。遇到 `postgresql://` 明确报错而不是"静默降级成 sqlite"——
    W2 换库时，启动失败比悄悄连错库安全。
    """
    if not database_url.startswith(_SQLITE_PREFIX):
        raise ValueError(
            f"暂不支持的 DATABASE_URL：{database_url!r}；"
            f"当前仅支持 {_SQLITE_PREFIX}<path>（Postgres 属于 W2）"
        )
    raw = database_url[len(_SQLITE_PREFIX) :]
    if not raw or raw == ":memory:":
        raise ValueError("SQLite 内存库无法跨连接共享（每次连接都是新库），请改用文件路径")
    return Path(raw)


class AuditStore:
    """审计表的最小封装：建表 + 插入 + 计数。"""

    def __init__(self, database_url: str) -> None:
        self._path = sqlite_path(database_url)

    @property
    def path(self) -> Path:
        return self._path

    def init(self) -> None:
        """建库建表。幂等，容器每次启动都跑一遍。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path)
        try:
            # WAL：读写不互相阻塞，并发请求写审计时不会排队等锁。
            conn.execute("PRAGMA journal_mode=WAL")
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()

    def insert(self, record: AuditRecord) -> None:
        """写一行。这是**同步阻塞**调用，调用方必须放进线程池（asyncio.to_thread）。"""
        conn = sqlite3.connect(self._path, timeout=5.0)
        try:
            with conn:  # 事务：正常退出自动 commit，异常自动 rollback
                conn.execute(
                    _INSERT_SQL,
                    (
                        record.order_id,
                        record.tier,
                        record.subtotal,
                        record.total,
                        record.trace_id,
                        record.created_at,
                    ),
                )
        finally:
            conn.close()

    def count(self) -> int:
        """只给测试和健康排查用。"""
        conn = sqlite3.connect(self._path)
        try:
            row = conn.execute("SELECT COUNT(*) FROM quote_audit").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row is not None else 0

    def fetch_by_trace_id(self, trace_id: str) -> list[AuditRecord]:
        """按 trace_id 取审计行。验收清单第 5 条就是靠它自证"能查到哪一单"。"""
        conn = sqlite3.connect(self._path)
        try:
            rows = conn.execute(
                "SELECT order_id, tier, subtotal, total, trace_id, created_at "
                "FROM quote_audit WHERE trace_id = ? ORDER BY id",
                (trace_id,),
            ).fetchall()
        finally:
            conn.close()
        return [AuditRecord(*row) for row in rows]
