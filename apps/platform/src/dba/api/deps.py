"""API 依赖：鉴权 / 分页 / 幂等。

对齐《设计文档 v2》§7.1 与《实现要点清单》§1.4。

★ JWT 采用 **HS256 标准库实现**（hmac + hashlib + base64），不引入 PyJWT——
避免为一个对称签名再加一个第三方依赖。B3/B4 若需非对称或更多算法，可在此替换。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Annotated, Any

from dba_runtime import DbaError
from fastapi import Depends, Header, Query, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dba.config import Settings
from dba.di import Container


# ─────────────────────────────────────────────────────────────
# 统一鉴权异常（映射到 40100）
# ─────────────────────────────────────────────────────────────
class AuthError(DbaError):
    code = "40100"
    symbol = "UNAUTHENTICATED"
    http_status = 401


_BEARER_AUTH = HTTPBearer(auto_error=False)


class Principal:
    """当前用户（由 JWT claims 构造，不含任何 DB 查询）。"""

    __slots__ = ("biz_line_id", "role_ids", "user_id", "username")

    def __init__(
        self,
        user_id: int,
        username: str,
        role_ids: list[int],
        biz_line_id: int | None = None,
    ) -> None:
        self.user_id = user_id
        self.username = username
        self.role_ids = role_ids
        self.biz_line_id = biz_line_id


# ─────────────────────────────────────────────────────────────
# JWT（HS256，标准库实现）
# ─────────────────────────────────────────────────────────────
def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (binascii.Error, ValueError) as exc:
        raise AuthError("Token 编码非法") from exc


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def create_access_token(
    *,
    subject: str,
    secret: str,
    role_ids: list[int] | None = None,
    biz_line_id: int | None = None,
    ttl_s: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发 HS256 access token。"""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + ttl_s,
        "roles": role_ids or [],
        "biz_line_id": biz_line_id,
        "typ": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    header = {"alg": "HS256", "typ": "JWT"}
    head = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    signature = _b64url_encode(_sign(signing_input, secret))
    return f"{head}.{body}.{signature}"


def decode_token(token: str, *, secret: str) -> dict[str, Any]:
    """校验并解析 HS256 token；失败一律抛 ``AuthError``。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("Token 格式非法")
    head, body, signature = parts
    signing_input = f"{head}.{body}".encode("ascii")
    expected = _b64url_encode(_sign(signing_input, secret))
    if not hmac.compare_digest(expected, signature):
        raise AuthError("Token 签名校验失败")
    try:
        claims: dict[str, Any] = json.loads(_b64url_decode(body))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthError("Token 载荷非法") from exc
    exp = claims.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise AuthError("Token 已过期")
    return claims


# ─────────────────────────────────────────────────────────────
# 依赖
# ─────────────────────────────────────────────────────────────
def get_settings(request: Request) -> Settings:
    """从应用状态取配置单例。"""
    settings: Settings = request.app.state.settings
    return settings


def get_container(request: Request) -> Container:
    """从应用状态取 DI 容器。"""
    container: Container = request.app.state.container
    return container


def bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER_AUTH)] = None,
) -> str:
    """提取 ``Authorization: Bearer <JWT>``。"""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError("缺少 Bearer Token")
    return credentials.credentials.strip()


def get_current_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    token: Annotated[str, Depends(bearer_token)],
) -> Principal:
    """解析当前用户（不访问 DB，claims 即身份）。"""
    claims = decode_token(token, secret=settings.jwt_secret)
    sub = str(claims.get("sub", "0"))
    roles = claims.get("roles") or []
    return Principal(
        user_id=int(sub) if sub.isdigit() else 0,
        username=sub,
        role_ids=[int(r) for r in roles],
        biz_line_id=claims.get("biz_line_id"),
    )


def require_roles(*required: int) -> Any:
    """生成「需要任一指定角色」的依赖。"""

    def _dep(principal: Annotated[Principal, Depends(get_current_principal)]) -> Principal:
        if required and not set(required).intersection(principal.role_ids):
            raise DbaError("越权访问", code="40300", symbol="SQL_PERMISSION_DENIED")
        return principal

    return _dep


class Pagination:
    """分页参数（``?page=1&size=20``）。"""

    __slots__ = ("page", "size")

    def __init__(self, page: int, size: int) -> None:
        self.page = page
        self.size = size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def pagination(
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> Pagination:
    """分页依赖（页大小上限 200）。"""
    return Pagination(page=page, size=size)


def idempotency_key(
    key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """写操作幂等键（预算 / 价格表 / 技能发布等）。"""
    return key
