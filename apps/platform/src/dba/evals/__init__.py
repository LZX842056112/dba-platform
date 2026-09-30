"""``dba.evals``：评测框架（§12 的可执行落地）。

对外入口：``python -m dba.evals.runner``（或 ``dba eval``）。

模块边界
--------
本包位于 **L1 接入/装配层**（`dba.evals`），需要静态 import L3 模块 `dba.modules.chatbi`
来驱动流水线——这与 `dba.di` / `dba.api` 同性质，故在 ``pyproject.toml`` 的
``per-file-ignores`` 中对该目录放行 TID251。
"""

from __future__ import annotations

from .models import CaseOutcome, GateCheck, GoldenCase, GuardCase, SuiteResult

__all__ = [
    "CaseOutcome",
    "GateCheck",
    "GoldenCase",
    "GuardCase",
    "SuiteResult",
]
