"""评测资产加载（§12.2）。

资产位置固定在 workspace 根的 ``evals/``：

* ``golden_set.jsonl``  —— 分层题集（单表 / 多表 JOIN / 时间对比 / 权限边界）；
* ``bird_mini.jsonl``   —— BIRD 公开集抽样（外部可比性）。**本仓不伪造**：
  需要外部下载，缺失时返回空列表并在报告中标注「未提供」（详见 ``evals/README.md``）。

所有加载器对**缺字段/坏行**都给出可读错误，绝不静默跳过（避免题集悄悄缩水）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import GoldenCase

__all__ = ["load_golden", "load_jsonl", "golden_path", "bird_mini_path"]


def golden_path(root: Path) -> Path:
    return root / "evals" / "golden_set.jsonl"


def bird_mini_path(root: Path) -> Path:
    return root / "evals" / "bird_mini.jsonl"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL；空行忽略。文件不存在返回空列表。"""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} 非法 JSON：{exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"{path}:{lineno} 期望对象，实得 {type(obj).__name__}")
        rows.append(obj)
    return rows


def load_golden(root: Path) -> list[GoldenCase]:
    """加载 golden set（含字段校验）。"""
    cases: list[GoldenCase] = []
    for row in load_jsonl(golden_path(root)):
        expect = row.get("expect") or {}
        if not isinstance(expect, dict):
            raise ValueError(f"golden {row.get('id')}: expect 必须是对象")
        denied = bool(expect.get("denied"))
        # 权限拒绝题不执行 SQL，故不要求 gold_sql；其余必须给金标 SQL
        required = ["id", "question", "category"] + ([] if denied else ["gold_sql"])
        missing = [k for k in required if not row.get(k)]
        if missing:
            raise ValueError(f"golden 记录缺字段 {missing}: {row.get('id') or row}")
        cases.append(
            GoldenCase(
                id=str(row["id"]),
                question=str(row["question"]),
                category=str(row["category"]),
                role=str(row.get("role") or "default"),
                metric_codes=tuple(str(x) for x in (row.get("metric_codes") or [])),
                gold_sql=str(row["gold_sql"]),
                candidate_sql=str(row.get("candidate_sql") or row["gold_sql"]),
                candidate_metric_codes=tuple(
                    str(x)
                    for x in (row.get("candidate_metric_codes") or row.get("metric_codes") or [])
                ),
                expect=dict(expect),
                notes=str(row.get("notes") or ""),
            )
        )
    return cases
