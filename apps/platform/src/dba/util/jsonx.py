"""JSON 安全解析工具（全平台统一，消除散落的 ``try/except JSONDecodeError``）。

为什么需要（消除重复）
----------------------
项目里「解析一段可能非法的 JSON 文本」出现在 6 个以上位置：护栏的规则值、
SQL 生成 Agent 的模型输出、大屏组装器、审计查询、评测数据集等。此前每处都各写一遍
``try: json.loads(...) except json.JSONDecodeError: ...``，且**降级策略各不相同**
（有的返回 ``None``、有的返回 ``[]``、有的继续尝试截取花括号子串）——规则一旦要统一
（例如都改成「记录一条 warning」），就得逐个文件去改。

本模块把这两类高频需求收敛成两个函数：

* ``loads_or(value, default)``：能解析就返回结果，否则回**同一个默认值**；
* ``loads_object(text)``：从「可能夹带解释文字 / Markdown 代码块」的模型输出里
  提取 JSON 对象（**LLM 输出专用**，先整体解析，失败再退化为「首个 ``{...}`` 子串」）。

注意：本模块**不吞掉异常类型以外的错误**——只捕获 ``JSONDecodeError`` 与 ``TypeError``，
其余（如内存错误）照常向上抛出，避免把真正的故障伪装成「JSON 不合法」。
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["loads_or", "loads_object"]


def loads_or[T](text: Any, default: T) -> Any:
    """安全解析 JSON 文本；解析失败 / 输入为空 / 类型不符时返回 ``default``。

    输入：``text`` 可为 ``str``（待解析）、``list``/``dict``（已是结构化值，原样返回）
          或 ``None``（返回 ``default``）；``default`` 为降级返回值。
    输出：解析结果或 ``default``（返回类型不确定，故标 ``Any``）。
    注意：``text`` 已是 ``list``/``dict`` 时**不做深拷贝**，调用方不应就地修改返回值。
    """
    if text is None or text == "":
        return default
    if isinstance(text, (list, dict)):
        return text
    if not isinstance(text, str):
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def loads_object(text: Any) -> dict[str, Any] | None:
    """从模型输出文本中提取 JSON **对象**；失败返回 ``None``。

    两级降级：
      1. 先尝试整体 ``json.loads``（模型严格输出 JSON 时命中）；
      2. 失败则找**最外层的一对花括号** ``{...}`` 再解析（模型夹带解释文字 /
         Markdown ```json 代码块时命中）。

    输入：模型返回的原始文本（``str``）；非 ``str`` / 无法提取 → ``None``。
    输出：``dict`` 或 ``None``。
    注意：只提取**对象**（``{...}``）；若模型返回的是 JSON 数组，请用 ``loads_or``。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        inner = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    return inner if isinstance(inner, dict) else None
