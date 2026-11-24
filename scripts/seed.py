"""种子数据脚本：角色 / 价格表 / 预算 + 首个管理员（``dba seed``）。

用法::

    uv run dba seed                          # 灌入角色 / 价格 / 预算
    DBA_BOOTSTRAP_ADMIN_USERNAME=admin DBA_BOOTSTRAP_ADMIN_PASSWORD=<强口令> uv run dba seed
                                             # 额外初始化首个管理员

★ 不在代码中内置任何账号或口令。管理员仅在显式设置两个环境变量时创建。
★ 幂等：所有写入走 ``INSERT ... ON DUPLICATE KEY UPDATE`` / ``INSERT IGNORE``。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import logging
import os
import sys

import sqlalchemy as sa
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.models import (
    AuthRole,
    AuthUser,
    AuthUserRole,
    Budget,
    PriceBook,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert
DEFAULT_DSN = os.environ.get("DBA_MYSQL_DSN", "")
_MICRO = 1_000_000  # 1 USD = 1_000_000 micro_usd

logger = logging.getLogger("dba.seed")


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


#: ★ 价格表生效时间用**固定值**（不能用 ``now``）——否则每次 seed 的
#: ``effective_from`` 都不同，按 (provider, model, effective_from) 去重会失效、重复灌入。
_SEED_EFFECTIVE_FROM = dt.datetime(2026, 1, 1, 0, 0, 0)


def _hash(password: str) -> str:
    """口令哈希：与 ``api/v1/auth.py::_verify_password`` 对齐（sha256 十六进制）。

    ★ 不能用 bcrypt：``_verify_password`` 只认 ``sha256hex`` 或明文，
    写 bcrypt 会导致登录**必然失败**。
    """
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


async def _insert_missing(
    conn: sa.ext.asyncio.AsyncConnection,
    table: sa.Table,
    rows: list[dict],
    key_cols: tuple[str, ...],
) -> int:
    """只插入「按 ``key_cols`` 不存在」的行，返回新增条数。

    ★ 为什么不用 ``INSERT IGNORE``：MySQL 唯一索引把 NULL 视为互不相同，
      ``sem_metric.biz_line_id``（全局口径为 NULL）与无唯一键的 ``price_book``
      用 ``INSERT IGNORE`` **无法去重**（重复执行会翻倍）。因此改为**显式存在性判断**：
      非空键用 ``=``、空键用 ``IS NULL``。
    """
    inserted = 0
    for row in rows:
        conds = []
        for col in key_cols:
            value = row.get(col)
            if value is None:
                conds.append(table.c[col].is_(None))
            else:
                conds.append(table.c[col] == value)
        exists = None
        if conds:
            stmt = sa.select(sa.literal(1)).select_from(table).where(*conds).limit(1)
            exists = (await conn.execute(stmt)).scalar()
        if exists:
            continue
        await conn.execute(sa.insert(table).values(**row))
        inserted += 1
    return inserted


async def _bootstrap_admin(
    conn: sa.ext.asyncio.AsyncConnection, now: dt.datetime
) -> int:
    """按环境变量初始化首个管理员；不内置任何账号或口令。

    ``DBA_BOOTSTRAP_ADMIN_USERNAME`` 与 ``DBA_BOOTSTRAP_ADMIN_PASSWORD`` 必须同时设置，
    否则跳过，不创建任何账号。明文口令不落库。
    """
    username = os.environ.get("DBA_BOOTSTRAP_ADMIN_USERNAME", "").strip()
    password = os.environ.get("DBA_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        logger.info("未设置管理员初始化环境变量，跳过账号创建")
        return 0

    role = await conn.execute(
        sa.select(AuthRole.__table__.c.id).where(AuthRole.__table__.c.code == "admin")
    )
    role_id = role.scalar_one_or_none()
    if role_id is None:
        raise RuntimeError("admin 角色不存在：请先确认角色种子已写入")

    digest = _hash(password)
    await conn.execute(
        mysql_insert(AuthUser.__table__)
        .values(
            username=username,
            password_hash=digest,
            display_name="平台管理员",
            status=1,
            created_at=now,
            deleted_at=None,
        )
        .on_duplicate_key_update(password_hash=digest, status=1, deleted_at=None)
    )
    user = await conn.execute(
        sa.select(AuthUser.__table__.c.id).where(AuthUser.__table__.c.username == username)
    )
    user_id = int(user.scalar_one())
    await conn.execute(
        mysql_insert(AuthUserRole.__table__)
        .values(user_id=user_id, role_id=int(role_id))
        .on_duplicate_key_update(role_id=int(role_id))
    )
    return 1


async def seed(engine: sa.ext.asyncio.AsyncEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.begin() as conn:
        # ── 角色 ─────────────────────────────────────────────────
        roles = [
            {"id": 1, "code": "admin", "name": "平台管理员"},
            {"id": 2, "code": "analyst", "name": "业务分析师"},
            {"id": 3, "code": "viewer", "name": "只读访客"},
        ]
        counts["auth_role"] = await _insert_missing(conn, AuthRole.__table__, roles, ("code",))

        # ── 价格表（归一化 micro_usd / 1K tokens） ───────────────
        now = _now()
        prices = [
            {
                "provider": "openai",
                "model": "gpt-4o",
                "billing_unit": "PER_1K_TOKEN",
                "input_price_micro_usd": 2500,  # $2.50 / 1M
                "output_price_micro_usd": 10000,  # $10.00 / 1M
                "cache_read_price_micro_usd": 1250,  # $1.25 / 1M
                "cache_write_price_micro_usd": 0,
                "currency": "USD",
                "fx_rate_to_usd": 1.0,
                "effective_from": _SEED_EFFECTIVE_FROM,
                "effective_to": None,
                "created_at": now,
            },
            {
                "provider": "openai",
                "model": "gpt-4o-mini",
                "billing_unit": "PER_1K_TOKEN",
                "input_price_micro_usd": 150,  # $0.15 / 1M
                "output_price_micro_usd": 600,  # $0.60 / 1M
                "cache_read_price_micro_usd": 75,
                "cache_write_price_micro_usd": 0,
                "currency": "USD",
                "fx_rate_to_usd": 1.0,
                "effective_from": _SEED_EFFECTIVE_FROM,
                "effective_to": None,
                "created_at": now,
            },
            {
                "provider": "anthropic",
                "model": "claude-3-5-sonnet",
                "billing_unit": "PER_1K_TOKEN",
                "input_price_micro_usd": 3000,
                "output_price_micro_usd": 15000,
                "cache_read_price_micro_usd": 300,
                "cache_write_price_micro_usd": 3750,
                "currency": "USD",
                "fx_rate_to_usd": 1.0,
                "effective_from": _SEED_EFFECTIVE_FROM,
                "effective_to": None,
                "created_at": now,
            },
            # ★ DeepSeek（真实模型默认 provider）：官方价 per 1M tokens =
            #   输入命中 $0.028 / 未命中 $0.28 / 输出 $0.42（deepseek-chat = V3.2 非思考模式）。
            #   换算 micro_usd/1K：×1000。
            #   缺了它 → 所有 deepseek 调用成本被记 0（FinOps 覆盖率失真）。
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "billing_unit": "PER_1K_TOKEN",
                "input_price_micro_usd": 280,
                "output_price_micro_usd": 420,
                "cache_read_price_micro_usd": 28,
                "cache_write_price_micro_usd": 0,
                "currency": "USD",
                "fx_rate_to_usd": 1.0,
                "effective_from": _SEED_EFFECTIVE_FROM,
                "effective_to": None,
                "created_at": now,
            },
        ]
        counts["price_book"] = await _insert_missing(
            conn, PriceBook.__table__, prices, ("provider", "model", "effective_from")
        )

        # ── 预算（★ P0-5：hard_limit_pct 默认 90） ───────────────
        budgets = [
            {
                "scope_type": "GLOBAL",
                "scope_id": "*",
                "period": "MONTH",
                "amount_micro_usd": 500 * _MICRO,
                "soft_limit_pct": 80,
                "hard_limit_pct": 90,
                "soft_action": "DOWNGRADE_MODEL",
                "hard_action": "BLOCK",
                "priority": 10,
                "timezone": "Asia/Shanghai",
                "enabled": 1,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            },
            {
                "scope_type": "BIZ_LINE",
                "scope_id": "1",
                "period": "MONTH",
                "amount_micro_usd": 200 * _MICRO,
                "soft_limit_pct": 80,
                "hard_limit_pct": 90,
                "soft_action": "ALERT",
                "hard_action": "BLOCK",
                "priority": 50,
                "timezone": "Asia/Shanghai",
                "enabled": 1,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            },
        ]
        counts["budget"] = await _insert_missing(
            conn, Budget.__table__, budgets, ("scope_type", "scope_id", "period", "version")
        )

        counts["auth_user"] = await _bootstrap_admin(conn, now)

    return counts


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dba seed", description="灌入种子数据")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="MySQL DSN")
    args = parser.parse_args(argv)

    if not args.dsn:
        print("[seed] 缺少 MySQL DSN：请设置 DBA_MYSQL_DSN 或传 --dsn", file=sys.stderr)
        return 2
    engine = build_engine(args.dsn)
    try:
        counts = await seed(engine)
    finally:
        await engine.dispose()
    print(f"[seed] 完成：{counts}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
