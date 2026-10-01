"""FinOps 数据源装配（把 L5 仓储适配成 L3 只读服务需要的 ``source``）。

对齐《设计方案 v2》§6.5 与《实现要点清单》U10：FinOps 只读服务不直连其它模块的表，
所需数据经 ``source`` 注入（owner 边界的实现）。

★ 覆盖：``coverage_source``（计价覆盖率）、``detail_source``（trace 四维归因）。
``curve_source``（复用率曲线）依赖 ``skill_usage``，该表为 0 且无生产写入方，
故暂以空态诚实呈现，待数据链路补齐后再接线。
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_coverage_source", "build_detail_source"]


def build_coverage_source(repos: Any) -> Any:
    """计价覆盖率数据源：``async fn(since, until, biz_line_id) -> dict``。

    返回 ``priced_ratio / unpriced_calls / by_provider / total_calls``。
    """

    async def coverage(since: Any, until: Any, biz_line_id: int | None = None) -> dict[str, Any]:
        _ = (since, until)  # llm_call 无时间戳列，覆盖率为全历史口径
        stats = await repos.llm_call.coverage_stats(biz_line_id)
        total = stats["total"]
        return {
            "priced_ratio": round(stats["priced"] / total, 4) if total else 0.0,
            "unpriced_calls": stats["unpriced"],
            "by_provider": stats["by_provider"],
            "total_calls": total,
        }

    return coverage


def build_detail_source(repos: Any) -> Any:
    """trace 四维归因数据源：带 ``by_trace`` / ``range`` 方法的对象。

    供 ``CostAttributor`` 使用：``by_trace`` 是归因主路径，``range`` 服务于价格重算
    （llm_call 无时间戳列，``range`` 诚实退回全量，见 ``LlmCallRepo.range`` 注释）。
    """

    class _DetailSource:
        async def by_trace(self, trace_id: str) -> list[dict[str, Any]]:
            return list(await repos.llm_call.by_trace(trace_id))

        async def range(self, start: Any, end: Any) -> list[dict[str, Any]]:
            return list(await repos.llm_call.range(start, end))

    return _DetailSource()

