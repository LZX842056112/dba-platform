"""worker 进程入口。

对齐《设计文档 v2》§6.1.4 与《实现要点清单》§1.6.6：
启动时 **必须** ① 装配依赖容器 ② ``bind_metering`` 并断言已绑定 ③ 用 ``AsyncIOScheduler``
注册任务；所有 job 入口由 ``with_run_context`` 统一包裹。
"""

from __future__ import annotations

import asyncio
import logging
import signal

from dba.config import get_settings
from dba.di import build_container
from dba_runtime import bind_metering, is_bound

from .scheduler import build_scheduler, register_jobs

logger = logging.getLogger("dba.worker")


async def amain() -> int:
    """启动 worker：装配 → 断言埋点 → 注册任务 → 常驻直到收到终止信号。"""
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    container = build_container(settings)

    metering = container.get("metering")
    cost = container.get("cost_normalizer")
    bind_metering(metering, cost=cost)
    if not is_bound():
        # ★ 启动断言：未绑定即拒绝启动（否则埋点静默 no-op）
        raise RuntimeError("worker 启动失败：bind_metering 未生效")

    scheduler = build_scheduler()
    jobs = register_jobs(scheduler, container)
    scheduler.start()
    logger.info("dba-worker 已启动 env=%s jobs=%d", settings.env, len(jobs))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows 部分环境不支持
            pass
    await stop.wait()

    scheduler.shutdown(wait=False)
    await container.aclose()
    logger.info("dba-worker 已优雅关闭")
    return 0


def main() -> int:
    """``dba-worker`` 控制台入口。"""
    return asyncio.run(amain())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
