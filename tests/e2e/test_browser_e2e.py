"""浏览器全流程 e2e 的 pytest 包装。

默认**跳过**——真机浏览器联调需要 Chrome + 前端 dev server + 虚拟机组件，
不应拖慢常规 CI。

启用方式::

    DBA_E2E_BROWSER=1 uv run pytest tests/e2e/test_browser_e2e.py -v

退出码语义（来自 ``run_browser_e2e.sh``）：
  0 = 全部断言通过；1 = 有断言失败；3 = 环境条件不足（本包装转为 skip）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "browser" / "run_browser_e2e.sh"


def test_browser_e2e_main_flow() -> None:
    """登录 → 提问 → 七步 → SSE → 出图（真实 Chrome）。"""
    if os.environ.get("DBA_E2E_BROWSER") != "1":
        pytest.skip("未启用：设 DBA_E2E_BROWSER=1 且确保 Chrome / 前端 / 虚拟机就绪")
    if not SCRIPT.exists():
        pytest.skip(f"找不到脚本：{SCRIPT}")
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode == 3:
        pytest.skip("环境条件不足（脚本返回 3：Chrome / 前端 / VM 未就绪）")
    assert proc.returncode == 0, f"浏览器 e2e 失败（退出码 {proc.returncode}）"
