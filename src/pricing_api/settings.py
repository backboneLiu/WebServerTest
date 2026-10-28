"""运行期配置。

原则：**配置只从环境变量来，仓库里不出现任何密钥字面量**（验收清单第 7 条）。
配置在进程启动时一次性读入、校验失败即崩溃（fail fast）——因为带着空密钥跑起来，
比启动失败危险得多：它会让所有请求都"认证通过"或全部失败，而运维看不出原因。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

DEFAULT_DATABASE_URL = "sqlite:///./data/pricing.db"
DEFAULT_VERSION = "0.2.0"

# HS256 是 HMAC-SHA256，密钥长度直接决定抗暴力破解强度。
# 32 字符 ≈ 256 bit 熵的下限，低于这个值等于给客户埋雷。
MIN_SECRET_LENGTH = 32


@dataclass(frozen=True)
class Settings:
    """服务的全部运行期配置。测试可以直接构造，绕过 from_env 的强校验路径。"""

    jwt_secret: str
    database_url: str
    log_level: str = "INFO"
    app_version: str = DEFAULT_VERSION
    # 支持双密钥并存的轮换窗口：新 token 用 jwt_secret 签，旧 token 用这个验。
    jwt_secret_previous: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        """从环境变量构造。缺失或非法一律抛 RuntimeError，让容器启动失败。"""
        secret = os.environ.get("JWT_SECRET", "")
        if not secret:
            raise RuntimeError("环境变量 JWT_SECRET 缺失，服务拒绝以空密钥启动")
        if len(secret) < MIN_SECRET_LENGTH:
            raise RuntimeError(
                f"JWT_SECRET 过短（{len(secret)} < {MIN_SECRET_LENGTH}），HS256 密钥至少 32 字符"
            )

        previous = os.environ.get("JWT_SECRET_PREVIOUS") or None
        if previous is not None and len(previous) < MIN_SECRET_LENGTH:
            raise RuntimeError(f"JWT_SECRET_PREVIOUS 过短（{len(previous)} < {MIN_SECRET_LENGTH}）")

        log_level = (os.environ.get("LOG_LEVEL") or "INFO").upper()
        if log_level not in logging.getLevelNamesMapping():
            raise RuntimeError(f"LOG_LEVEL 非法：{log_level!r}，可选 DEBUG/INFO/WARNING/ERROR")

        return cls(
            jwt_secret=secret,
            database_url=os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL,
            log_level=log_level,
            app_version=os.environ.get("APP_VERSION") or DEFAULT_VERSION,
            jwt_secret_previous=previous,
        )
