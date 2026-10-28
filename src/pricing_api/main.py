"""uvicorn 入口：`uvicorn pricing_api.main:app`。

这里刻意不在 import 时做任何 try/except：配置缺失就让进程直接崩。
容器编排会把"启动失败"当成部署失败并保留上一个健康版本，
这比"带着空密钥/空数据库跑起来"安全得多。
"""

from __future__ import annotations

from fastapi import FastAPI

from pricing_api.app import create_app

app: FastAPI = create_app()
