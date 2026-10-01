"""时间工具（全平台统一口径，禁止各处自造）。

约定（§6.2）：
* **UTC naive**：库内 ``DATETIME(3)`` 均为 UTC naive，一律用 ``utcnow_naive()`` 取当前时间；
* **时间窗半开**：``[start, end)``，避免整点边界把同一 Run 计两次。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["utcnow_naive", "recent_window"]


def utcnow_naive() -> datetime:
    """当前 UTC naive 时间（与 MySQL ``DATETIME(3)`` 一致）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def recent_window(*, days: int = 0, hours: int = 0) -> tuple[datetime, datetime]:
    """以当前时刻为右端点，回退 ``days``/``hours`` 的半开窗口 ``[start, now)``。"""
    until = utcnow_naive()
    return until - timedelta(days=days, hours=hours), until
