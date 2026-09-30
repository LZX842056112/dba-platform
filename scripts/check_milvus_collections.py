"""Milvus collection 改名自检（DoD 3）。

断言：
  1. ``COLLECTION_SPECS`` 的 5 个名字都带 ``dba_`` 前缀；
  2. 全仓 **不再有裸的旧 collection 字面量**（grep 兜底）。

用法：``uv run python scripts/check_milvus_collections.py``（退出码 0 = 通过）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dba.storage.milvus.collections import COLLECTION_SPECS, DBA_COLLECTION_PREFIX

#: 加前缀前的旧名字（不得再以「带引号的字符串字面量」形式出现在源码里）
OLD_NAMES: tuple[str, ...] = (
    "sem_metric_vec",
    "skill_vec",
    "semantic_cache_vec",
    "finops_kb_vec",
    "obs_anomaly_vec",
)

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS: tuple[str, ...] = ("apps", "packages", "scripts", "migrations", "deploy", "tests")
_SKIP_PARTS = {".venv", "__pycache__", ".ruff_cache", ".mypy_cache"}


def check_prefix() -> list[str]:
    """断言 5 个 collection 均带 ``dba_`` 前缀。"""
    problems: list[str] = []
    names = [spec.name for spec in COLLECTION_SPECS]
    if len(names) != 5:
        problems.append(f"期望 5 个 collection，实际 {len(names)}：{names}")
    for name in names:
        if not name.startswith(DBA_COLLECTION_PREFIX):
            problems.append(f"collection 名缺 `{DBA_COLLECTION_PREFIX}` 前缀：{name}")
    return problems


def check_bare_literals() -> list[str]:
    """扫描源码：旧名不得作为**带引号的字符串字面量**出现。

    注意：新名 ``dba_sem_metric_vec`` 里含 ``sem_metric_vec`` 子串，故用「前后带引号」
    的正则 ``["']sem_metric_vec["']`` 匹配，避免误伤新名。
    """
    problems: list[str] = []
    alternatives = "|".join(re.escape(name) for name in OLD_NAMES)
    pattern = re.compile(rf"""["']({alternatives})["']""")
    self_path = Path(__file__).resolve()
    for base in SCAN_DIRS:
        for path in (ROOT / base).rglob("*.py"):
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            if path.resolve() == self_path:
                continue  # 本文件定义 OLD_NAMES 常量，属于检查器自身，跳过
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    rel = path.relative_to(ROOT)
                    problems.append(f"{rel}:{lineno}: 仍含裸旧字面量 → {line.strip()}")
    return problems


def main() -> int:
    problems = check_prefix() + check_bare_literals()
    if problems:
        print("[check-milvus-collections] ✗ 发现以下问题：")
        for problem in problems:
            print("  -", problem)
        return 1
    print(
        f"[check-milvus-collections] ✓ 5 个 collection 均带 `{DBA_COLLECTION_PREFIX}` "
        "前缀，且全仓无裸旧字面量"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
