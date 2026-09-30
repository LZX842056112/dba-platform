#!/usr/bin/env python
"""``evals/run_eval.py`` —— 评测便捷入口（等价于 ``dba eval``）。

用法::

    uv run python evals/run_eval.py --suite guard --gate evals/thresholds.yaml
    uv run dba eval --suite all --gate evals/thresholds.yaml   # 二者等价

真正的实现位于 ``dba.evals.runner``（L1 装配层），此处只做入口转发，
以保证评测逻辑可被 CLI 与 CI 复用、不产生第二份真相。
"""

from __future__ import annotations

import sys

from dba.evals.runner import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
