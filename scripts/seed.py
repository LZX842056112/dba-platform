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
import logging
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
    SemFieldMapping,
    SemMetric,
    SkillRegistry,
)
from sqlalchemy.dialects.mysql import insert as mysql_insert

#: 逻辑字段 → ``fact_sales`` 物理列映射。
#: ★ 这决定真实模型能否写出对的 SQL —— ``schema_link`` 的提示词「## 字段映射」段就来自本表。
#: ``metric_code`` 非空时关联 ``sem_metric``（外键）；``samples`` 供模型理解取值。
_FACT_FIELD_MAPPINGS: tuple[dict, ...] = (
    {
        "logical_field": "dt",
        "physical_column": "dt",
        "is_dimension": 1,
        "samples": ["2026-09-01", "2026-09-02"],
    },
    {
        "logical_field": "region_code",
        "physical_column": "region_code",
        "is_dimension": 1,
        "samples": ["north", "east", "west"],
    },
    {
        "logical_field": "channel",
        "physical_column": "channel",
        "is_dimension": 1,
        "samples": ["online", "store"],
    },
    {
        "logical_field": "province_code",
        "physical_column": "province_code",
        "is_dimension": 1,
        "samples": ["110000", "320000", "330000"],
    },
    {
        "logical_field": "province_name",
        "physical_column": "province_name",
        "is_dimension": 1,
        "samples": ["北京市", "江苏省", "浙江省"],
    },
    {
        "logical_field": "city_name",
        "physical_column": "city_name",
        "is_dimension": 1,
        "samples": ["北京市", "南京市", "杭州市"],
    },
    {
        "logical_field": "lat",
        "physical_column": "lat",
        "is_dimension": 1,
        "samples": [39.9042, 32.0603],
    },
    {
        "logical_field": "lon",
        "physical_column": "lon",
        "is_dimension": 1,
        "samples": [116.4074, 118.7969],
    },
    {
        "logical_field": "category_code",
        "physical_column": "category_code",
        "is_dimension": 1,
        "samples": ["digital", "appliance", "apparel"],
    },
    {
        "logical_field": "category_name",
        "physical_column": "category_name",
        "is_dimension": 1,
        "samples": ["数码", "家电", "服饰"],
    },
    {
        "logical_field": "gmv",
        "physical_column": "gmv_ex_tax",
        "is_dimension": 0,
        "metric_code": "revenue",
        "samples": [],
    },
    {
        "logical_field": "order_cnt",
        "physical_column": "order_cnt",
        "is_dimension": 0,
        "metric_code": "order_cnt",
        "samples": [],
    },
    {
        "logical_field": "profit",
        "physical_column": "profit_ex_tax",
        "is_dimension": 0,
        "samples": [],
    },
    {"logical_field": "uv", "physical_column": "uv", "is_dimension": 0, "samples": []},
    {
        "logical_field": "refund_cnt",
        "physical_column": "refund_cnt",
        "is_dimension": 0,
        "samples": [],
    },
)

DEFAULT_DSN = os.environ.get(
    "DBA_MYSQL_DSN",
    "mysql+asyncmy://dba_user:dba_user_pwd_2026@192.168.200.10:3306/dba",
)
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


#: 演示省份 —— ★ ``name`` 必须与 ``frontend/src/assets/geo/china-100000-full.json``
#: 里的 ``properties.name`` **完全一致**，否则地图面板会因名称对不上而空白。
_FACT_PROVINCES: tuple[dict, ...] = (
    {"code": "110000", "name": "北京市", "city": "北京市", "lat": 39.9042, "lon": 116.4074},
    {"code": "320000", "name": "江苏省", "city": "南京市", "lat": 32.0603, "lon": 118.7969},
    {"code": "330000", "name": "浙江省", "city": "杭州市", "lat": 30.2741, "lon": 120.1551},
    {"code": "370000", "name": "山东省", "city": "济南市", "lat": 36.6512, "lon": 117.1201},
    {"code": "410000", "name": "河南省", "city": "郑州市", "lat": 34.7466, "lon": 113.6254},
    {"code": "420000", "name": "湖北省", "city": "武汉市", "lat": 30.5928, "lon": 114.3055},
    {"code": "440000", "name": "广东省", "city": "广州市", "lat": 23.1291, "lon": 113.2644},
    {"code": "510000", "name": "四川省", "city": "成都市", "lat": 30.5728, "lon": 104.0668},
)

#: 演示品类
_FACT_CATEGORIES: tuple[dict, ...] = (
    {"code": "digital", "name": "数码"},
    {"code": "appliance", "name": "家电"},
    {"code": "apparel", "name": "服饰"},
    {"code": "food", "name": "食品"},
    {"code": "beauty", "name": "美妆"},
)

_FACT_CHANNELS: tuple[str, ...] = ("online", "store")

#: 品类体量权重（与 ``_FACT_CATEGORIES`` 同序）—— 让占比图有可读的差异，而非五等分。
_FACT_CATEGORY_WEIGHT: tuple[float, ...] = (1.4, 1.2, 1.0, 0.8, 0.6)

