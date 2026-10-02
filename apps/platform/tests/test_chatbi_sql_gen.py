"""SQL 生成重试上下文回归用例。"""

from __future__ import annotations

from dba.modules.chatbi.agents.sql_gen import SqlGeneratorAgent


def test_sql_retry_retains_schema_and_accessible_table_context() -> None:
    payload = {
        "schema_prompt": (
            "指标：GMV 定义为实付金额。\n可访问表：fact_sales。"
            "字段：fact_sales.gmv_ex_tax、fact_sales.dt。原始问题：近 30 天 GMV 趋势。"
        ),
        "question": "近 30 天 GMV 趋势",
        "tables": ["fact_sales"],
        "sql": "SELECT total FROM fact_sales",
    }

    messages = SqlGeneratorAgent._build_retry_messages(
        payload,
        {"error_code": "SQL_COLUMN_NOT_FOUND", "error_message": "未知列 total"},
    )
    retry_prompt = messages[1]["content"]

    assert "可访问表：fact_sales" in retry_prompt
    assert "fact_sales.gmv_ex_tax" in retry_prompt
    assert "近 30 天 GMV 趋势" in retry_prompt
    assert "SELECT total FROM fact_sales" in retry_prompt
    assert "SQL_COLUMN_NOT_FOUND" in retry_prompt
    assert "未知列 total" in retry_prompt
