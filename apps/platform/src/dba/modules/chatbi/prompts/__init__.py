"""六角色 Prompt 模板加载器。

对齐《设计方案 v2》§4.2 / 附录 B 与《实现要点清单》§2.3（3.17）。

★ 文档只给了 ``prompts/`` 目录、未给全文（为独立调优课题）；本模块提供**可加载的
   基线模板**（``*.md``），并保证「模板缺失时回退到内置默认」——不让 prompt 文件成为
   运行期硬依赖。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

__all__ = ["load_prompt", "PROMPT_DIR"]

PROMPT_DIR = Path(__file__).parent


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """按名加载 ``prompts/<name>.md``；不存在时返回空串（调用方用内置默认）。"""
    path = PROMPT_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