#: 省份体量权重（与 ``_FACT_PROVINCES`` 同序）—— 让排行/地图有真实的量级差异，
#: 而不是各声一个数（adcode 全是 xxxx0000，取模无法区分省份）。
_FACT_PROVINCE_WEIGHT: tuple[float, ...] = (0.9, 1.15, 1.0, 0.8, 0.7, 0.75, 1.35, 0.95)

#: 省份 → 大区（保留 ``region_code`` 供行级权限链路使用）
_FACT_REGION_OF: dict[str, str] = {
    "110000": "north",
    "320000": "east",
    "330000": "east",
    "370000": "north",
    "410000": "north",
    "420000": "west",
    "440000": "east",
    "510000": "west",
}

#: ``fact_sales`` 演示事实表 —— 列名必须与 DemoLLM 固定 SQL 对齐（``dt`` / ``gmv_ex_tax``）。
_FACT_SALES_DDL = """
CREATE TABLE IF NOT EXISTS fact_sales (
  id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  dt             DATE            NOT NULL,
  region_code    VARCHAR(16)     NOT NULL,
  channel        VARCHAR(16)     NOT NULL,
  province_code  VARCHAR(16)     NOT NULL,
  province_name  VARCHAR(32)     NOT NULL,
  city_name      VARCHAR(32)     NULL,
  lat            DECIMAL(9,6)    NULL,
  lon            DECIMAL(9,6)    NULL,
  category_code  VARCHAR(16)     NOT NULL,
  category_name  VARCHAR(32)     NOT NULL,
  order_cnt      INT             NOT NULL DEFAULT 0,
  gmv_ex_tax     DECIMAL(18,2)   NOT NULL DEFAULT 0,
  profit_ex_tax  DECIMAL(18,2)   NOT NULL DEFAULT 0,
  uv             INT             NOT NULL DEFAULT 0,
  refund_cnt     INT             NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uk_grain (dt, province_code, channel, category_code),
  KEY idx_dt (dt),
  KEY idx_province (province_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


async def _ensure_fact_sales_shape(conn: sa.ext.asyncio.AsyncConnection) -> None:
    """确保 ``fact_sales`` 是**新版结构**（含省份/品类等地图形维度）。

    若库里是旧版（只有 ``region_code``/``channel`` 的 4 列粒度），直接
    ``DROP`` 重建 —— 本表是**纯演示合成数据**（非业务表），重建无数据损失风险，
    且比「补列 + 迁移唯一键」简单可靠（旧数据在 4 列粒度下会撞唯一键）。
    """
    await conn.execute(sa.text(_FACT_SALES_DDL))
    res = await conn.execute(
        sa.text(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'fact_sales'"
        )
    )
    existing = {row[0] for row in res.fetchall()}
    if "province_code" not in existing:
        logger.warning("fact_sales 是旧版结构（缺省份维度），按演示表语义重建")
        await conn.execute(sa.text("DROP TABLE fact_sales"))
        await conn.execute(sa.text(_FACT_SALES_DDL))


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

    粒度 = 45 天 × 8 省 × 2 渠道 × 5 品类 = **3600 行**；
    45 天 = DemoLLM 的 30 天窗口 + 缓冲；``ON DUPLICATE KEY UPDATE`` 保证幂等。
    确定性伪随机（按日期与维度派生）→ 反复跑结果一致，便于断言。
    """
    await _ensure_fact_sales_shape(conn)
    today = dt.date.today()
    rows: list[dict] = []
    for i in range(45):
        day = today - dt.timedelta(days=i)
        for pi, p in enumerate(_FACT_PROVINCES):
            for ci, channel in enumerate(_FACT_CHANNELS):
                for gi, cat in enumerate(_FACT_CATEGORIES):
                    base = (day.toordinal() * 7 + pi * 31 + ci * 13 + gi * 29) % 97
                    gmv = int(
                        (3000 + base * 173) * _FACT_PROVINCE_WEIGHT[pi] * _FACT_CATEGORY_WEIGHT[gi]
                    )
                    rows.append(
                        {
                            "dt": day,
                            "region_code": _FACT_REGION_OF[p["code"]],
                            "channel": channel,
                            "province_code": p["code"],
                            "province_name": p["name"],
                            "city_name": p["city"],
                            "lat": p["lat"],
                            "lon": p["lon"],
                            "category_code": cat["code"],
                            "category_name": cat["name"],
                            "order_cnt": 20 + base % 40,
                            "gmv_ex_tax": gmv,
                            "profit_ex_tax": round(gmv * 0.18, 2),
                            "uv": 100 + base % 200,
                            "refund_cnt": base % 5,
                        }
                    )
    await conn.execute(
        sa.text(
            "INSERT INTO fact_sales (dt, region_code, channel, province_code, "
            "province_name, city_name, lat, lon, category_code, category_name, "
            "order_cnt, gmv_ex_tax, profit_ex_tax, uv, refund_cnt) "
            "VALUES (:dt, :region_code, :channel, :province_code, :province_name, "
            ":city_name, :lat, :lon, :category_code, :category_name, :order_cnt, "
            ":gmv_ex_tax, :profit_ex_tax, :uv, :refund_cnt) AS new "
            "ON DUPLICATE KEY UPDATE order_cnt=new.order_cnt, "
            "gmv_ex_tax=new.gmv_ex_tax, profit_ex_tax=new.profit_ex_tax, "
            "uv=new.uv, refund_cnt=new.refund_cnt"
        ),
        rows,
    )
    return len(rows)


