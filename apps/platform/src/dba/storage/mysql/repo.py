"""MySQL Repository 实现（L5 存储适配层）。

对齐《设计文档 v2》§5.2 / §5.9 与《实现要点清单》§5、§6.10、§6.13。

设计取舍
--------
* 只在**归属矩阵**里被标为 owner 的模块会构造对应 Repo（§5.9）；本模块本身不 import
  L3 模块，也不 import L4 能力层——方向是 ``L5 ← L4``，反向 import 会被 banned-api 拦截。
* 高写入表（``llm_call`` / ``tool_call``）走 Core 批量 INSERT（不走 ORM 会话），
  以减少 ORM 开销；分区表（``run`` / ``metric_daily``）**读取必须带时间窗**（§6.10），
  否则无法分区裁剪、会全分区扫描。

★ P0-4 的关键实现（照抄 v1 会怎样错）
-----------------------------------
``budget_reservation`` 是预算预留在**权威存储**里的落点。v1 只把预留放在 Redis，
Redis 一重启/驱逐，预留就凭空消失 → 结算时账不平，且无法幂等。
v2 要求：

* ``insert_if_absent``：同 ``reservation_id`` 二次插入返回 ``False``（不覆盖）；
* ``settle`` / ``release``：**状态机迁移 + 幂等**——
  ``RESERVED → SETTLED|RELEASED``；重复结算返回 ``already_settled`` 而**不再改数**；
  预留不存在返回 ``reservation_missing``（由上层决定是否告警，而非静默成功）。
  实现用「条件 UPDATE ... WHERE state='RESERVED'」+ ``rowcount`` 判定，
  天然并发安全（InnoDB 行锁），不依赖先读后写。

★ U16（Mongo 侧，见 storage/mongo/repo.py）：``seq`` 用原子 ``$inc`` 分配。
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, cast

import sqlalchemy as sa
from dba_runtime.errors import StorageUnavailableError
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError

from . import models as m

__all__ = [
    "MysqlRepositories",
]

Row = dict[str, Any]


def _row(value: Any) -> Row:
    """把 ORM/Result 行统一转成 ``Row``（列名 → 值）。"""
    mapping = getattr(value, "_mapping", None)
    if mapping is not None:
        return {str(k): v for k, v in mapping.items()}
    cols = getattr(value, "__table__", None)
    if cols is not None:  # ORM 实例
        return {str(c.name): getattr(value, c.name) for c in cols.columns}
    return cast("Row", dict(value))


def _rows(result: Any) -> list[Row]:
    return [_row(r) for r in result.fetchall()]


class _Repo:
    """所有 MySQL Repo 的公共基类：持有读写引擎。"""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def _execute(self, stmt: Any, *, many: list[Row] | None = None) -> Any:
        """在**自动提交事务**中执行写语句，返回 Result。

        ``many`` 非空时走 Core executemany（高写入批量路径，如 ``llm_call``/``tool_call``）。
        """
        try:
            async with self._engine.begin() as conn:
                if many is not None:
                    return await conn.execute(stmt, many)
                return await conn.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError(
                f"MySQL 写入失败：{exc}", detail={"component": "mysql"}
            ) from exc

    async def _fetch(self, stmt: Any) -> list[Row]:
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(stmt)
                return _rows(result)
        except Exception as exc:  # noqa: BLE001
            raise StorageUnavailableError(
                f"MySQL 查询失败：{exc}", detail={"component": "mysql"}
            ) from exc

    async def _fetch_one(self, stmt: Any) -> Row | None:
        rows = await self._fetch(stmt)
        return rows[0] if rows else None


# ═════════════════════════════════════════════════════════════════════
# 组织 / 权限 / 语义（owner: capabilities.semantics）
# ═════════════════════════════════════════════════════════════════════
class BizLineRepo(_Repo):
    async def get(self, biz_line_id: int) -> Row | None:
        return await self._fetch_one(
            sa.select(m.BizLine.__table__).where(m.BizLine.id == biz_line_id)
        )

    async def list_active(self) -> list[Row]:
        stmt = sa.select(m.BizLine.__table__).where(
            m.BizLine.status == 1, m.BizLine.deleted_at.is_(None)
        )
        return await self._fetch(stmt)


class AuthUserRepo(_Repo):
    async def by_username(self, username: str) -> Row | None:
        stmt = sa.select(m.AuthUser.__table__).where(
            m.AuthUser.username == username, m.AuthUser.deleted_at.is_(None)
        )
        return await self._fetch_one(stmt)

    async def get(self, user_id: int) -> Row | None:
        return await self._fetch_one(
            sa.select(m.AuthUser.__table__).where(m.AuthUser.id == user_id)
        )


class AuthRoleRepo(_Repo):
    async def roles_of_user(self, user_id: int) -> list[Row]:
        stmt = (
            sa.select(m.AuthRole.__table__)
            .join(m.AuthUserRole.__table__, m.AuthUserRole.role_id == m.AuthRole.id)
            .where(m.AuthUserRole.user_id == user_id)
        )
        return await self._fetch(stmt)

    async def role_ids_of_user(self, user_id: int) -> list[int]:
        stmt = sa.select(m.AuthUserRole.role_id).where(m.AuthUserRole.user_id == user_id)
        rows = await self._fetch(stmt)
        return [int(r["role_id"]) for r in rows]


class UserRoleRepo(_Repo):
    async def grant(self, user_id: int, role_id: int) -> None:
        # 幂等授予：重复授予不报错（用 INSERT IGNORE 语义）
        stmt = (
            sa.insert(m.AuthUserRole).prefix_with("IGNORE").values(user_id=user_id, role_id=role_id)
        )
        await self._execute(stmt)


class RowScopeRuleRepo(_Repo):
    async def rules_for_roles(self, role_ids: list[int]) -> list[Row]:
        if not role_ids:
            return []
        stmt = (
            sa.select(m.RowScopeRule.__table__)
            .where(m.RowScopeRule.role_id.in_(role_ids), m.RowScopeRule.enabled == 1)
            .order_by(m.RowScopeRule.priority.asc())
        )
        return await self._fetch(stmt)

    async def rule_version(self, role_ids: list[int]) -> str | None:
        """★ P0-10：该角色集 ``row_scope_rule`` 的最大 ``updated_at``（ISO8601）。

        参与 ``scope_hash``；不然改了规则 hash 不变 → 继续命中旧权限缓存（越权/漏权）。
        """
        if not role_ids:
            return None
        stmt = sa.select(sa.func.max(m.RowScopeRule.updated_at)).where(
            m.RowScopeRule.role_id.in_(role_ids), m.RowScopeRule.enabled == 1
        )
        async with self._engine.connect() as conn:
            value = (await conn.execute(stmt)).scalar()
        if value is None:
            return None
        assert isinstance(value, dt.datetime)
        return value.isoformat(timespec="milliseconds")


class SemMetricRepo(_Repo):
    async def search(self, keyword: str, biz_line_id: int | None, limit: int = 8) -> list[Row]:
        like = f"%{keyword}%"
        conds = [
            m.SemMetric.status == 1,
            sa.or_(
                m.SemMetric.metric_code.like(like),
                m.SemMetric.metric_name.like(like),
                m.SemMetric.caliber_desc.like(like),
            ),
        ]
        if biz_line_id is not None:
            # 业务线内 + 全局（NULL）口径都算命中
            conds.append(
                sa.or_(m.SemMetric.biz_line_id == biz_line_id, m.SemMetric.biz_line_id.is_(None))
            )
        stmt = sa.select(m.SemMetric.__table__).where(*conds).limit(limit)
        return await self._fetch(stmt)

    async def by_codes(self, codes: list[str], biz_line_id: int | None) -> list[Row]:
        if not codes:
            return []
        conds = [m.SemMetric.metric_code.in_(codes), m.SemMetric.status == 1]
        if biz_line_id is not None:
            conds.append(
                sa.or_(m.SemMetric.biz_line_id == biz_line_id, m.SemMetric.biz_line_id.is_(None))
            )
        stmt = sa.select(m.SemMetric.__table__).where(*conds)
        return await self._fetch(stmt)


class SemFieldMappingRepo(_Repo):
    async def by_logical(self, logical_field: str) -> list[Row]:
        stmt = sa.select(m.SemFieldMapping.__table__).where(
            m.SemFieldMapping.logical_field == logical_field
        )
        return await self._fetch(stmt)

    async def all_mappings(self) -> list[Row]:
        return await self._fetch(sa.select(m.SemFieldMapping.__table__))


class SemDictEntryRepo(_Repo):
    async def search(self, term: str, biz_line_id: int | None) -> list[Row]:
        like = f"%{term}%"
        conds = [sa.or_(m.SemDictEntry.term.like(like))]
        if biz_line_id is not None:
            conds.append(
                sa.or_(
                    m.SemDictEntry.biz_line_id == biz_line_id, m.SemDictEntry.biz_line_id.is_(None)
                )
            )
        stmt = sa.select(m.SemDictEntry.__table__).where(*conds)
        return await self._fetch(stmt)


# ═════════════════════════════════════════════════════════════════════
# Run 埋点与成本明细（owner: capabilities.telemetry / capabilities.cost）
# ═════════════════════════════════════════════════════════════════════
class RunRepo(_Repo):
    async def insert(self, row: Row) -> None:
        await self._execute(sa.insert(m.Run).values(**row))

    async def finish(self, trace_id: str, *, started_at: dt.datetime, row: Row) -> int:
        """收尾一次 Run（★ 必须带 ``started_at`` 才能命中分区）。返回受影响行数。"""
        stmt = (
            sa.update(m.Run)
            .where(m.Run.trace_id == trace_id, m.Run.started_at == started_at)
            .values(**row)
        )
        result = await self._execute(stmt)
        return int(result.rowcount or 0)

    async def get(self, trace_id: str, *, since: dt.datetime, until: dt.datetime) -> Row | None:
        stmt = sa.select(m.Run.__table__).where(
            m.Run.trace_id == trace_id,
            m.Run.started_at >= since,
            m.Run.started_at < until,
        )
        return await self._fetch_one(stmt)

    async def list_runs(self, flt: Row, *, limit: int = 50) -> list[Row]:
        stmt = sa.select(m.Run.__table__)
        if flt.get("module"):
            stmt = stmt.where(m.Run.module == flt["module"])
        if flt.get("biz_line_id") is not None:
            stmt = stmt.where(m.Run.biz_line_id == flt["biz_line_id"])
        if flt.get("status"):
            stmt = stmt.where(m.Run.status == flt["status"])
        if flt.get("since") is not None:
            stmt = stmt.where(m.Run.started_at >= flt["since"])
        if flt.get("until") is not None:
            stmt = stmt.where(m.Run.started_at < flt["until"])
        stmt = stmt.order_by(m.Run.started_at.desc()).limit(limit)
        return await self._fetch(stmt)


class LlmCallRepo(_Repo):
    async def bulk_insert(self, rows: list[Row]) -> int:
        if not rows:
            return 0
        # 高写入：Core executemany（一条 prepared statement 多值），不开 ORM 会话
        result = await self._execute(sa.insert(m.LlmCall), many=rows)
        return int(result.rowcount or 0)


class ToolCallRepo(_Repo):
    async def bulk_insert(self, rows: list[Row]) -> int:
        if not rows:
            return 0
        result = await self._execute(sa.insert(m.ToolCall), many=rows)
        return int(result.rowcount or 0)


class BudgetReservationRepo(_Repo):
    """★ P0-4：预留明细的**权威账本**（幂等 insert / settle / release）。"""

    async def insert_if_absent(self, row: Row) -> bool:
        stmt = (
            sa.insert(m.BudgetReservation)
            .prefix_with("IGNORE")  # 同 reservation_id 已存在则不覆盖
            .values(**row)
        )
        result = await self._execute(stmt)
        return int(result.rowcount or 0) > 0

    async def settle(self, reservation_id: str, actual_micro_usd: int) -> Row:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        # 条件更新：仅当处于 RESERVED 才能结算（幂等 + 并发安全）
        upd = (
            sa.update(m.BudgetReservation)
            .where(
                m.BudgetReservation.reservation_id == reservation_id,
                m.BudgetReservation.state == "RESERVED",
            )
            .values(state="SETTLED", actual_micro_usd=actual_micro_usd, settled_at=now)
        )
        result = await self._execute(upd)
        changed = int(result.rowcount or 0) > 0
        current = await self._fetch_one(
            sa.select(m.BudgetReservation.__table__).where(
                m.BudgetReservation.reservation_id == reservation_id
            )
        )
        if current is None:
            return {"status": "reservation_missing", "estimated_micro_usd": 0}
        if changed:
            return {"status": "settled", "estimated_micro_usd": int(current["estimated_micro_usd"])}
        # 已结算 / 已释放 / 已过期：不再改数，显式返回状态供上层对账
        return {
            "status": f"already_{str(current['state']).lower()}",
            "estimated_micro_usd": int(current["estimated_micro_usd"]),
        }

    async def release(self, reservation_id: str) -> Row:
        upd = (
            sa.update(m.BudgetReservation)
            .where(
                m.BudgetReservation.reservation_id == reservation_id,
                m.BudgetReservation.state == "RESERVED",
            )
            .values(state="RELEASED", settled_at=dt.datetime.now(dt.UTC).replace(tzinfo=None))
        )
        result = await self._execute(upd)
        changed = int(result.rowcount or 0) > 0
        current = await self._fetch_one(
            sa.select(m.BudgetReservation.__table__).where(
                m.BudgetReservation.reservation_id == reservation_id
            )
        )
        if current is None:
            return {"status": "reservation_missing", "estimated_micro_usd": 0}
        if changed:
            return {
                "status": "released",
                "estimated_micro_usd": int(current["estimated_micro_usd"]),
            }
        return {
            "status": f"already_{str(current['state']).lower()}",
            "estimated_micro_usd": int(current["estimated_micro_usd"]),
        }

    async def sum_reserved(self, budget_id: int, period_start: dt.date) -> int:
        stmt = sa.select(
            sa.func.coalesce(sa.func.sum(m.BudgetReservation.estimated_micro_usd), 0)
        ).where(
            m.BudgetReservation.budget_id == budget_id,
            m.BudgetReservation.period_start == period_start,
            m.BudgetReservation.state == "RESERVED",
        )
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar() or 0)

    async def expire_stale(self, now: dt.datetime) -> list[Row]:
        """把超期未结算的预留标记为 EXPIRED，返回被过期的行（供上层退回额度）。"""
        rows = await self._fetch(
            sa.select(m.BudgetReservation.__table__).where(
                m.BudgetReservation.state == "RESERVED",
                m.BudgetReservation.expires_at < now,
            )
        )
        if not rows:
            return []
        ids = [r["reservation_id"] for r in rows]
        await self._execute(
            sa.update(m.BudgetReservation)
            .where(
                m.BudgetReservation.reservation_id.in_(ids), m.BudgetReservation.state == "RESERVED"
            )
            .values(state="EXPIRED", settled_at=now)
        )
        return rows


class PriceBookRepo(_Repo):
    async def lookup(self, provider: str, model: str, at: dt.datetime) -> Row | None:
        stmt = (
            sa.select(m.PriceBook.__table__)
            .where(
                m.PriceBook.provider == provider,
                m.PriceBook.model == model,
                m.PriceBook.effective_from <= at,
                sa.or_(m.PriceBook.effective_to.is_(None), m.PriceBook.effective_to > at),
            )
            .order_by(m.PriceBook.effective_from.desc())
            .limit(1)
        )
        return await self._fetch_one(stmt)

    async def upsert(self, row: Row) -> None:
        await self._execute(sa.insert(m.PriceBook).values(**row))

    async def list_active(self) -> list[Row]:
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        stmt = sa.select(m.PriceBook.__table__).where(
            m.PriceBook.effective_from <= now,
            sa.or_(m.PriceBook.effective_to.is_(None), m.PriceBook.effective_to > now),
        )
        return await self._fetch(stmt)


class OutboxRepo(_Repo):
    """★ P1-6：**禁止内存队列**——埋点先落本表，再异步投递。"""

    async def enqueue(self, rows: list[Row]) -> None:
        if not rows:
            return
        await self._execute(sa.insert(m.Outbox), many=rows)

    async def claim(self, *, limit: int = 200) -> list[Row]:
        """原子领取一批待投递记录。

        ``SELECT ... FOR UPDATE SKIP LOCKED``（多副本不重复抢）**并在同一事务内**
        置为 ``processing``，避免「事务提交后锁释放、另一副本又抢到同一批」。
        """
        async with self._engine.begin() as conn:
            rows = [
                _row(r)
                for r in (
                    await conn.execute(
                        sa.select(m.Outbox.__table__)
                        .where(m.Outbox.status == "pending")
                        .order_by(m.Outbox.id.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).fetchall()
            ]
            if rows:
                ids = [int(r["id"]) for r in rows]
                await conn.execute(
                    sa.update(m.Outbox).where(m.Outbox.id.in_(ids)).values(status="processing")
                )
            return rows

    async def mark_done(self, ids: list[int]) -> int:
        if not ids:
            return 0
        result = await self._execute(
            sa.update(m.Outbox).where(m.Outbox.id.in_(ids)).values(status="done")
        )
        return int(result.rowcount or 0)

    async def mark_retry(self, outbox_id: int, error: str) -> None:
        """重试：attempts+1、记录错误，状态**回到 pending**（下轮继续投递）。"""
        await self._execute(
            sa.update(m.Outbox)
            .where(m.Outbox.id == outbox_id)
            .values(status="pending", last_error=error[:500], attempts=m.Outbox.attempts + 1)
        )

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """终态失败：attempts+1、置 failed（DLQ 由调用方落盘）。"""
        await self._execute(
            sa.update(m.Outbox)
            .where(m.Outbox.id == outbox_id)
            .values(status="failed", last_error=error[:500], attempts=m.Outbox.attempts + 1)
        )

    async def pending_count(self) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(m.Outbox.__table__)
            .where(m.Outbox.status.in_(("pending", "processing")))
        )
        async with self._engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar() or 0)


# ═════════════════════════════════════════════════════════════════════
# 预算（owner: capabilities.budget）
# ═════════════════════════════════════════════════════════════════════
class BudgetRepo(_Repo):
    async def effective(self, scope_type: str, scope_id: str, period: str) -> Row | None:
        """★ U15：取 ``enabled=1`` 且 ``version`` 最大的一行（改了预算 = 新版本）。"""
        stmt = (
            sa.select(m.Budget.__table__)
            .where(
                m.Budget.scope_type == scope_type,
                m.Budget.scope_id == scope_id,
                m.Budget.period == period,
                m.Budget.enabled == 1,
            )
            .order_by(m.Budget.version.desc(), m.Budget.id.desc())
            .limit(1)
        )
        return await self._fetch_one(stmt)

    async def by_id(self, budget_id: int) -> Row | None:
        return await self._fetch_one(sa.select(m.Budget.__table__).where(m.Budget.id == budget_id))

    async def upsert(self, row: Row) -> int:
        """按 ``(scope_type, scope_id, period, version)`` 幂等写入，返回 ``budget.id``。"""
        existing = await self._fetch_one(
            sa.select(m.Budget.__table__).where(
                m.Budget.scope_type == row["scope_type"],
                m.Budget.scope_id == row["scope_id"],
                m.Budget.period == row["period"],
                m.Budget.version == row.get("version", 1),
            )
        )
        if existing is not None:
            return int(existing["id"])
        result = await self._execute(sa.insert(m.Budget).values(**row))
        return int(result.inserted_primary_key[0])


class BudgetUsageRepo(_Repo):
    async def _ensure_row(self, conn: Any, budget_id: int, period_start: dt.date) -> None:
        """保证 usage 行存在。

        ★ Redis 快路径下首次结算时，MySQL 侧可能**还没有** usage 行
        （预留只在 Redis 增 reserved、MySQL 只落 reservation 明细）——若直接 UPDATE
        会命中 0 行，随后 ``_read_usage`` 读到 ``None`` 而崩溃/漏记。
        """
        await conn.execute(
            sa.insert(m.BudgetUsage)
            .prefix_with("IGNORE")
            .values(budget_id=budget_id, period_start=period_start)
        )

    async def get_or_create(self, budget_id: int, period_start: dt.date) -> Row:
        async with self._engine.begin() as conn:
            await self._ensure_row(conn, budget_id, period_start)
            return await self._read_usage(conn, budget_id, period_start)

    async def apply_reserve(self, budget_id: int, period_start: dt.date, amount: int) -> Row:
        """预留：``reserved += amount``；同时在 MySQL 侧做**原子上限校验**（P1-9 兜底）。"""
        async with self._engine.begin() as conn:
            await self._ensure_row(conn, budget_id, period_start)
            await conn.execute(
                sa.update(m.BudgetUsage)
                .where(
                    m.BudgetUsage.budget_id == budget_id,
                    m.BudgetUsage.period_start == period_start,
                )
                .values(
                    reserved_micro_usd=m.BudgetUsage.reserved_micro_usd + amount,
                    call_count=m.BudgetUsage.call_count + 1,
                )
            )
            return await self._read_usage(conn, budget_id, period_start)

    async def reserve_atomic(
        self, budget_id: int, period_start: dt.date, *, amount: int, hard_limit: int
    ) -> Row:
        """★ P1-9：**行锁下**原子「判断上限 + 预留」（Redis fail-open 的兜底路径）。

        在**同一事务**里先 ``SELECT ... FOR UPDATE`` 锁住 usage 行，再判断
        ``consumed + reserved + amount <= hard_limit``：满足才 ``reserved += amount``，
        否则**不改数**直接返回 ``granted=False``。InnoDB 行锁保证并发不超卖。
        """
        async with self._engine.begin() as conn:
            await self._ensure_row(conn, budget_id, period_start)
            row = (
                await conn.execute(
                    sa.select(m.BudgetUsage.__table__)
                    .where(
                        m.BudgetUsage.budget_id == budget_id,
                        m.BudgetUsage.period_start == period_start,
                    )
                    .with_for_update()
                )
            ).fetchone()
            current = _row(row)
            consumed = int(current.get("consumed_micro_usd", 0) or 0)
            reserved = int(current.get("reserved_micro_usd", 0) or 0)
            used = consumed + reserved
            if used + amount > hard_limit:
                return {"granted": False, "used": used, "consumed": consumed, "reserved": reserved}
            await conn.execute(
                sa.update(m.BudgetUsage)
                .where(
                    m.BudgetUsage.budget_id == budget_id,
                    m.BudgetUsage.period_start == period_start,
                )
                .values(
                    reserved_micro_usd=m.BudgetUsage.reserved_micro_usd + amount,
                    call_count=m.BudgetUsage.call_count + 1,
                )
            )
            return {
                "granted": True,
                "used": used + amount,
                "consumed": consumed,
                "reserved": reserved + amount,
            }

    async def apply_settle(
        self, budget_id: int, period_start: dt.date, *, released: int, actual: int
    ) -> Row:
        """结算：``reserved -= released``，``consumed += actual``（同一事务内原子）。"""
        async with self._engine.begin() as conn:
            await self._ensure_row(conn, budget_id, period_start)
            await conn.execute(
                sa.update(m.BudgetUsage)
                .where(
                    m.BudgetUsage.budget_id == budget_id,
                    m.BudgetUsage.period_start == period_start,
                )
                .values(
                    consumed_micro_usd=m.BudgetUsage.consumed_micro_usd + actual,
                    # ★ 列是 BIGINT UNSIGNED：直接相减会在下溢时抛 1690，
                    #   必须先 CAST 成 SIGNED 再 GREATEST(...,0) 兜底。
                    reserved_micro_usd=sa.func.greatest(
                        sa.cast(m.BudgetUsage.reserved_micro_usd, sa.BigInteger) - released, 0
                    ),
                )
            )
            return await self._read_usage(conn, budget_id, period_start)

    async def apply_release(self, budget_id: int, period_start: dt.date, amount: int) -> Row:
        """释放：``reserved -= amount``（下限 0，避免负数）。"""
        async with self._engine.begin() as conn:
            await self._ensure_row(conn, budget_id, period_start)
            await conn.execute(
                sa.update(m.BudgetUsage)
                .where(
                    m.BudgetUsage.budget_id == budget_id,
                    m.BudgetUsage.period_start == period_start,
                )
                .values(
                    reserved_micro_usd=sa.func.greatest(
                        sa.cast(m.BudgetUsage.reserved_micro_usd, sa.BigInteger) - amount, 0
                    )
                )
            )
            return await self._read_usage(conn, budget_id, period_start)

    async def get(self, budget_id: int, period_start: dt.date) -> Row | None:
        return await self._fetch_one(
            sa.select(m.BudgetUsage.__table__).where(
                m.BudgetUsage.budget_id == budget_id,
                m.BudgetUsage.period_start == period_start,
            )
        )

    async def set_breaker(self, budget_id: int, period_start: dt.date, state: str) -> None:
        values: Row = {"breaker_state": state}
        if state == "OPEN":
            values["breaker_opened_at"] = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        await self._execute(
            sa.update(m.BudgetUsage)
            .where(
                m.BudgetUsage.budget_id == budget_id,
                m.BudgetUsage.period_start == period_start,
            )
            .values(**values)
        )

    async def _read_usage(self, conn: Any, budget_id: int, period_start: dt.date) -> Row:
        result = await conn.execute(
            sa.select(m.BudgetUsage.__table__).where(
                m.BudgetUsage.budget_id == budget_id,
                m.BudgetUsage.period_start == period_start,
            )
        )
        return _row(result.fetchone())


# ═════════════════════════════════════════════════════════════════════
# 技能（owner: capabilities.skills）
# ═════════════════════════════════════════════════════════════════════
class SkillRegistryRepo(_Repo):
    async def by_key(self, skill_key: str, version: int | None = None) -> Row | None:
        stmt = sa.select(m.SkillRegistry.__table__).where(
            m.SkillRegistry.skill_key == skill_key, m.SkillRegistry.status == "active"
        )
        if version is not None:
            stmt = stmt.where(m.SkillRegistry.version == version)
        stmt = stmt.order_by(m.SkillRegistry.version.desc()).limit(1)
        return await self._fetch_one(stmt)

    async def match_intent(self, intent: str, biz_line_id: int | None) -> list[Row]:
        like = f"%{intent}%"
        conds = [
            m.SkillRegistry.status == "active",
            m.SkillRegistry.is_dead == 0,
            sa.or_(
                m.SkillRegistry.name.like(like),
                sa.func.json_extract(m.SkillRegistry.meta_json, "$.intent").like(like),
            ),
        ]
        if biz_line_id is not None:
            conds.append(
                sa.or_(
                    m.SkillRegistry.biz_line_id == biz_line_id,
                    m.SkillRegistry.biz_line_id.is_(None),
                )
            )
        stmt = sa.select(m.SkillRegistry.__table__).where(*conds).limit(10)
        return await self._fetch(stmt)

    async def upsert(self, row: Row) -> int:
        existing = await self._fetch_one(
            sa.select(m.SkillRegistry.__table__).where(
                m.SkillRegistry.skill_key == row["skill_key"],
                m.SkillRegistry.version == row.get("version", 1),
            )
        )
        if existing is not None:
            await self._execute(
                sa.update(m.SkillRegistry)
                .where(m.SkillRegistry.id == existing["id"])
                .values(**{k: v for k, v in row.items() if k not in {"id"}})
            )
            return int(existing["id"])
        result = await self._execute(sa.insert(m.SkillRegistry).values(**row))
        return int(result.inserted_primary_key[0])

    async def stats(self, biz_line_id: int | None) -> Row:
        conds = [] if biz_line_id is None else [m.SkillRegistry.biz_line_id == biz_line_id]
        stmt = sa.select(
            sa.func.count().label("skill_total"),
            sa.func.coalesce(sa.func.sum(m.SkillRegistry.usage_count), 0).label("usage_count"),
            sa.func.coalesce(sa.func.sum(m.SkillRegistry.reuse_count), 0).label("reuse_count"),
            sa.func.coalesce(sa.func.sum(m.SkillRegistry.is_dead), 0).label("dead_skill_count"),
        ).where(*conds)
        row = await self._fetch_one(stmt)
        return row or {}


class SkillUsageRepo(_Repo):
    """★ P1-2：``UNIQUE(skill_id, trace_id)`` + upsert。

    自愈重试会让同一 trace 重复经过同一技能；v1 每次 INSERT 都会**灌水复用率**。
    v2 用唯一键 + ``ON DUPLICATE KEY UPDATE`` 保证「同一 trace 同一技能只记一条」。
    """

    async def record(
        self,
        *,
        skill_id: int,
        trace_id: str,
        biz_line_id: int | None,
        is_reuse: bool,
        tokens_saved_est: int = 0,
        baseline_cost_micro_usd: int = 0,
    ) -> None:
        values: Row = {
            "skill_id": skill_id,
            "trace_id": trace_id,
            "biz_line_id": biz_line_id,
            "is_reuse": 1 if is_reuse else 0,
            "tokens_saved_est": tokens_saved_est,
            "baseline_cost_micro_usd": baseline_cost_micro_usd,
        }
        stmt = mysql_insert(m.SkillUsage).values(**values)
        # ON DUPLICATE KEY UPDATE：同 (skill_id, trace_id) 更新为最新一次的结果，不新增行
        update_cols = {
            "is_reuse": stmt.inserted.is_reuse,
            "tokens_saved_est": stmt.inserted.tokens_saved_est,
            "baseline_cost_micro_usd": stmt.inserted.baseline_cost_micro_usd,
        }
        stmt = stmt.on_duplicate_key_update(**update_cols)
        await self._execute(stmt)


# ═════════════════════════════════════════════════════════════════════
# 观测（owner: modules.observability）
# ═════════════════════════════════════════════════════════════════════
class AppAgentRepo(_Repo):
    async def list_agents(self, flt: Row) -> list[Row]:
        stmt = sa.select(m.AppAgent.__table__)
        if flt.get("biz_line_id") is not None:
            stmt = stmt.where(m.AppAgent.biz_line_id == flt["biz_line_id"])
        if flt.get("status"):
            stmt = stmt.where(m.AppAgent.status == flt["status"])
        return await self._fetch(stmt)

    async def upsert(self, row: Row) -> None:
        existing = await self._fetch_one(
            sa.select(m.AppAgent.__table__).where(m.AppAgent.agent_uid == row["agent_uid"])
        )
        if existing is not None:
            await self._execute(
                sa.update(m.AppAgent)
                .where(m.AppAgent.agent_uid == row["agent_uid"])
                .values(**{k: v for k, v in row.items() if k != "id"})
            )
            return
        await self._execute(sa.insert(m.AppAgent).values(**row))

    async def heartbeat(self, agent_uid: str, at: dt.datetime) -> None:
        await self._execute(
            sa.update(m.AppAgent)
            .where(m.AppAgent.agent_uid == agent_uid)
            .values(last_heartbeat_at=at)
        )


class AlertEventRepo(_Repo):
    async def insert(self, row: Row) -> int:
        result = await self._execute(sa.insert(m.AlertEvent).values(**row))
        return int(result.inserted_primary_key[0])

    async def list_events(self, flt: Row) -> list[Row]:
        stmt = sa.select(m.AlertEvent.__table__)
        if flt.get("status"):
            stmt = stmt.where(m.AlertEvent.status == flt["status"])
        if flt.get("severity"):
            stmt = stmt.where(m.AlertEvent.severity == flt["severity"])
        if flt.get("category"):
            stmt = stmt.where(m.AlertEvent.category == flt["category"])
        stmt = stmt.order_by(m.AlertEvent.detected_at.desc()).limit(int(flt.get("limit", 100)))
        return await self._fetch(stmt)

    async def ack(self, alert_id: int, user_id: int) -> int:
        result = await self._execute(
            sa.update(m.AlertEvent)
            .where(m.AlertEvent.id == alert_id, m.AlertEvent.status == "open")
            .values(
                status="acked",
                acked_by=user_id,
                acked_at=dt.datetime.now(dt.UTC).replace(tzinfo=None),
            )
        )
        return int(result.rowcount or 0)


class MetricDailyRepo(_Repo):
    async def upsert_many(self, rows: list[Row]) -> int:
        if not rows:
            return 0
        total = 0
        async with self._engine.begin() as conn:
            for row in rows:
                stmt = mysql_insert(m.MetricDaily).values(**row)
                # ★ 用显式 VALUES(col)：SQLAlchemy 的 ``stmt.inserted`` 在 MySQL 8.4 下
                #   会生成 ``AS new`` + ``new.col``，但别名缺失 → 报
                #   "Unknown column 'new.p50_latency_ms' in 'field list'"（实测）。
                update_cols = {
                    c.name: sa.text(f"VALUES({c.name})")
                    for c in m.MetricDaily.__table__.columns
                    if c.name not in {"stat_date", "biz_line_id", "agent_uid", "model"}
                }
                await conn.execute(stmt.on_duplicate_key_update(**update_cols))
                total += 1
        return total

    async def timeseries(self, flt: Row) -> list[Row]:
        stmt = sa.select(m.MetricDaily.__table__).where(
            m.MetricDaily.stat_date >= flt["since"], m.MetricDaily.stat_date <= flt["until"]
        )
        if flt.get("biz_line_id") is not None:
            # ★ P1-1：哨兵 0 表示「全业务线」，与指定业务线一起返回
            stmt = stmt.where(
                sa.or_(
                    m.MetricDaily.biz_line_id == flt["biz_line_id"], m.MetricDaily.biz_line_id == 0
                )
            )
        if flt.get("model"):
            stmt = stmt.where(m.MetricDaily.model == flt["model"])
        stmt = stmt.order_by(m.MetricDaily.stat_date.asc())
        return await self._fetch(stmt)


# ═════════════════════════════════════════════════════════════════════
# ChatBI（owner: modules.chatbi）
# ═════════════════════════════════════════════════════════════════════
class SqlAuditRepo(_Repo):
    async def insert(self, row: Row) -> int:
        result = await self._execute(sa.insert(m.SqlAudit).values(**row))
        return int(result.inserted_primary_key[0])

    async def query(self, flt: Row) -> list[Row]:
        stmt = sa.select(m.SqlAudit.__table__)
        if flt.get("trace_id"):
            stmt = stmt.where(m.SqlAudit.trace_id == flt["trace_id"])
        if flt.get("decision"):
            stmt = stmt.where(m.SqlAudit.decision == flt["decision"])
        stmt = stmt.order_by(m.SqlAudit.created_at.desc()).limit(int(flt.get("limit", 100)))
        return await self._fetch(stmt)

    async def write(
        self,
        *,
        trace_id: str,
        sql_text: str,
        decision: str,
        rewritten_sql: str | None = None,
        guard_stage: str | None = None,
        deny_reason: str | None = None,
        code: str | None = None,
        scope_injected: bool = False,
        scope_hash: str | None = None,
        rows_returned: int | None = None,
        exec_ms: int | None = None,
        user_id: int | None = None,
        biz_line_id: int | None = None,
    ) -> int:
        """写一条 SQL 审计（护栏 deny/rewrite、执行审计共用）。"""
        # sql_audit 无独立 code 列：把符号错误码折进 deny_reason，便于按码检索。
        reason = f"[{code}] {deny_reason}" if code and deny_reason else deny_reason
        # ★ deny_reason 是 VARCHAR(255)，而护栏会把「整条 SQL + 完整报错」塞进来（常 > 500 字符）
        #   → 不截断会 DataError(1406)，令审计写入失败，进而把整个 Run 打成 STORAGE_UNAVAILABLE
        #   （实测：模型写出未知列 → dry-run 报错 → 审计写入 1406 → 整次查询 500）。
        max_reason = 255
        if reason and len(reason) > max_reason:
            reason = reason[: max_reason - 1] + "…"
        row: Row = {
            "trace_id": trace_id,
            "user_id": user_id,
            "biz_line_id": biz_line_id,
            "sql_fingerprint": hashlib.sha256(sql_text.encode("utf-8")).hexdigest()[:64],
            "sql_text": sql_text,
            "rewritten_sql": rewritten_sql,
            "decision": decision,
            "deny_reason": reason,
            "guard_stage": guard_stage,
            "scope_injected": 1 if scope_injected else 0,
            "scope_hash": scope_hash,
            "rows_returned": rows_returned,
            "exec_ms": exec_ms,
        }
        return await self.insert(row)

    async def mark_truncated(self, trace_id: str, rows_returned: int, max_rows: int) -> int:
        """标记「最近一条该 trace 的审计被截断」（写进 deny_reason，保留审计可读性）。

        ★ MySQL 限制：不能在 ``UPDATE t`` 的子查询里直接 ``SELECT`` 同一张 ``t``
        （错误 1093 ``You can't specify target table ... for update in FROM clause``）。
        故把 ``MAX(id)`` 包成**派生表**（多一层 ``SELECT ... FROM (...) AS x``）——
        MySQL 会先物化派生表，从而绕开 1093。实测：AST 未变、行为等价、rowcount=1。
        """
        latest = (
            sa.select(sa.func.max(m.SqlAudit.id).label("id"))
            .where(m.SqlAudit.trace_id == trace_id)
            .subquery()
        )
        stmt = (
            sa.update(m.SqlAudit)
            .where(m.SqlAudit.id == sa.select(latest.c.id).scalar_subquery())
            .values(
                rows_returned=rows_returned,
                deny_reason=sa.func.concat_ws(
                    "; ", m.SqlAudit.deny_reason, f"truncated:rows>{max_rows}"
                ),
            )
        )
        result = await self._execute(stmt)
        return int(result.rowcount or 0)


# ═════════════════════════════════════════════════════════════════════
# 聚合：DI 容器按 key 取用（owner → repo 的映射在此集中，便于审计）
# ═════════════════════════════════════════════════════════════════════
class MysqlRepositories:
    """一次性构造全部 MySQL Repo（共享同一个读写引擎）。"""

    def __init__(self, engine: Any) -> None:
        self.biz_line = BizLineRepo(engine)
        self.auth_user = AuthUserRepo(engine)
        self.auth_role = AuthRoleRepo(engine)
        self.user_role = UserRoleRepo(engine)
        self.row_scope_rule = RowScopeRuleRepo(engine)
        self.sem_metric = SemMetricRepo(engine)
        self.sem_field_mapping = SemFieldMappingRepo(engine)
        self.sem_dict_entry = SemDictEntryRepo(engine)
        self.run = RunRepo(engine)
        self.llm_call = LlmCallRepo(engine)
        self.tool_call = ToolCallRepo(engine)
        self.budget_reservation = BudgetReservationRepo(engine)
        self.price_book = PriceBookRepo(engine)
        self.outbox = OutboxRepo(engine)
        self.budget = BudgetRepo(engine)
        self.budget_usage = BudgetUsageRepo(engine)
        self.skill_registry = SkillRegistryRepo(engine)
        self.skill_usage = SkillUsageRepo(engine)
        self.app_agent = AppAgentRepo(engine)
        self.alert_event = AlertEventRepo(engine)
        self.metric_daily = MetricDailyRepo(engine)
        self.sql_audit = SqlAuditRepo(engine)


_ = IntegrityError  # 供将来冲突重试使用，避免未使用告警
