"""``dba`` 命令行。

对齐《设计文档 v2》§4.7 / §12.4 与《实现要点清单》§1.2.6.4。

子命令：``migrate`` / ``bootstrap-storage`` / ``seed``（B0 交付骨架与 ``--help``）；
``reindex`` / ``eval`` 于后续批次接线。
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from .config import get_settings

PROG = "dba"


def _redact(dsn: str) -> str:
    """脱敏 DSN（去掉 ``user:pass@`` 部分），避免日志泄漏口令。"""
    if "@" in dsn and "://" in dsn:
        scheme, rest = dsn.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return dsn


def _project_root() -> Path:
    """定位 workspace 根（``apps/platform/src/dba/cli.py`` → 上溯 4 层）。"""
    return Path(__file__).resolve().parents[4]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="dba-platform 命令行：数据库迁移 / 存储初始化 / 造数 / 评测",
    )
    parser.add_argument("--version", action="version", version="dba 0.1.0")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    m = sub.add_parser("migrate", help="Alembic 升级 MySQL（幂等）")
    m.add_argument("--revision", default="head", help="目标版本（默认 head）")
    m.add_argument("--dry-run", action="store_true", help="仅打印将执行的迁移，不落库")

    b = sub.add_parser("bootstrap-storage", help="建 Milvus collection / ES index / MinIO bucket")
    b.add_argument("--only", default=None, help="仅初始化指定组件（milvus/es/minio）")

    s = sub.add_parser("seed", help="灌入口径 / 技能模板 / 价格表 / 演示数据")
    s.add_argument("--demo", action="store_true", help="含演示业务数据")

    r = sub.add_parser("reindex", help="重建检索索引（Milvus / ES）")
    r.add_argument("--target", default="all", help="milvus | es | all")

    e = sub.add_parser("eval", help="运行评测套件")
    e.add_argument(
        "--suite",
        default="all",
        choices=["all", "guard", "golden", "bird-mini"],
        help="all | guard | golden | bird-mini",
    )
    e.add_argument("--gate", default=None, help="阈值文件路径（退出码即结论：0/1/2）")
    e.add_argument("--out", default=None, help="结果 JSON 落盘路径")
    e.add_argument("--root", default=None, help="workspace 根（默认自动定位）")

    return parser


def cmd_migrate(args: argparse.Namespace) -> int:
    """执行 Alembic 迁移（幂等）。★ B2：接 ``storage.mysql.migrations_runner``。"""
    settings = get_settings()
    migrations_dir = _project_root() / "migrations"
    if not migrations_dir.exists():
        print(f"[migrate] 未找到迁移目录：{migrations_dir}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"[migrate] dry-run：将升级 {_redact(settings.mysql_dsn)} → {args.revision}")
        return 0
    try:
        from .storage.mysql.migrations_runner import current_revision, run_upgrade  # noqa: PLC0415
    except ImportError:
        print(
            "[migrate] 未安装 alembic。请执行：uv sync --all-packages --extra prod", file=sys.stderr
        )
        return 2
    try:
        before = current_revision(settings.mysql_dsn, migrations_dir=migrations_dir)
        run_upgrade(settings.mysql_dsn, args.revision, migrations_dir=migrations_dir)
        after = current_revision(settings.mysql_dsn, migrations_dir=migrations_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[migrate] 迁移失败：{exc}", file=sys.stderr)
        return 1
    print(f"[migrate] 完成：{before} → {after}")
    return 0


def cmd_bootstrap_storage(args: argparse.Namespace) -> int:
    """初始化非关系型存储（Milvus / ES / MinIO / MongoDB）。★ B2 落地。"""
    target = args.only or "all"
    settings = get_settings()
    failures: list[str] = []

    async def _run() -> None:
        if target in ("all", "mongo"):
            try:
                from .storage.mongo.client import MongoStorage  # noqa: PLC0415

                mongo_storage = MongoStorage(settings.mongo_dsn, settings.mongo_db)
                await mongo_storage.ensure_indexes()
                await mongo_storage.aclose()
                print("[bootstrap-storage] MongoDB：8 集合 + 索引就绪")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"mongo: {exc}")
        if target in ("all", "milvus"):
            try:
                from .storage.milvus.client import MilvusStorage  # noqa: PLC0415

                milvus_storage = MilvusStorage(
                    settings.milvus_host, settings.milvus_port, dim=settings.embedding_dim
                )
                await milvus_storage.ensure_collections()
                await milvus_storage.aclose()
                print("[bootstrap-storage] Milvus：5 collection 就绪")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"milvus: {exc}")
        if target in ("all", "es"):
            try:
                from .storage.es.client import EsStorage  # noqa: PLC0415

                es_storage = EsStorage(settings.es_url, prefix=settings.es_index_prefix)
                await es_storage.ensure_indices()
                await es_storage.aclose()
                print("[bootstrap-storage] ES：3 索引模板 + ILM 就绪")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"es: {exc}")
        if target in ("all", "minio"):
            try:
                from .storage.minio.client import MinioStorage  # noqa: PLC0415

                minio_storage = MinioStorage(
                    settings.minio_endpoint,
                    settings.minio_access_key,
                    settings.minio_secret_key,
                    secure=settings.minio_secure,
                )
                await minio_storage.ensure_buckets()
                await minio_storage.aclose()
                print("[bootstrap-storage] MinIO：5 bucket 就绪")
            except Exception as exc:  # noqa: BLE001
                failures.append(f"minio: {exc}")

    asyncio.run(_run())
    if failures:
        for item in failures:
            print(f"[bootstrap-storage] 失败：{item}", file=sys.stderr)
        return 1
    print(f"[bootstrap-storage] 完成（target={target}）")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """造数：口径 / 技能模板 / 价格表 / 演示数据。★ B2 落地（``scripts/seed.py``）。"""
    settings = get_settings()
    script = _project_root() / "scripts" / "seed.py"
    if not script.exists():
        print(f"[seed] 未找到造数脚本：{script}", file=sys.stderr)
        return 2
    cmd = [sys.executable, str(script), "--dsn", settings.mysql_dsn]
    if args.demo:
        cmd.append("--demo")
    try:
        completed = subprocess.run(cmd, check=False)  # noqa: S603
    except OSError as exc:
        print(f"[seed] 执行失败：{exc}", file=sys.stderr)
        return 1
    return int(completed.returncode)


def cmd_reindex(args: argparse.Namespace) -> int:
    """重建检索索引（Milvus / ES）。★ B3：由 worker 的 reindex job 落地。"""
    print(
        f"[reindex] target={args.target}：由 worker ``reindex`` job 落地"
        "（Milvus 重灌向量 / ES 重建索引）。当前批次仅登记入口，执行在 B4/B5 接线。"
    )
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    """运行评测套件（★ B6 落地：接 ``dba.evals.runner``）。

    退出码语义（§12.4 / U23）：``0`` 全部达标 / ``1`` 有套件未达标 / ``2`` 执行错误。
    ``--suite guard`` 离线秒级；``--suite golden`` 需真实 MySQL（读 ``DBA_TEST_MYSQL_DSN``）。
    """
    from .evals.runner import main as eval_main  # noqa: PLC0415 - 延迟导入，避免拖慢 CLI 冷启

    argv: list[str] = ["--suite", str(args.suite)]
    if args.gate:
        argv += ["--gate", str(args.gate)]
    if getattr(args, "out", None):
        argv += ["--out", str(args.out)]
    if getattr(args, "root", None):
        argv += ["--root", str(args.root)]
    return eval_main(argv)


_DISPATCH = {
    "migrate": cmd_migrate,
    "bootstrap-storage": cmd_bootstrap_storage,
    "seed": cmd_seed,
    "reindex": cmd_reindex,
    "eval": cmd_eval,
}


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handler = _DISPATCH[args.command]
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
