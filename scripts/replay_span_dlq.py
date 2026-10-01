"""一次性运维脚本：把 ``var/dlq/telemetry.jsonl`` 中的 span 记录重投到 Mongo ``run_doc``。

背景
----
``di.py::_span`` 曾把含 ``_id`` 的 Mongo doc 直接 ``upsert``，撞上 Mongo 的
"update on the path '_id' would modify the immutable field '_id'"（错误码 66），
导致 221 行 span 全部进 DLQ。根因已在 ``di.py`` 修复（upsert 前 ``doc.pop("_id")``），
本脚本把这些历史 span 按 ``span_id`` 去重后合并回 ``run_doc``，恢复历史拓扑/链路。

行为
----
* 只处理 ``kind == "span"`` 的记录（``run`` 记录是**更早**的 ``datetime 不可序列化``
  入队失败的残留，根因已修，由脚本报告但不重投）；
* 按 ``trace_id`` 分组 → 与现有 ``run_doc`` 的 spans 按 ``span_id`` 去重合并（幂等，可重复跑）；
* 成功后把 DLQ 文件**归档**（重命名加时间戳），不直接删除，保留审计痕迹。

用法：``uv run python scripts/replay_span_dlq.py [--dry-run]``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dba.config import get_settings
from dba.storage.mongo.client import MongoStorage
from dba.storage.mongo.repo import RunDocRepo


def _load_records(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """读 DLQ，返回 ``(span 记录, 其它记录)``。非法 JSON 行跳过并告警。"""
    spans: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"  [skip] 非法 JSON 行：{exc}")
            continue
        (spans if obj.get("kind") == "span" else others).append(obj)
    return spans, others


async def _replay(spans: list[dict[str, Any]], *, dry_run: bool) -> tuple[int, int, int]:
    settings = get_settings()
    storage = MongoStorage(settings.mongo_dsn, settings.mongo_db)
    repo = RunDocRepo(storage)

    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obj in spans:
        payload = obj.get("payload") or {}
        tid = payload.get("trace_id")
        if tid:
            by_trace[str(tid)].append(payload)

    added = 0
    dup = 0
    try:
        for tid, payloads in by_trace.items():
            doc = await repo.get(tid) or {"trace_id": tid, "spans": []}
            existing = {
                str(s.get("span_id"))
                for s in (doc.get("spans") or [])
                if s.get("span_id")
            }
            spans_in_doc = list(doc.get("spans") or [])
            trace_added = 0
            for p in payloads:
                sid = p.get("span_id")
                if sid and str(sid) in existing:
                    dup += 1
                    continue
                spans_in_doc.append(p)
                if sid:
                    existing.add(str(sid))
                added += 1
                trace_added += 1
            if dry_run:
                print(f"  [dry-run] trace={tid} 新增 {trace_added} 条 span")
                continue
            doc["spans"] = spans_in_doc
            doc.pop("_id", None)
            await repo.upsert(doc)
            print(f"  [done] trace={tid} 新增 {trace_added} 条 span，累计 {len(spans_in_doc)}")
    finally:
        await storage.aclose()
    return added, dup, len(by_trace)


def _archive(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.replayed.{stamp}")
    path.replace(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="重投 span DLQ 到 Mongo run_doc")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作，不落库、不归档")
    args = parser.parse_args()

    settings = get_settings()
    dlq = Path(settings.dlq_path)
    if not dlq.exists():
        print(f"DLQ 文件不存在：{dlq}")
        return 0

    spans, others = _load_records(dlq)
    print(f"读取 {len(spans)} 条 span、{len(others)} 条其它记录（kind≠span）")
    if others:
        kinds: dict[str, int] = defaultdict(int)
        for o in others:
            kinds[str(o.get("kind"))] += 1
        note = "多为 datetime 入队失败的 run 残留，根因已修"
        print(f"  [note] 跳过非 span 记录：{dict(kinds)}（{note}）")

    added, dup, traces = asyncio.run(_replay(spans, dry_run=args.dry_run))
    print(f"结果：{traces} 个 trace，新增 {added} 条 span，去重跳过 {dup} 条")
    if args.dry_run:
        print("（--dry-run：未落库、未归档）")
        return 0
    archived = _archive(dlq)
    print(f"DLQ 已归档：{archived}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
