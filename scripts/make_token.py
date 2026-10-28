"""生成测试用 JWT。

为什么要有这个脚本：手搓 HS256 token 的翻车方式至少有三种——
base64 忘了去 `=` padding、`exp` 写成毫秒时间戳、payload 用了 `ensure_ascii` 导致签名对不上。
每一样都会让你在服务端调半小时，最后发现是 token 本身的问题。

用法（PowerShell）：

    $env:JWT_SECRET = "dev-secret-change-me-0123456789abcdef"   # 至少 32 字符
    uv run python scripts/make_token.py --sub dealer-001 --ttl 3600

    # 造一个已过期的 token，用来验证 401
    uv run python scripts/make_token.py --sub dealer-001 --ttl -60

注意：`--secret` 参数会把密钥留在 shell history 里，生产环境只用环境变量。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from pricing_api.security import create_token
from pricing_api.settings import MIN_SECRET_LENGTH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成 HS256 测试 token（只打印 token，不打印密钥）"
    )
    parser.add_argument("--sub", default="dealer-001", help="主体（经销商标识），默认 dealer-001")
    parser.add_argument("--ttl", type=int, default=3600, help="有效期秒数，负数可造过期 token")
    parser.add_argument(
        "--secret",
        default=None,
        help="签名密钥；不传则读环境变量 JWT_SECRET（推荐）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    secret = args.secret or os.environ.get("JWT_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        print(
            f"密钥缺失或过短（至少 {MIN_SECRET_LENGTH} 字符）："
            "请设置环境变量 JWT_SECRET，或用 --secret 临时指定",
            file=sys.stderr,
        )
        return 2

    token = create_token(subject=args.sub, secret=secret, ttl_seconds=args.ttl)
    # 只输出 token，方便 `$TOKEN = uv run python scripts/make_token.py` 直接接管道。
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
