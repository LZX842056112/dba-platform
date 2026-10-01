"""模型路由能力（owner: capabilities.routing）。

对齐《设计文档 v2》§6.4 / §6.1.5 与《实现要点清单》U10、§4.4。

* ``router.ModelRouter``：**纯决策**（策略表 → 档位 / 降级链 / max_tokens）；
* ``gateway.ModelGateway``：实现内核 ``ModelRouter`` Protocol 的**完整链路**
  （预留 → 调用 → 计量 → 结算/释放）。
"""

from __future__ import annotations

from .gateway import DEFAULT_TASK_STRATEGY, ModelGateway
from .router import ModelRoute, ModelRouter, ModelSpec, RouteStrategy, build_ladder

__all__ = [
    "ModelRoute",
    "ModelRouter",
    "ModelSpec",
    "RouteStrategy",
    "ModelGateway",
    "DEFAULT_TASK_STRATEGY",
    "build_ladder",
]
