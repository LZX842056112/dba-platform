"""统一异常体系（内核 L2）。

对齐《设计方案 v2》§6.9 与《实现要点清单》§6.3 统一错误码表（含 ★ v2 新增 50013/50014/50015）。

设计说明（与文档的一处有意收敛）
--------------------------------
文档 §6.9 里 ``DbaError.code`` 示例是数值码（``"50000"``）；而 §5.3 / U2 要求
``Pipeline._classify(exc)`` 返回**符号码**（``SQL_PERMISSION_DENIED`` / ``STEP_TIMEOUT`` …）。
为同时满足两者，本模块给 ``DbaError`` 两个字段：

* ``code``   —— 统一错误码表的**数值码**（对外 HTTP 响应体 ``code`` 字段）；
* ``symbol`` —— Pipeline 用的**符号码**（短路 / 回退语义判定）。

``api/errors.py`` 负责把符号码映射到数值码；内核之外（如 B4 的 ``SqlGuardError``）只要
设置 ``code``/``symbol`` 之一即可被 ``_classify`` 识别。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DbaError",
    "SqlGuardError",
    "BudgetExceededError",
    "StepTimeoutError",
    "StepVisitExceededError",
    "RunDeadlineExceededError",
    "PermissionDeniedError",
    "ScopeEmptyError",
    "StorageUnavailableError",
    "TelemetryNotBoundError",
    "TransientSqlError",
    "DryRunFailedError",
    "classify",
]

# 数值码 → 符号码（供 _classify 在只拿到数值码时反查）
NUMERIC_TO_SYMBOL: dict[str, str] = {
    "40001": "VALIDATION_ERROR",
    "40100": "UNAUTHENTICATED",
    "40300": "SQL_PERMISSION_DENIED",
    "40400": "NOT_FOUND",
    "40900": "CONFLICT",
    "42900": "RATE_LIMITED",
    "42901": "BUDGET_EXCEEDED",
    "50000": "INTERNAL_ERROR",
    "50010": "SQL_GUARD_BLOCKED",
    "50011": "SQL_EXEC_TRANSIENT",
    "50012": "RETRY_EXHAUSTED",
    "50013": "STEP_TIMEOUT",
    "50014": "STEP_VISIT_EXCEEDED",
    "50015": "SQL_SCOPE_EMPTY",
    "50301": "STORAGE_UNAVAILABLE",
}


class DbaError(Exception):
    """所有领域异常的基类。``code`` 为数值码，``symbol`` 为 Pipeline 用的符号码。"""

    code: str = "50000"
    symbol: str = "INTERNAL_ERROR"
    http_status: int = 500

    def __init__(
        self,
        message: str = "",
        code: str | None = None,
        detail: dict[str, Any] | None = None,
        symbol: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message or self.__class__.__name__
        if code is not None:
            self.code = code
        if symbol is not None:
            self.symbol = symbol
        self.detail: dict[str, Any] = detail or {}

    def to_dict(self) -> dict[str, Any]:
        """序列化为统一错误体（不含 trace_id，由 API 层补齐）。"""
        return {"code": self.code, "message": self.message, "detail": self.detail}


class SqlGuardError(DbaError):
    """SQL 护栏拒绝。

    ★ 本类定义在内核，B4 的 ``modules/chatbi/guard/base.py`` **必须复用**它，
    不得再定义一个同名类（否则 ``Pipeline._classify`` 无法统一识别）。
    具体拒绝原因通过 ``code``（数值）或 ``symbol``（符号）区分，例如：
    ``symbol="SQL_MULTI_STATEMENT"`` / ``symbol="SQL_NOT_READONLY"``。
    """

    code = "50010"
    symbol = "SQL_GUARD_BLOCKED"
    http_status = 422


class BudgetExceededError(DbaError):
    """预算硬上限 / 熔断阻断。"""

    code = "42901"
    symbol = "BUDGET_EXCEEDED"
    http_status = 429


class StepTimeoutError(DbaError):
    """步骤执行超时（★ v2 新增 50013）。"""

    code = "50013"
    symbol = "STEP_TIMEOUT"
    http_status = 408


class StepVisitExceededError(DbaError):
    """步骤访问次数超限（★ v2 新增 50014，用于 goto 成环等配置错误）。"""

    code = "50014"
    symbol = "STEP_VISIT_EXCEEDED"
    http_status = 422


class RunDeadlineExceededError(DbaError):
    """整次 Run 的墙钟预算耗尽。"""

    code = "50013"
    symbol = "RUN_DEADLINE_EXCEEDED"
    http_status = 408


class PermissionDeniedError(DbaError):
    """越权（行级权限不足）。策略性拒绝，**绝不重试**。"""

    code = "40300"
    symbol = "SQL_PERMISSION_DENIED"
    http_status = 403


class ScopeEmptyError(DbaError):
    """权限值域为空（★ v2 新增 50015）：该用户一行都不该看见，直接拒绝而非放行。"""

    code = "50015"
    symbol = "SQL_SCOPE_EMPTY"
    http_status = 403


class StorageUnavailableError(DbaError):
    """依赖存储不可用（响应体含组件名）。"""

    code = "50301"
    symbol = "STORAGE_UNAVAILABLE"
    http_status = 503


class TelemetryNotBoundError(DbaError):
    """埋点未装配（未调用 bind_metering）。"""

    code = "50000"
    symbol = "TELEMETRY_NOT_BOUND"
    http_status = 500


class TransientSqlError(DbaError):
    """SQL 执行的**临时性**失败（锁等待 / 连接中断），可触发 self-heal 回退。"""

    code = "50011"
    symbol = "SQL_EXEC_TRANSIENT"
    http_status = 422


class DryRunFailedError(DbaError):
    """SQL dry-run 失败（语法 / 列名等**技术性**失败），可触发 self-heal 回退。"""

    code = "50011"
    symbol = "SQL_DRY_RUN_FAILED"
    http_status = 422


# MySQL 可恢复错误的保守匹配（U11：锁等待 1205 / 连接中断 2006/2013 → 可回退）
_TRANSIENT_HINTS: tuple[str, ...] = (
    "lock wait timeout",
    "deadlock",
    "connection reset",
    "lost connection",
    "server has gone away",
    "1205",
    "2006",
    "2013",
)


def classify(exc: BaseException) -> tuple[str, str]:
    """把任意异常分类为 ``(symbol, message)``，供 Pipeline 决定短路 / 重试 / 回退。

    返回的 symbol ∈ {SQL_PERMISSION_DENIED, SQL_EXEC_TRANSIENT, SQL_DRY_RUN_FAILED,
    STEP_TIMEOUT, STEP_VISIT_EXCEEDED, RUN_DEADLINE_EXCEEDED, SQL_SCOPE_EMPTY,
    STEP_* , INTERNAL_ERROR}。

    兼容策略：优先读 ``exc.symbol``，其次读 ``exc.code``（若为非数值字符串则视为符号码，
    否则按数值码反查）。这样既可识别内核异常，也可识别 B4 只设置了符号 ``code`` 的异常。
    """
    if isinstance(exc, TimeoutError):  # asyncio.TimeoutError 在 3.11+ 即 TimeoutError
        return "STEP_TIMEOUT", str(exc) or "step timed out"

    if isinstance(exc, DbaError):
        return exc.symbol, exc.message

    symbol = getattr(exc, "symbol", None)
    if isinstance(symbol, str) and symbol:
        return symbol, str(exc)

    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        if not code.isdigit():
            return code, str(exc)
        if code in NUMERIC_TO_SYMBOL:
            return NUMERIC_TO_SYMBOL[code], str(exc)

    text = str(exc).lower()
    if any(hint in text for hint in _TRANSIENT_HINTS):
        return "SQL_EXEC_TRANSIENT", str(exc)

    return "INTERNAL_ERROR", str(exc)
