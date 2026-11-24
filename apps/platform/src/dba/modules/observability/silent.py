"""「静默失败」的唯一 Python 判据。

为什么单独成文件：这条判据原先在三处各写一遍（rollup 的 SQL、overview 的 Python 内联、
``SilentFailureDetector`` 的静态方法），改口径时容易只改一处。现在：

* **Python 侧只保留本函数**；
* SQL 侧无法共享实现，写在 ``storage/mysql/repo.py::aggregate_daily``（``silent`` 变量），
  注释指向本文件，两处必须同步。

★ 口径边界（有意为之，勿「顺手统一」）
---------------------------------------
* KPI 与 ``metric_daily.silent_fail_count`` 只算「成功但零 token 零工具调用」；
* ``SilentFailureDetector`` 在此基础上另加「耗时低于同类任务 P5」一条，用于产出告警事件。
  所以 KPI 的静默失败数 ≤ 告警条数，属预期差异，不是 bug。
"""

from __future__ import annotations

__all__ = ["is_silent_failure"]


def is_silent_failure(*, status: str, tokens_in: int, tool_calls: int) -> bool:
    """成功、但既没有 token 也没有工具调用 —— 典型「跑了但什么都没做」。"""
    return status == "success" and tokens_in == 0 and tool_calls == 0
