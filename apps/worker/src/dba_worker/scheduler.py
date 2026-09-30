"""worker 调度器。

对齐《设计文档 v2》§4.5 / §6.1.4 与《实现要点清单》§1.6.6、§5.5。

★ 两处 v1 硬伤修复：
  1) 所有 job 入口必须 ``set_current(RunContext(module="system", trace_id=...))`` 包裹，
     否则 worker 里调 ``ctx()`` 会 ``RuntimeError`` 或读到上一个任务的残留上下文（P1-6）。
  2) 启动前断言 ``bind_metering`` 已绑定，否则埋点静默 no-op（最难发现的故障形态）。

★ 类型：用 ``AsyncIOScheduler``（U25）。
★ P2-8 多实例互斥：默认用 **Redis 锁**（``job:lock:{id}`` / SET NX EX）保证同一任务在同一时刻
  只被一个副本执行；无 Redis 时降级为「假设单实例」（启动日志如实告警），绝不静默假设安全。
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dba_runtime import (
    RunContext,
    new_span_id,
    new_trace_id,
    reset_current,
    set_current,
)

from .jobs import (
    close_stale_runs,
    maintain_partitions,
    run_anomaly_scan,
    run_archive,
    run_daily_report,
    run_reconcile,
    run_rollup,
    run_semcache_gc,
)

logger = logging.getLogger("dba.worker")

JobFn = Callable[..., Awaitable[Any]]

#: job_id → (任务函数, 触发参数)。触发参数直接透传给 ``scheduler.add_job``。
#: 时刻均为 UTC（与全平台 UTC 口径一致）。
JOB_SPECS: dict[str, tuple[JobFn, dict[str, Any]]] = {
    "rollup": (run_rollup, {"trigger": "cron", "hour": 2, "minute": 0}),
    "partition": (maintain_partitions, {"trigger": "cron", "day": 1, "hour": 1, "minute": 0}),
    "anomaly_scan": (run_anomaly_scan, {"trigger": "interval", "minutes": 15}),
    "report": (run_daily_report, {"trigger": "cron", "hour": 7, "minute": 0}),
    "reconcile": (run_reconcile, {"trigger": "cron", "hour": 3, "minute": 0}),
    "stale_run": (close_stale_runs, {"trigger": "interval", "minutes": 10}),
    "semcache_gc": (run_semcache_gc, {"trigger": "interval", "minutes": 30}),
    "archive": (run_archive, {"trigger": "cron", "day_of_week": "sun", "hour": 4, "minute": 0}),
}

#: 各任务的分布式锁 TTL（秒）——远大于单次运行时长，避免锁提前过期导致并发
_LOCK_TTL_S = 600


def with_run_context(fn: JobFn) -> JobFn:
    """把 job 包进一个 ``module="system"`` 的 RunContext。

    ★ v1 的 worker job 直接调 ``ctx()`` 会报错（未设置）或读到残留上下文；
      本装饰器为每个 job 调用建立干净的上下文，并在结束时还原。
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        run_ctx = RunContext(trace_id=new_trace_id(), span_id=new_span_id(), module="system")
        token = set_current(run_ctx)
        try:
            return await fn(*args, **kwargs)
        finally:
            reset_current(token)

    return wrapper


def _singleton_guard(fn: JobFn, container: Any, job_id: str) -> JobFn:
    """P2-8：用 Redis 锁保证同一 job 在集群内同一时刻只执行一次。

    无 Redis 时**不静默假设安全**——记录告警并直接执行（单实例部署下正确）。
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        redis = container.get("redis")
        if redis is None:
            logger.warning("job=%s 无 Redis：跳过分布式锁（仅单实例安全）", job_id)
            return await fn(*args, **kwargs)
        key = f"job:lock:{job_id}"
        owner = new_trace_id()
        acquired = False
        try:
            acquired = bool(await redis.client.set(key, owner, nx=True, ex=_LOCK_TTL_S))
        except Exception as exc:  # noqa: BLE001 - 锁不可用时不阻塞（降级单实例语义）
            logger.warning("job=%s 获取 Redis 锁失败（降级执行）：%s", job_id, exc)
            return await fn(*args, **kwargs)
        if not acquired:
            logger.info("job=%s 已在其他副本运行，跳过本次", job_id)
            return {"ok": True, "skipped": "locked"}
        try:
            return await fn(*args, **kwargs)
        finally:
            try:
                # 只释放「自己持有」的锁（比对 owner），避免把别人的锁删掉引发并发
                raw = await redis.client.get(key)
                value = raw.decode() if isinstance(raw, bytes | bytearray) else raw
                if value == owner:
                    await redis.client.delete(key)
            except Exception:  # noqa: BLE001 - 释放失败让 TTL 兜底
                pass

    return wrapper


def _build_job(fn: JobFn, container: Any, job_id: str) -> JobFn:
    """把任务函数绑定容器 → 加 Redis 锁 → 加 RunContext。"""
    bound: JobFn = functools.partial(fn, container)
    guarded = _singleton_guard(bound, container, job_id)
    return with_run_context(guarded)


def build_scheduler() -> AsyncIOScheduler:
    """创建 AsyncIOScheduler（UTC）。"""
    return AsyncIOScheduler(timezone="UTC")


def register_jobs(scheduler: AsyncIOScheduler, container: Any) -> list[str]:
    """注册 8 个周期任务；返回已注册的 job id 列表。"""
    job_ids: list[str] = []
    for job_id, (fn, trigger_args) in JOB_SPECS.items():
        scheduler.add_job(
            _build_job(fn, container, job_id),
            id=job_id,
            name=f"dba.{job_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            **trigger_args,
        )
        job_ids.append(job_id)
    logger.info("worker 已注册 %d 个周期任务：%s", len(job_ids), ", ".join(job_ids))
    return job_ids
