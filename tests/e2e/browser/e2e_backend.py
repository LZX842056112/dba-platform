"""浏览器联调专用的后端启动器（验证工具，不属于产品代码）。

背景
----
2026-10-03 联调曾发现：``PATCH /finops/budgets/{budget_id}`` 的返回注解改为联合类型却
漏了 ``response_model=None``，导致 ``dba.main`` 在 **import 阶段**抛 ``FastAPIError``、
后端完全无法启动（详见 ``docs/联调验证报告-2026-10-03.md`` 问题 1）。该缺陷**已修复**。

因此兼容垫片**默认关闭**：默认启动的就是真实应用，同类「启动即失败」的回归会在 P1 阶段
如实暴露，不再被垫片掩盖。仅在需要做对照实验时显式设 ``DBA_E2E_SHIM=1``。

用法::

    python tests/e2e/browser/e2e_backend.py --host 127.0.0.1 --port 8000

环境变量
--------
* ``DBA_E2E_SHIM=1``            开启 FastAPI 兼容垫片（默认关闭，用于对照实验）；
* ``DBA_E2E_LLM_SCRIPT=<mode>`` 把演示替身的 SQL 输出替换为**脚本化 SQL**，
  用于确定性驱动「护栏拒绝 / 自愈重生成 / 终态失败」等分支（见下）。

脚本化模式（``DBA_E2E_LLM_SCRIPT``）
------------------------------------
``bad_table``           每次生成引用未登记物理表的 SQL（→ SQL_TABLE_NOT_ALLOWED，重试耗尽）
``bad_table_then_ok``   首次违规、其后生成合规 SQL（→ sql.retry.resolved 自愈成功）
``not_readonly``        生成 DELETE（→ SQL_NOT_READONLY）
``multi_stmt``          生成两条语句（→ SQL_MULTI_STATEMENT）
``dangerous_func``      生成含 SLEEP 的 SQL（→ SQL_DANGEROUS_FUNCTION）
``func_not_allowed``    生成白名单外函数（→ SQL_FUNCTION_NOT_ALLOWED）
``dry_run_fail``        引用不存在的列（→ SQL_DRY_RUN_FAILED，真实 EXPLAIN 拦截）
``province_ok``         合规 SQL **且查询列含 province_name**（用于验证行级权限真实注入）

运行期可切换：``POST /__e2e__/script {"mode": "<mode>"}``（仅本验证进程注册该路由，
用于一个实例依次驱动多种护栏分支，避免为每种模式各起一个后端）。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from fastapi import Request


def _install_response_model_shim() -> None:
    """让 FastAPI 容忍「联合返回注解但未声明 response_model=None」。"""
    import fastapi.routing as routing
    from fastapi.exceptions import FastAPIError

    original = routing.create_model_field

    def create_model_field_tolerant(*args: object, **kwargs: object) -> object:
        try:
            return original(*args, **kwargs)  # type: ignore[operator]
        except FastAPIError:
            # 无法从注解推断响应模型 → 视为未声明响应模型（= response_model=None）
            return None

    routing.create_model_field = create_model_field_tolerant  # type: ignore[assignment]


_SCRIPT_SQL: dict[str, str] = {
    "bad_table": "SELECT dt, SUM(gmv_ex_tax) AS gmv FROM dwd_order_detail GROUP BY dt LIMIT 500",
    "not_readonly": "DELETE FROM fact_sales WHERE 1 = 0",
    "multi_stmt": "SELECT 1; SELECT 2",
    "dangerous_func": "SELECT SLEEP(1) AS s FROM fact_sales LIMIT 1",
    "func_not_allowed": "SELECT UUID() AS u FROM fact_sales LIMIT 1",
    "dry_run_fail": "SELECT no_such_column_e2e FROM fact_sales LIMIT 10",
    "province_ok": (
        "SELECT province_name AS province, SUM(gmv_ex_tax) AS gmv FROM fact_sales "
        "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) "
        "GROUP BY province_name ORDER BY gmv DESC LIMIT 500"
    ),
}


def _install_llm_script(mode: str) -> None:
    """把 DemoLLMClient 的 SQL 任务输出替换为脚本化 SQL（仅验证进程内生效）。"""
    import json

    from dba.di import DemoLLMClient

    state: dict[str, Any] = {"mode": mode, "sql_calls": 0}

    class ScriptedDemoLLMClient(DemoLLMClient):  # type: ignore[misc]
        def _text(self, messages: list[dict[str, object]]) -> str:
            if self._task(messages) != "sql":
                return super()._text(messages)
            state["sql_calls"] = int(state["sql_calls"]) + 1
            current = state["mode"]
            if current == "bad_table_then_ok" and int(state["sql_calls"]) > 1:
                return super()._text(messages)
            sql = (
                _SCRIPT_SQL["bad_table"] if current == "bad_table_then_ok" else _SCRIPT_SQL[current]
            )
            return json.dumps({"sql": sql, "queries": [{"ref": "q1", "sql": sql}]})

    import dba.di as di

    di.DemoLLMClient = ScriptedDemoLLMClient  # type: ignore[misc]
    _LLM_SCRIPT_STATE["state"] = state


#: 运行期脚本模式（由 ``POST /__e2e__/script`` 改写）
_LLM_SCRIPT_STATE: dict[str, Any] = {}


def _register_script_control(app: Any) -> None:
    """注册仅用于联调的脚本模式切换端点。"""
    from fastapi.responses import JSONResponse

    async def set_script_mode(request: Request) -> Any:  # noqa: F821
        payload = await request.json()
        mode = str(payload.get("mode") or "")
        state = _LLM_SCRIPT_STATE.get("state")
        if state is None:
            return JSONResponse(status_code=409, content={"error": "scripted LLM not installed"})
        if mode not in _SCRIPT_SQL and mode != "bad_table_then_ok":
            return JSONResponse(status_code=400, content={"error": f"unknown mode: {mode}"})
        state["mode"] = mode
        state["sql_calls"] = 0  # ★ 每次切换重置「本轮生成次数」，保证重试分支可确定复现
        return {"ok": True, "mode": mode}

    app.add_api_route("/__e2e__/script", set_script_mode, methods=["POST"], include_in_schema=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="e2e_backend", description="联调验证用后端启动器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)

    if os.environ.get("DBA_E2E_SHIM", "0") == "1":
        _install_response_model_shim()

    script_mode = os.environ.get("DBA_E2E_LLM_SCRIPT", "").strip()
    if script_mode:
        _install_llm_script(script_mode)

    import uvicorn
    from dba.main import app

    if script_mode:
        _register_script_control(app)

    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