async def _upsert_sem_metrics(conn: sa.ext.asyncio.AsyncConnection, rows_in: list[dict]) -> int:
    """按 ``(metric_code, biz_line_id IS NULL, version)`` **upsert** 指标口径。

    ★ 为什么必须显式 upsert，不能用 ``_insert_missing`` / ``ON DUPLICATE KEY UPDATE``：
      ① ``_insert_missing`` 遇已存在行会 ``continue`` 跳过 → **改了 ``sql_expr`` 也写不进库**；
      ② 本表唯一键是 ``(metric_code, biz_line_id, version)``，而 ``biz_line_id IS NULL`` 时
         MySQL 唯一索引把 NULL 视为互不相同 → 唯一键**根本约束不住**，dup-key 更新失效。
    """
    table = SemMetric.__table__
    affected = 0
    for row in rows_in:
        values = {**row, "created_at": _now(), "updated_at": _now()}
        biz = row.get("biz_line_id")
        conds = [
            table.c.metric_code == row["metric_code"],
            table.c.biz_line_id.is_(None) if biz is None else table.c.biz_line_id == biz,
            table.c.version == row["version"],
        ]
        exists = (
            await conn.execute(sa.select(sa.literal(1)).select_from(table).where(*conds).limit(1))
        ).scalar()
        if exists:
            await conn.execute(sa.update(table).where(*conds).values(**values))
        else:
            await conn.execute(sa.insert(table).values(**values))
        affected += 1
    return affected


async def _seed_field_mappings(conn: sa.ext.asyncio.AsyncConnection) -> int:
    """登记「逻辑字段 → ``fact_sales`` 物理列」（幂等）。

    必须在 ``_upsert_sem_metrics`` 之后调用：``metric_id`` 是指向 ``sem_metric.id`` 的外键。
    """
    table = SemFieldMapping.__table__
    res = await conn.execute(
        sa.select(SemMetric.__table__.c.metric_code, SemMetric.__table__.c.id).where(
            SemMetric.__table__.c.biz_line_id.is_(None),
            SemMetric.__table__.c.status == 1,
        )
    )
    code_to_id: dict[str, int] = {str(code): int(mid) for code, mid in res.fetchall()}

    rows: list[dict] = []
    for item in _FACT_FIELD_MAPPINGS:
        code = item.get("metric_code")
        samples = item.get("samples") or []
        rows.append(
            {
                "logical_field": item["logical_field"],
                "physical_table": "fact_sales",
                "physical_column": item["physical_column"],
                "join_path": None,
                "is_dimension": item["is_dimension"],
                # sa.JSON 会自行序列化：这里传 Python 对象，不要 json.dumps（否则双重编码）
                "sample_values": samples or None,
                "metric_id": code_to_id.get(str(code)) if code else None,
                "created_at": _now(),
            }
        )
    return await _insert_missing(
        conn,
        table,
        rows,
        ("logical_field", "physical_table", "physical_column"),
    )


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
        # ★ 口径必须与演示事实表 ``fact_sales`` 的真实列对齐。此前写的是
        #   ``SUM(amount)`` / ``COUNT(DISTINCT order_id)`` / ``…user_id``，
        #   而 fact_sales 根本没有这三列 —— 会把真实模型带偏成必错的 SQL。
        metrics = [
            {
                "metric_code": "revenue",
                "metric_name": "营业收入（GMV）",
                "biz_line_id": None,
                "caliber_desc": "支付口径 GMV，不含税，按统计日聚合。",
                "sql_expr": "SUM(gmv_ex_tax)",
                "unit": "CNY",
                "include_tax": 0,
                "version": 1,
                "status": 1,
            },
            {
                "metric_code": "order_cnt",
                "metric_name": "订单量",
                "biz_line_id": None,
                "caliber_desc": "有效订单量（按粒度预聚合，直接求和）。",
                "sql_expr": "SUM(order_cnt)",
                "unit": "笔",
                "version": 1,
                "status": 1,
            },
            {
                "metric_code": "arpu",
                "metric_name": "访客客单价",
                "biz_line_id": None,
                "caliber_desc": "访客客单价 = GMV / 访客数（UV）。",
                "sql_expr": "SUM(gmv_ex_tax) / NULLIF(SUM(uv), 0)",
                "unit": "CNY",
                "version": 1,
                "status": 1,
            },
        ]
        counts["sem_metric"] = await _upsert_sem_metrics(conn, metrics)
        counts["sem_field_mapping"] = await _seed_field_mappings(conn)

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
