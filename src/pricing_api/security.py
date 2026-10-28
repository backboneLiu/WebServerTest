"""JWT（HS256）签发与校验。

三个安全要点，都是常见的实战踩坑点：

1. **校验时显式指定 `algorithms=["HS256"]`**。不指定的话，攻击者可以把
   header 改成 `{"alg":"none"}` 或换成非对称算法，让服务用公钥当 HMAC 密钥验签（算法混淆攻击）。

2. **强制要求 `exp` 和 `sub`**（`options={"require": [...]}`）。少了 exp 就是永久 token，
   `jwt.decode` 默认不强制；少了 sub 就无法追溯是哪个经销商。

3. **密钥轮换要能并存**。`JWT_SECRET_PREVIOUS` 只用于验签、不用于签发，
   这样轮换期间已发出去的 token 不会立刻失效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import jwt

from pricing_api.settings import Settings

ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 3600


class TokenError(Exception):
    """token 不可用的基类。`str(exc)` 直接作为 401 的 detail 返回给调用方。"""


class TokenExpired(TokenError):
    """签名有效但已过期——调用方应该重新换一个 token，而不是查密钥。"""


class TokenInvalid(TokenError):
    """签名错误 / 结构非法 / 缺必需声明。"""


@dataclass(frozen=True)
class Principal:
    """认证通过后拿到的调用方身份。目前只用到 subject（经销商标识）。"""

    subject: str


def create_token(
    subject: str,
    secret: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: datetime | None = None,
) -> str:
    """签发一个 HS256 token。`now` 可注入，测试用它造过期 token，不必真的 sleep。"""
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": subject,
        "iat": int(issued_at.timestamp()),
        "exp": int(issued_at.timestamp()) + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_token(token: str, settings: Settings) -> Principal:
    """验签并取出主体。新钥在前、旧钥在后，任一验通即可。"""
    candidates = [settings.jwt_secret]
    if settings.jwt_secret_previous is not None:
        candidates.append(settings.jwt_secret_previous)

    last_error: Exception | None = None
    for secret in candidates:
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=[ALGORITHM],
                options={"require": ["exp", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            # 注意顺序：ExpiredSignatureError 是 InvalidTokenError 的子类，
            # 必须写在前面，否则过期 token 会被当成"密钥不对"继续试旧钥。
            raise TokenExpired("token expired") from None
        except jwt.InvalidTokenError as exc:
            last_error = exc
            continue

        subject = payload.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenInvalid("invalid token")
        return Principal(subject=subject)

    raise TokenInvalid("invalid token") from last_error
