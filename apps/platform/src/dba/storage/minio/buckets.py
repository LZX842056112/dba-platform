"""MinIO bucket 定义（§5.8）。

5 个 bucket 与其 owner：
  1. ``dba-dashboard``    owner=modules.chatbi          大屏 HTML/导出物
  2. ``dba-export``       owner=modules.chatbi          查询结果导出（CSV/XLSX）
  3. ``dba-skill-assets`` owner=capabilities.skills     技能附件/脚本
  4. ``dba-doc-chunks``   owner=capabilities.embedding  文档切块原文（供 RAG 溯源）
  5. ``dba-dlq``          owner=capabilities.telemetry  投递失败 DLQ 归档
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Bucket", "BUCKETS"]


@dataclass(frozen=True)
class Bucket:
    name: str
    owner: str
    description: str
    #: 是否公开读（大屏产物需要匿名读；其余禁止）
    public_read: bool = False


BUCKETS: tuple[Bucket, ...] = (
    Bucket("dba-dashboard", "modules.chatbi", "大屏 HTML/导出物", public_read=True),
    Bucket("dba-export", "modules.chatbi", "查询结果导出（CSV/XLSX）", public_read=False),
    Bucket("dba-skill-assets", "capabilities.skills", "技能附件/脚本", public_read=False),
    Bucket(
        "dba-doc-chunks", "capabilities.embedding", "文档切块原文（RAG 溯源）", public_read=False
    ),
    Bucket("dba-dlq", "capabilities.telemetry", "投递失败 DLQ 归档", public_read=False),
)
