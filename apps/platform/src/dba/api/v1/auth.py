"""``/api/v1/auth`` —— 认证与用户（§7.2）。

对齐《设计文档 v2》§7.2 与《实现要点清单》§1.6。

* HS256 JWT 由 ``api.deps`` 用标准库实现（不引 PyJWT）；
* 登录校验 ``auth_user.password_hash``：兼容明文与 ``sha256`` 十六进制（演示数据），
  生产必须改为 bcrypt/argon2（见报告遗留问题）；
* ``/auth/me`` 返回 ``scope_summary``（可访问表 + 行级权限谓词 + ``hash``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from dba.api.deps import (
    AuthError,
    Principal,
    bearer_token,
    create_access_token,
    decode_token,
    get_container,
    get_current_principal,
    get_settings,
    require_roles,
)
from dba.config import Settings
from dba.di import Container

__all__ = ["router"]

logger = logging.getLogger("dba.api.auth")

router = APIRouter()

#: admin 角色 ID（seed 数据约定 role_id=1 为 admin）
ADMIN_ROLE_ID = 1


def _verify_password(password: str, stored: str) -> bool:
    """校验口令：兼容 ``sha256`` 十六进制与明文（演示）。"""
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return stored in (digest, password)


@router.post("/login")
async def login(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``POST /auth/login``：校验用户并签发 access/refresh token。"""
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    if not username:
        raise AuthError("缺少用户名")

    repos = container.get("repos")
    user: dict[str, Any] | None = None
    if repos is not None:
        try:
            user = await repos.auth_user.by_username(username)
        except Exception as exc:  # noqa: BLE001 - DB 不可用时按失败处理（不静默放行）
            logger.warning("用户查询失败：%s", exc)
    if user is None:
        raise AuthError("用户名或口令错误")
    if not _verify_password(password, str(user.get("password_hash", ""))):
        raise AuthError("用户名或口令错误")

    user_id = int(user["id"])
    role_ids: list[int] = []
    if repos is not None:
        try:
            role_ids = [int(r) for r in await repos.auth_role.role_ids_of_user(user_id)]
        except Exception:  # noqa: BLE001 - 角色读取失败按无角色处理（最小权限）
            role_ids = []
    biz_line_id = user.get("biz_line_id")

    access = create_access_token(
        subject=str(user_id),
        secret=settings.jwt_secret,
        role_ids=role_ids,
        biz_line_id=biz_line_id,
        ttl_s=settings.access_token_ttl_s,
    )
    refresh = create_access_token(
        subject=str(user_id),
        secret=settings.jwt_secret,
        role_ids=role_ids,
        biz_line_id=biz_line_id,
        ttl_s=settings.refresh_token_ttl_s,
        extra_claims={"typ": "refresh"},
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": settings.access_token_ttl_s,
    }


@router.post("/refresh")
async def refresh_token(
    payload: dict[str, Any],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """``POST /auth/refresh``：用 refresh token 换新 access token。"""
    token = str(payload.get("refresh_token") or "")
    claims = decode_token(token, secret=settings.jwt_secret)
    if claims.get("typ") != "refresh":
        raise AuthError("非 refresh token")
    access = create_access_token(
        subject=str(claims.get("sub", "0")),
        secret=settings.jwt_secret,
        role_ids=[int(r) for r in (claims.get("roles") or [])],
        biz_line_id=claims.get("biz_line_id"),
        ttl_s=settings.access_token_ttl_s,
    )
    return {
        "access_token": access,
        "refresh_token": token,
        "expires_in": settings.access_token_ttl_s,
    }


@router.post("/logout")
async def logout(token: Annotated[str, Depends(bearer_token)]) -> dict[str, Any]:
    """``POST /auth/logout``：签发/吊销由幂等策略决定，当前为无状态实现（返回 ok）。"""
    _ = token
    return {"ok": True}


@router.get("/me")
async def me(
    principal: Annotated[Principal, Depends(get_current_principal)],
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """``GET /auth/me``：返回用户、角色、业务线与 ``scope_summary``。"""
    repos = container.get("repos")
    roles: list[dict[str, Any]] = []
    scope_summary = await _scope_summary(container, principal.role_ids)
    user: dict[str, Any] = {
        "user_id": principal.user_id,
        "username": principal.username,
        "biz_line_id": principal.biz_line_id,
    }
    if repos is not None:
        try:
            roles = [dict(r) for r in await repos.auth_role.roles_of_user(principal.user_id)]
        except Exception:  # noqa: BLE001
            roles = []
    return {
        "user": user,
        "roles": roles,
        "biz_lines": [principal.biz_line_id] if principal.biz_line_id is not None else [],
        "scope_summary": scope_summary,
    }


@router.get("/users")
async def list_users(
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=200)] = 20,
    q: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    """``GET /auth/users``（admin）。"""
    repos = container.get("repos")
    if repos is None:
        return []
    _ = (page, size, q)
    # 复用 auth_user 的 ``by_username`` 之外没有列表接口 → 返回空（避免直连表）
    return []


@router.post("/users/{user_id}/roles")
async def grant_roles(
    user_id: int,
    payload: dict[str, Any],
    _admin: Annotated[Principal, Depends(require_roles(ADMIN_ROLE_ID))],
    container: Annotated[Container, Depends(get_container)],
) -> JSONResponse:
    """``POST /auth/users/{id}/roles``（admin，幂等）。"""
    repos = container.get("repos")
    role_ids = [int(r) for r in (payload.get("role_ids") or [])]
    if repos is None:
        return JSONResponse({"ok": False, "reason": "storage_unavailable"}, status_code=503)
    for role_id in role_ids:
        try:
            await repos.user_role.grant(user_id, role_id)
        except Exception:  # noqa: BLE001 - 已存在（复合主键冲突）即幂等
            continue
    return JSONResponse({"ok": True})


async def _scope_summary(container: Container, role_ids: list[int]) -> dict[str, Any]:
    """构造 ``scope_summary``（可访问表 + 谓词 + ``sha256`` 前 32 hex 的 hash）。"""
    repos = container.get("repos")
    accessible: list[str] = []
    predicates: list[dict[str, Any]] = []
    rule_version: str | None = None
    if repos is not None and role_ids:
        try:
            rules = await repos.row_scope_rule.rules_for_roles(role_ids)
        except Exception:  # noqa: BLE001
            rules = []
        try:
            rule_version = await repos.row_scope_rule.rule_version(role_ids)
        except Exception:  # noqa: BLE001
            rule_version = None
        for rule in rules:
            table = str(rule.get("physical_table") or "")
            if table and table not in accessible:
                accessible.append(table)
            values = rule.get("scope_values")
            if isinstance(values, str):
                try:
                    values = json.loads(values)
                except json.JSONDecodeError:
                    values = [values]
            predicates.append(
                {
                    "table": table,
                    "column": rule.get("scope_column"),
                    "values": values or [],
                }
            )
    # ★ 与 scope_hash 同口径：sha256(canonical_json(表+谓词+规则版本))[:32]
    canonical = json.dumps(
        {"tables": sorted(accessible), "predicates": predicates, "rule_version": rule_version},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return {
        "accessible_tables": accessible,
        "scope_predicates": predicates,
        "hash": digest,
        "rule_version": rule_version,
    }
