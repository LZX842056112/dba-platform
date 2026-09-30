"""技能能力（owner: capabilities.skills）。

对齐《设计文档 v2》§5.2.5 / §5.4 / §5.3 与《实现要点清单》P1-2。
"""

from __future__ import annotations

from .service import SkillMatch, SkillService

__all__ = ["SkillMatch", "SkillService"]
