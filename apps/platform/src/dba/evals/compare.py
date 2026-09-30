"""结果比较器：Execution Accuracy 的**逐值**判定（§12.3 口径）。

口径（必须精确到可复现，任一放宽都会让 EX 数字失真）
----------------------------------------------------
1. **行序无关**：对两侧结果各自做**规范化后排序**再逐行比较；
2. **列序按金标 SELECT 顺序**：结果已按列顺序展开为元组（``dict`` 的插入序即 SELECT 序）；
3. **数值容差**：相对误差 ≤ 1e-6（零附近退化为绝对误差 ≤ 1e-9）；
4. **日期 UTC 日粒度**：``date`` / ``datetime`` 统一归一为 ``YYYY-MM-DD``；
5. **``NULL`` 与空串视为不同**：``None`` 与 ``""`` 是可区分的两种取值；
6. **列名不参与比较**：只比较值，不比较列名。

★ 为什么不做「字符串相等」：MySQL/DECIMAL 的数值表示（``Decimal('12.30')`` vs ``12.3``）
  与驱动层类型（``int`` vs ``float`` vs ``Decimal``）都不同一，字符串比会大面积误判。
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Any

__all__ = ["values_equal", "rows_equal", "normalize_value"]

_REL_TOL = 1e-6
_ABS_TOL = 1e-9


def normalize_value(value: Any) -> Any:
    """把单个值归一为可比较的规范形式（保留类型语义）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 的子类，必须先判；归一为 int 以免 True == 1 造成误判
        return int(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC)
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return str(value)


def values_equal(left: Any, right: Any) -> bool:
    """按 §12.3 判定两个值是否等价。"""
    left_n = normalize_value(left)
    right_n = normalize_value(right)
    if left_n is None or right_n is None:
        # NULL 只与 NULL 相等；且 NULL 与空串不同（空串会被 normalize 成 ""）
        return left_n is None and right_n is None
    if isinstance(left_n, float) and isinstance(right_n, float):
        return math.isclose(left_n, right_n, rel_tol=_REL_TOL, abs_tol=_ABS_TOL)
    return bool(left_n == right_n)


def _row_key(row: list[Any]) -> str:
    """把一行转成可排序的稳定键（排序只为消除行序差异）。"""
    return repr([normalize_value(v) for v in row])


def rows_equal(gold: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> bool:
    """判定两个结果集是否逐值一致（行序无关、列序按各自 SELECT 序）。"""
    if len(gold) != len(candidate):
        return False
    gold_rows = sorted((list(r.values()) for r in gold), key=_row_key)
    cand_rows = sorted((list(r.values()) for r in candidate), key=_row_key)
    for g_row, c_row in zip(gold_rows, cand_rows, strict=True):
        if len(g_row) != len(c_row):
            return False
        if not all(values_equal(g, c) for g, c in zip(g_row, c_row, strict=True)):
            return False
    return True
