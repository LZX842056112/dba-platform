"""L3 领域服务层（三模块）。

* ``chatbi``         —— 模块 01：对话式 BI（七步流水线 + 五道 SQL 护栏 + 唯一执行入口）
* ``observability``  —— 模块 03：可观测（B5）
* ``finops``         —— 模块 10：成本治理（B5）

★ 红线 1：三个模块**禁止互相 import**（由根 ``pyproject.toml`` 的 Ruff
``banned-api`` 强制）。跨模块协作只能经 L4 能力层或 ``trace_id`` 软关联。
"""

from __future__ import annotations

__all__: list[str] = []
