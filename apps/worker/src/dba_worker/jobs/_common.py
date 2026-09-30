"""worker 周期任务的公共工具（时间窗 / 解析 / 业务线枚举）。

对齐《设计文档 v2》§6.1.4 与《实现要点清单》§1.6.6。

统一约定（与全平台一致，禁止各处自造）：
* **UTC naive**：库内 ``DATETIME(3)`` 均为 UTC naive，任务里一律用 ``_now()`` 取当前时间；
* **金额 micro_usd**：只做整数求和，绝不做浮点累加；
* **时间窗半开**：``[start, end)``——避免整点边界把同一 Run 计两次。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

__all__ = [
    "naive_now",
    "parse_date",
    "parse_dt",
    "period_bounds",
    "active_biz_lines",
]

#: P1-1：哨兵 0 = 「全业务线/未归属」维度（与 metric_daily / Milvus 分区键保持一致）
GLOBAL_SENTINEL = 0


def naive_now() -> datetime:
    """当前 UTC naive 时间（库内一致口径）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def parse_date(value: Any) -> date | None:
    """把 ``str``/``date``/``datetime`` 解析为 ``date``；无法解析返回 ``None``。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def parse_dt(value: Any) -> datetime | None:
    """把 ``str``/``datetime`` 解析为 UTC naive ``datetime``；无法解析返回 ``None``。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def period_bounds(period: str, anchor: datetime | None = None) -> tuple[datetime, datetime]:
    """把一个周期名解析为半开区间 ``[start, end)``（UTC naive）。

    ``DAY`` / ``WEEK``（周一起）/ ``MONTH``。用于预算对账与 rollup 的时间窗。
    """
    at = anchor or naive_now()
    name = str(period).upper()
    if name == "DAY":
        start = at.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if name == "WEEK":
        start = (at - timedelta(days=at.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return start, start + timedelta(days=7)
    if name == "MONTH":
        start = at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # +32 天后取当月 1 号 = 下月 1 号（避免 28/30/31 天数差异）
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt
    raise ValueError(f"未知周期：{period!r}（仅支持 DAY/WEEK/MONTH）")


async def active_biz_lines(container: Any) -> list[int]:
    """读取启用中的业务线 ID 列表（含哨兵 0），失败时退化为 ``[0]``。"""
    repos = container.get("repos")
    if repos is None:
        return [GLOBAL_SENTINEL]
    try:
        rows: list[dict[str, Any]] = await repos.biz_line.list_active()
    except Exception:  # noqa: BLE001 - 枚举失败不阻塞任务
        return [GLOBAL_SENTINEL]
    lines = [GLOBAL_SENTINEL]
    for row in rows:
        value = row.get("id")
        if value is not None:
            lines.append(int(value))
    # 去重保序
    seen: set[int] = set()
    ordered: list[int] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            ordered.append(line)
    return ordered
