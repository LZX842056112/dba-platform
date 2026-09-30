"""L1 接入层（FastAPI）。

- ``deps.py``       鉴权 / 分页 / 幂等依赖
- ``errors.py``     统一错误体与异常处理器
- ``middleware.py`` Trace / RateLimit / Audit 中间件
- ``v1/``           REST / SSE / WebSocket 端点（B4~B6 填充）
"""

from __future__ import annotations
