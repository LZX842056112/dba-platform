"""造数脚本：口径 / 价格表 / 技能模板 / 预算 / 演示数据（``dba seed``）。

对齐《设计文档 v2》§12.4 与《实现要点清单》§1.2.6.4。

用法::

    uv run python scripts/seed.py            # 最小可用种子（口径 + 价格 + 预算 + 角色）
    uv run python scripts/seed.py --demo     # 追加演示业务数据（业务线 / 用户 / 技能）

★ 幂等：所有写入走 ``INSERT ... ON DUPLICATE KEY UPDATE`` / ``INSERT IGNORE``，
  重复执行不会产生重复行（DoD 要求 ``migrate`` 后再 ``seed`` 可反复跑）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import sys

import sqlalchemy as sa
from dba.storage.mysql.engine import build_engine
from dba.storage.mysql.models import (
    AuthRole,
    AuthUser,
    AuthUserRole,
    BizLine,
    Budget,
    PriceBook,
    SemMetric,
    SkillRegistry,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert

DEFAULT_DSN = os.environ.get(
    "DBA_MYSQL_DSN",
    "mysql+asyncmy://dba_user:dba_user_pwd_2026@192.168.200.10:3306/dba",
)
_MICRO = 1_000_000  # 1 USD = 1_000_000 micro_usd


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


#: 演示账号（本地联调用；口令见 README。生产必须改）
DEMO_USERS: tuple[dict, ...] = (
    {
        "id": 1,
        "username": "admin",
        "password": "admin123",
        "display_name": "演示管理员",
        "email": "admin@example.com",
        "biz_line_id": None,
        "role_id": 1,
    },
    {
        "id": 2,
        "username": "analyst1",
        "password": "analyst123",
        "display_name": "演示分析师",
        "email": "analyst1@example.com",
        "biz_line_id": 1,
        "role_id": 2,
    },
)


#: ``fact_sales`` 演示事实表 —— 列名必须与 ``di.py`` 的 DemoLLM 固定 SQL 对齐
#: （``SELECT dt, SUM(gmv_ex_tax) ... FROM fact_sales``）。
_FACT_SALES_DDL = """
CREATE TABLE IF NOT EXISTS fact_sales (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  dt          DATE            NOT NULL,
  region_code VARCHAR(16)     NOT NULL,
  channel     VARCHAR(16)     NOT NULL,
  order_cnt   INT             NOT NULL DEFAULT 0,
  gmv_ex_tax  DECIMAL(18,2)   NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_dt_region_channel (dt, region_code, channel),
  KEY idx_dt (dt)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


async def _upsert_users(conn: sa.ext.asyncio.AsyncConnection, now: dt.datetime) -> int:
    """按 ``username`` upsert 演示用户（★ 必须 upsert，不能只 insert）。

    ``_insert_missing`` 对已存在的 ``admin`` 会**跳过**，导致老的假 bcrypt 口令
    永远修不掉、登录恒失败。这里走 ``ON DUPLICATE KEY UPDATE`` 强制刷新口令。
    """
    affected = 0
    for u in DEMO_USERS:
        row = {
            "id": u["id"],
            "username": u["username"],
            "password_hash": _hash(u["password"]),
            "display_name": u["display_name"],
            "email": u["email"],
            "biz_line_id": u["biz_line_id"],
            "status": 1,
            "created_at": now,
        }
        await conn.execute(
            mysql_insert(AuthUser.__table__)
            .values(**row)
            .on_duplicate_key_update(
                password_hash=row["password_hash"],
                display_name=row["display_name"],
                email=row["email"],
                biz_line_id=row["biz_line_id"],
                status=1,
                deleted_at=None,
            )
        )
        affected += 1
    return affected


async def _seed_fact_sales(conn: sa.ext.asyncio.AsyncConnection) -> int:
    """建 ``fact_sales`` 演示表并灌 45 天数据（★ 日期相对 ``CURDATE()``，任何时刻跑都有数）。

    45 天 = DemoLLM 的 30 天窗口 + 缓冲；``ON DUPLICATE KEY UPDATE`` 保证幂等。
    """
    await conn.execute(sa.text(_FACT_SALES_DDL))
    today = dt.date.today()
    combos = (("east", "online"), ("east", "store"), ("west", "online"), ("north", "store"))
    rows: list[dict] = []
    for i in range(45):
        day = today - dt.timedelta(days=i)
        for j, (region, channel) in enumerate(combos):
            # 确定性伪随机（按日期与组合派生）→ 反复跑结果一致，便于断言
            base = (day.toordinal() * 7 + j * 13) % 50
            rows.append(
                {
                    "dt": day,
                    "region_code": region,
                    "channel": channel,
                    "order_cnt": 100 + base,
                    "gmv_ex_tax": 10000 + base * 137,
                }
            )
    await conn.execute(
        sa.text(
            "INSERT INTO fact_sales (dt, region_code, channel, order_cnt, gmv_ex_tax) "
            "VALUES (:dt, :region_code, :channel, :order_cnt, :gmv_ex_tax) AS new "
            "ON DUPLICATE KEY UPDATE order_cnt=new.order_cnt, "
            "gmv_ex_tax=new.gmv_ex_tax"
        ),
        rows,
    )
    return len(rows)


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


async def seed(engine: sa.ext.asyncio.AsyncEngine, *, demo: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    async with engine.begin() as conn:
        # ── 业务线 ────────────────────────────────────────────────
        biz_lines = [
            {"id": 1, "code": "retail", "name": "零售业务线"},
            {"id": 2, "code": "finance", "name": "金融业务线"},
        ]
        counts["biz_line"] = await _insert_missing(conn, BizLine.__table__, biz_lines, ("code",))

        # ── 角色 ─────────────────────────────────────────────────
        roles = [
            {"id": 1, "code": "admin", "name": "平台管理员"},
            {"id": 2, "code": "analyst", "name": "业务分析师"},
            {"id": 3, "code": "viewer", "name": "只读访客"},
        ]
        counts["auth_role"] = await _insert_missing(conn, AuthRole.__table__, roles, ("code",))

        # ── 指标口径（语义层权威源） ─────────────────────────────
        metrics = [
            {
                "metric_code": "revenue",
                "metric_name": "营业收入",
                "biz_line_id": None,
                "caliber_desc": "按财务口径的确认收入，含税，按统计日聚合。",
                "sql_expr": "SUM(amount)",
                "unit": "CNY",
                "include_tax": 1,
                "version": 1,
                "status": 1,
            },
            {
                "metric_code": "order_cnt",
                "metric_name": "订单量",
                "biz_line_id": None,
                "caliber_desc": "有效订单去重计数（剔除取消/退单）。",
                "sql_expr": "COUNT(DISTINCT order_id)",
                "unit": "笔",
                "version": 1,
                "status": 1,
            },
            {
                "metric_code": "arpu",
                "metric_name": "用户客单价",
                "biz_line_id": None,
                "caliber_desc": "营业收入 / 活跃用户数。",
                "sql_expr": "SUM(amount) / NULLIF(COUNT(DISTINCT user_id), 0)",
                "unit": "CNY",
                "version": 1,
                "status": 1,
            },
        ]
        counts["sem_metric"] = await _insert_missing(
            conn,
            SemMetric.__table__,
            [{**x, "created_at": _now(), "updated_at": _now()} for x in metrics],
            ("metric_code", "biz_line_id", "version"),
        )

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

        if demo:
            # ★ 走 upsert（而非 _insert_missing）：保证已存在的 admin 也被刷新为真口令
            counts["auth_user"] = await _upsert_users(conn, now)
            counts["auth_user_role"] = await _insert_missing(
                conn,
                AuthUserRole.__table__,
                [{"user_id": u["id"], "role_id": u["role_id"]} for u in DEMO_USERS],
                ("user_id", "role_id"),
            )
            # ★ 演示事实表：DemoLLM 的固定 SQL 要查它，缺了主链路必然失败
            counts["fact_sales"] = await _seed_fact_sales(conn)
            skills = [
                {
                    "skill_key": "revenue_trend",
                    "name": "营收趋势查询",
                    "version": 1,
                    "biz_line_id": None,
                    "meta_json": json.dumps(
                        {"intent": "查询营收趋势", "tags": ["chatbi", "trend"]}
                    ),
                    "steps_json": json.dumps(
                        [
                            {"step": "semantic_lookup", "metric": "revenue"},
                            {"step": "sql_gen"},
                            {"step": "execute"},
                        ]
                    ),
                    "params_schema": json.dumps(
                        {"type": "object", "properties": {"days": {"type": "integer"}}}
                    ),
                    "status": "active",
                    "created_at": now,
                },
                {
                    "skill_key": "fail_rate_diag",
                    "name": "失败率诊断",
                    "version": 1,
                    "biz_line_id": None,
                    "meta_json": json.dumps(
                        {"intent": "分析失败率升高", "tags": ["observability"]}
                    ),
                    "steps_json": json.dumps(
                        [{"step": "fetch_metrics"}, {"step": "attribution"}, {"step": "report"}]
                    ),
                    "params_schema": json.dumps({"type": "object", "properties": {}}),
                    "status": "active",
                    "created_at": now,
                },
            ]
            counts["skill_registry"] = await _insert_missing(
                conn, SkillRegistry.__table__, skills, ("skill_key", "version")
            )

    return counts


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dba seed", description="灌入种子数据")
    parser.add_argument("--demo", action="store_true", help="含演示业务数据")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="MySQL DSN")
    args = parser.parse_args(argv)

    engine = build_engine(args.dsn)
    try:
        counts = await seed(engine, demo=bool(args.demo))
    finally:
        await engine.dispose()
    print(f"[seed] 完成：{counts}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(asyncio.run(main()))
