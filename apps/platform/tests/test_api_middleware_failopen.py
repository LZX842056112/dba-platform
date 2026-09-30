"""★ FIX-A / FIX-B 回归：观测 / 审计链路故障时，探针与业务请求都**不得** 500。

对应两处 P0：
* **FIX-B**：``TraceMiddleware`` 原先把 ``progress.write``（``run:progress``）放在 skip 判定
  之前且无 try/except → Redis 一挂，``/health`` / ``/ready`` / ``/metrics`` 全 500
  （违反 §7.6「/health 不检查依赖」、P1-9「Redis 故障 fail-open」，运维后果是 K8s
  liveness 探针失败反复杀 Pod）。
* **FIX-A**：容器把 ``audit_sink`` 设为 ``EventIndexRepo``，而 ``AuditMiddleware`` 调
  ``sink.write`` —— 该类此前无 ``write`` → 任意非 skip 请求 ``AttributeError``。

本文件用「必定抛 ``ConnectionError``」的假 progress / 假审计落点注入容器，验证 fail-open。
（后台 outbox 投递协程需要真实 DB，与本用例无关，故置 None 隔离。）
"""

from __future__ import annotations

from typing import Any

import httpx
from dba.config import Settings
from dba.main import create_app

_SKIP = ("/health", "/ready", "/metrics")
_NON_SKIP = "/api/v1/chat/sessions"


class _BoomProgress:
    """必定抛错的 ``run:progress`` 写入器（模拟 Redis 不可用）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def write(self, trace_id: str, envelope: dict[str, Any]) -> None:  # noqa: ARG002
        self.calls += 1
        raise ConnectionError("redis down")


class _BoomSink:
    """必定抛错的审计落点（模拟 ES 不可用）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def write(self, record: dict[str, Any]) -> None:  # noqa: ARG002
        self.calls += 1
        raise ConnectionError("es down")


async def test_observability_failure_never_breaks_probes_or_business() -> None:
    app = create_app(Settings(env="test"))
    progress, sink = _BoomProgress(), _BoomSink()
    app.state.container.set("progress", progress)
    app.state.container.set("audit_sink", sink)
    app.state.container.set("dispatcher", None)  # 后台 outbox 投递需真实 DB，与本用例无关

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with app.router.lifespan_context(app):
            # ① 探针路径：观测链路挂掉也绝不 500
            #    （/health→200；/ready→200 degraded 或 503；/metrics→200）
            for path in _SKIP:
                resp = await client.get(path)
                assert resp.status_code != 500, f"{path} 不应 500（progress 故障必须 fail-open）"

            # ② 探针不是业务 Run → 绝不产 run.started（progress 一次都没被调用）
            assert progress.calls == 0, "探针路径不应写 run.started"

            # ③ 业务路径：审计落点抛错也绝不 500（状态码由业务路由决定，非 500 即可）
            resp = await client.get(_NON_SKIP)
            assert resp.status_code != 500, "观测/审计故障不得把业务请求打成 500"

    # ④ 业务 Run 确实尝试过写 run.started + 写审计，只是失败被 fail-open 吞掉
    assert progress.calls == 1
    assert sink.calls == 1


def test_event_index_repo_implements_audit_sink_contract() -> None:
    # ★ FIX-A：EventIndexRepo 必须实现 AuditMiddleware 依赖的 write()
    from dba.storage.es.repo import EventIndexRepo

    assert callable(getattr(EventIndexRepo, "write", None))
