"""结构化日志（JSON 行）。

运维要的是 `grep <trace_id>` 一把捞出一次请求的全部日志，所以：
- 每行一个 JSON 对象（Loki/CloudWatch/阿里云 SLS 都能直接解析）；
- 每行必带 `trace_id`，由 formatter 统一注入，业务代码不需要手动传；
- 不做多行输出（异常堆栈压成 `exc` 字段里的字符串，否则一行一条的约定就破了）。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from pricing_api.trace import current_trace_id

# LogRecord 自带属性，不能当成业务字段重复输出。
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """把一条日志渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            # trace_id 由 formatter 注入：业务代码永远不需要记得传它。
            "trace_id": current_trace_id(),
        }

        # 业务自定义字段：logger.info("quote.ok", extra={"order_id": ...})
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info is not None:
            payload["exc"] = self.formatException(record.exc_info)

        # ensure_ascii=False 让中文日志可读；json.dumps 会自动转义换行，不会撕裂成多行。
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """安装 JSON formatter 到 root logger。

    先 `handlers.clear()`：避免 uvicorn / 测试重复调用时同一条日志被打印多遍。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 的 access log 是纯文本，而且不带 trace_id。
    # 反面教材：只把它 propagate 到 root，它会被 JSON formatter 包成一行
    # `{"msg": "127.0.0.1:xxx - POST /v1/quote HTTP/1.1 200", "trace_id": "-"}`——
    # 看似格式对了，实则一次请求出现两行、且其中一行查不到对应请求。
    # 所以直接 disabled：访问日志由 trace_middleware 统一输出（带 trace_id 与耗时）。
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    # 启动/错误日志保留，但改走 root，以获得 JSON + trace_id 字段。
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
