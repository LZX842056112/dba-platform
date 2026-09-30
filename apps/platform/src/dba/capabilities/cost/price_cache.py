"""价格缓存（预热，供**同步** ``normalize`` 使用）。

对齐《设计文档 v2》§5.2.3 与《实现要点清单》U6。

★ 为什么必须预热成内存字典（照抄 v1 会怎样错）
--------------------------------------------
内核 ``@metered`` 装饰器在 ``finally`` 里调用 ``normalize``——那可能处于**协程取消路径**，
绝不能 ``await`` 数据库（v1 正是在 ``finally`` 里 await 落库，取消时整条埋点丢失）。
因此 ``normalize`` 被定为**同步**方法，价格必须提前从 ``price_book`` 载入内存字典。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

__all__ = ["PriceRow", "PriceCache"]


@dataclass(frozen=True)
class PriceRow:
    """一行价格（金额为 micro_usd）。"""

    provider: str
    model: str
    billing_unit: str
    input_price_micro_usd: int
    output_price_micro_usd: int
    cache_read_price_micro_usd: int
    cache_write_price_micro_usd: int
    currency: str
    fx_rate_to_usd: float
    effective_from: dt.datetime
    effective_to: dt.datetime | None
    price_book_id: int | None = None
    tier_json: Any = None

    @property
    def unit_size(self) -> int:
        """计价单位含多少 token（``PER_1K_TOKEN`` → 1000；``PER_CALL``/``PER_SECOND`` → 1）。"""
        return 1000 if self.billing_unit == "PER_1K_TOKEN" else 1

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PriceRow:
        fx = row.get("fx_rate_to_usd", 1.0)
        return cls(
            provider=str(row["provider"]),
            model=str(row["model"]),
            billing_unit=str(row["billing_unit"]),
            input_price_micro_usd=int(row.get("input_price_micro_usd", 0) or 0),
            output_price_micro_usd=int(row.get("output_price_micro_usd", 0) or 0),
            cache_read_price_micro_usd=int(row.get("cache_read_price_micro_usd", 0) or 0),
            cache_write_price_micro_usd=int(row.get("cache_write_price_micro_usd", 0) or 0),
            currency=str(row.get("currency", "USD")),
            fx_rate_to_usd=float(fx if not isinstance(fx, Decimal) else fx),
            effective_from=row["effective_from"],
            effective_to=row.get("effective_to"),
            price_book_id=int(row["id"]) if row.get("id") is not None else None,
            tier_json=row.get("tier_json"),
        )


class PriceCache:
    """``(provider, model)`` → ``PriceRow`` 的内存缓存（启动时预热）。"""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], PriceRow] = {}
        self._loaded_at: dt.datetime | None = None

    def load_rows(self, rows: list[dict[str, Any]]) -> int:
        """从 ``price_book`` 行集合构建缓存（同键取 ``effective_from`` 最新的一条）。"""
        cache: dict[tuple[str, str], PriceRow] = {}
        for row in rows:
            price = PriceRow.from_row(row)
            key = (price.provider, price.model)
            existing = cache.get(key)
            if existing is None or price.effective_from > existing.effective_from:
                cache[key] = price
        self._rows = cache
        self._loaded_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        return len(cache)

    async def refresh(self, repo: Any) -> int:
        """从 ``PriceBookRepo`` 重新预热（``repo`` 只依赖 Protocol）。"""
        rows = await repo.list_active()
        return self.load_rows(list(rows))

    def get(self, provider: str, model: str) -> PriceRow | None:
        return self._rows.get((provider, model))

    @property
    def loaded_at(self) -> dt.datetime | None:
        return self._loaded_at

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, tuple) and key in self._rows
