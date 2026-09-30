"""CI 门槛：把 ``evals/thresholds.yaml`` 变成可判定的 ``GateCheck``（§12.4）。

约定
----
* YAML 被**展平**为点号键：``chatbi.execution_accuracy.overall`` 等；
* 上下界由键名推断：
  * 以 ``_max`` / ``_ms`` / ``_allowed`` 结尾 → **上界**（actual ≤ threshold）；
  * 其余 → **下界**（actual ≥ threshold）。
* ``bool`` 门槛折算为 1.0/0.0 参与比较。

★ 为什么门槛要写进文件而不是代码：避免「口头达标」——评审时能当场打开文件对齐。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import GateCheck

__all__ = ["load_thresholds", "make_check", "flatten"]

_UPPER_SUFFIXES = ("_max", "_ms", "_allowed")


def flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, float]:
    """把嵌套 dict 展平为 ``dotted.key -> float``（忽略非数值叶子）。"""
    out: dict[str, float] = {}
    for key, value in tree.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten(value, dotted))
        elif isinstance(value, bool):
            out[dotted] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            out[dotted] = float(value)
    return out


def load_thresholds(path: Path) -> dict[str, float]:
    """读取并展平门槛文件；文件缺失抛 ``FileNotFoundError``。"""
    if not path.exists():
        raise FileNotFoundError(f"未找到门槛文件：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"门槛文件根节点必须是映射：{path}")
    return flatten(raw)


def make_check(name: str, actual: float, threshold: float) -> GateCheck:
    """按键名推断上下界并给出判定。"""
    upper = name.endswith(_UPPER_SUFFIXES)
    ok = actual <= threshold if upper else actual >= threshold
    return GateCheck(name=name, actual=actual, threshold=threshold, upper_bound=upper, ok=ok)
