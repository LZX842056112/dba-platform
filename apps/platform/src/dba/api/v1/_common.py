"""API v1 共享工具（时间窗解析）。

finops / observability 两个 router 都用到「from/to 时间窗 + ISO8601 解析」，
统一收口到此处，避免解析规则改一处漏一处。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["parse_ts", "parse_window"]


def parse_ts(value: str | None) -> datetime | None:
    """解析 ISO8601 时间串为 naive UTC datetime；空 / 非法返回 ``None``。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def parse_window(
    from_: str | None, to: str | None, *, span: timedelta
) -> tuple[datetime, datetime]:
    """解析 ``from/to`` 时间窗；缺省回退到「近 ``span``」窗口（UTC naive）。"""
    until = parse_ts(to) or datetime.now(UTC).replace(tzinfo=None)
    since = parse_ts(from_) or (until - span)
    return since, until
