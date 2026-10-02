"""① 只读护栏：单语句 + 只读类型 + 危险构造/函数黑名单 + 表/函数白名单。

对齐《设计方案 v2》§6.3.2 ① 与《实现要点清单》§5.8。

★ v2 相比 v1 加严三点（照抄 v1 会怎样错）
-----------------------------------------
1) 用 ``sqlglot.parse()`` 而不是 ``parse_one()``，**先要求「恰好一条语句」**——
   多语句（``SELECT 1; DROP TABLE x``）一律拒绝，不依赖解析器对分号的宽容度。
2) 表与函数改为**白名单**：v1「拒绝写操作」是黑名单思路，而 ``information_schema`` /
   ``mysql.*`` / ``performance_schema`` 的**读取同样是越权面**。
3) 补齐 v1 漏掉的构造：``INTO OUTFILE/DUMPFILE``、``FOR UPDATE``、
   ``LOCK IN SHARE MODE``、会话变量赋值 ``@a := ...``、``/*! ... */`` 注释注入。

★ 危险构造用**正则先于解析**判定：像 ``INTO OUTFILE`` / ``/*!`` 这类构造 sqlglot
   直接抛 ParseError/TokenError，若只靠解析它们会变成 ``SQL_PARSE_ERROR``（可回退），
   而它们本质是**高危构造**（必须硬拒且不可回退），故必须先在文本层判定。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import sqlglot
from dba_runtime.context import RunContext
from sqlglot import exp

from .ast_utils import physical_tables
from .base import GuardResult, sql_guard_error

__all__ = ["ReadonlyGuard", "DEFAULT_FUNCTION_WHITELIST"]

logger = logging.getLogger("dba.modules.chatbi.guard.readonly")

#: 只读语句类型（SELECT / UNION / EXCEPT / INTERSECT；WITH 在 sqlglot 30 解析为 Select）
_READONLY_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Except,
    exp.Intersect,
)

#: 一律禁止的系统库（只读账号也不该读信息模式/元数据）
_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})

#: 高危构造（文本判定；命中即 ``SQL_DANGEROUS_CONSTRUCT``）
_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\binto\s+(outfile|dumpfile)\b", re.IGNORECASE), "INTO OUTFILE/DUMPFILE"),
    (re.compile(r"\bfor\s+update\b", re.IGNORECASE), "FOR UPDATE"),
    (re.compile(r"\block\s+in\s+share\s+mode\b", re.IGNORECASE), "LOCK IN SHARE MODE"),
    (re.compile(r"@[A-Za-z0-9_$]+\s*:="), "会话变量赋值 @var :="),
    (re.compile(r"/\*!"), "注释注入 /*! ... */"),
)

#: 高危函数（文本判定；命中即 ``SQL_DANGEROUS_FUNCTION``）
_DANGEROUS_FUNCTIONS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"\b{name}\s*\(", re.IGNORECASE)
    for name in (
        "sleep",
        "benchmark",
        "load_file",
        "get_lock",
        "release_lock",
        "sys_exec",
        "sys_eval",
        "sys_reload_conf",
    )
)

#: ★ sqlglot 把「**谓词构造**」也归入 ``exp.Func``——它们**不是函数**，必须整体从
#:   函数白名单判定中排除。同一个坑已踩过两次，故按「谓词构造」统一处理（而非逐个补丁）：
#:
#:   * 布尔连接词：``And`` / ``Or`` / ``Xor``（``Binary`` 子类）、``Not``（``Unary`` 子类）
#:     —— ``sql_name()`` 分别为 ``'AND'`` / ``'OR'`` / ``'XOR'`` / ``'NOT'``；
#:     不排除 → 任何多条件 WHERE（``a = 1 AND b = 2``）都被误判「白名单外函数：['AND']」，
#:     而**行级注入的结果几乎总含 AND**，等于行级权限一开就全量报错。
#:   * 子查询存在谓词：``Exists``（MRO: ``Exists → Func → SubqueryPredicate → Predicate``）
#:     —— ``sql_name()`` 为 ``'EXISTS'``；``NOT EXISTS`` 的内层同样是该节点（``Not`` 本身
#:     不是 ``Func``），故排除 ``Exists`` 即同时覆盖 ``EXISTS`` 与 ``NOT EXISTS``。
#:
#: ★ 判据一致性：``In`` / ``Between`` / ``Like`` **不是** ``Func`` 子类，本就不参与该判定；
#:   而 ``IN (子查询)`` 已放行、其安全性由 row-scope 注入 + 覆盖性断言保证。``EXISTS (子查询)``
#:   与 ``IN (子查询)`` 是**同一安全剖面**（都是子查询谓词），一个放行一个拒绝本身即不一致。
#:   函数白名单的职责是拦「危险构造」（文件读写 / 系统函数 / 会话变量），
#:   **不是拦合法读语法**——受保护表的约束必须由 row-scope 防线承担（见 ``row_scope.py``）。
#: ★ 注意：``In`` / ``Between`` / ``Like`` 不是 ``Func``，无需（也无法）列在此处。
_PREDICATE_CONSTRUCTS_NOT_FUNCTIONS: tuple[type[exp.Expression], ...] = (
    exp.And,
    exp.Or,
    exp.Xor,
    exp.Not,
    exp.Exists,
)

#: 默认函数白名单（聚合 / 窗口 / 日期 / 数学 / 字符串 / 条件 / 类型转换）
DEFAULT_FUNCTION_WHITELIST: frozenset[str] = frozenset(
    name.upper()
    for name in (
        # 聚合
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "GROUP_CONCAT",
        "STD",
        "STDDEV",
        "STDDEV_POP",
        "STDDEV_SAMP",
        "VARIANCE",
        "VAR_POP",
        "VAR_SAMP",
        "BIT_AND",
        "BIT_OR",
        "BIT_XOR",
        "ANY_VALUE",
        # 窗口
        "ROW_NUMBER",
        "RANK",
        "DENSE_RANK",
        "PERCENT_RANK",
        "CUME_DIST",
        "NTILE",
        "LAG",
        "LEAD",
        "FIRST_VALUE",
        "LAST_VALUE",
        "NTH_VALUE",
        # 数学
        "ABS",
        "ROUND",
        "CEIL",
        "CEILING",
        "FLOOR",
        "MOD",
        "POW",
        "POWER",
        "SQRT",
        "EXP",
        "LN",
        "LOG",
        "LOG2",
        "LOG10",
        "SIGN",
        "TRUNCATE",
        "RAND",
        "GREATEST",
        "LEAST",
        "DIV",
        # 日期时间
        "NOW",
        "CURDATE",
        "CURTIME",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "UNIX_TIMESTAMP",
        "FROM_UNIXTIME",
        "DATE",
        "TIME",
        "YEAR",
        "MONTH",
        "DAY",
        "DAYOFMONTH",
        "DAYOFWEEK",
        "DAYOFYEAR",
        "HOUR",
        "MINUTE",
        "SECOND",
        "WEEK",
        "QUARTER",
        "DATE_FORMAT",
        "STR_TO_DATE",
        "DATE_ADD",
        "DATE_SUB",
        "DATEDIFF",
        "TIMESTAMPDIFF",
        "DATE_TRUNC",
        "LAST_DAY",
        "EXTRACT",
        "TO_DAYS",
        # 字符串
        "CONCAT",
        "CONCAT_WS",
        "SUBSTRING",
        "SUBSTR",
        "LEFT",
        "RIGHT",
        "LENGTH",
        "CHAR_LENGTH",
        "LOWER",
        "UPPER",
        "TRIM",
        "LTRIM",
        "RTRIM",
        "REPLACE",
        "REVERSE",
        "LPAD",
        "RPAD",
        "LOCATE",
        "INSTR",
        "REGEXP_REPLACE",
        "REGEXP_SUBSTR",
        "REPEAT",
        "SPLIT_PART",
        "CONVERT_TZ",
        # 条件 / 空值 / 类型
        "COALESCE",
        "IFNULL",
        "NULLIF",
        "IF",
        "CAST",
        "CONVERT",
        "ROUND",
    )
)


class ReadonlyGuard:
    """只读护栏（第一道，白名单思路）。"""

    name = "readonly"

    def __init__(
        self,
        *,
        allowed_tables: set[str] | None = None,
        extra_functions: set[str] | None = None,
        table_loader: Callable[[], Awaitable[set[str]]] | None = None,
    ) -> None:
        #: 显式表白名单（``None`` 表示改用 ``table_loader`` 或「不限制」）
        self._allowed_tables = allowed_tables
        self._table_loader = table_loader
        self._loaded_tables: set[str] | None = None
        self._warned_unrestricted = False
        self._functions = DEFAULT_FUNCTION_WHITELIST | {
            f.upper() for f in (extra_functions or set())
        }

    async def _allowed(self) -> set[str] | None:
        """解析当前生效的表白名单；``None`` 表示不限制（仅开发环境）。"""
        if self._allowed_tables is not None:
            return self._allowed_tables
        if self._table_loader is not None:
            if self._loaded_tables is None:
                self._loaded_tables = {t.lower() for t in await self._table_loader()}
            if not self._loaded_tables:
                # 语义层未登记任何物理表 → 无法据此建白名单（配置缺口，需告警）。
                logger.warning("语义层未登记任何物理表，表级白名单回退为「不限制」（请尽快补登记）")
                return None
            return self._loaded_tables
        return None

    async def check(self, sql: str, ctx: RunContext, scope: object | None) -> GuardResult:
        """只读护栏判定（① 道）。

        校验顺序（**顺序不可调**，每步都有原因）：
          0. 危险构造正则（先于解析：``OUTFILE`` / ``/*!`` 会让解析器抛错，
             若放后判定会退化成可回退的 ``SQL_PARSE_ERROR``）；
          1. 危险函数黑名单（``sleep`` / ``load_file`` 等）；
          2. 解析且**恰好一条语句**（拦截 ``SELECT 1; DROP TABLE x``）；
          3. 语句类型必须是只读；
          4. AST 层锁读构造（``FOR UPDATE`` / ``LOCK IN SHARE MODE``）；
          5. 系统库 + 表级白名单；
          6. 函数白名单。

        输入：待校验 SQL（已解析前的原文）、上下文、权限子域（本护栏不消费）。
        输出：``GuardResult(ok=True)``（只读护栏**不改写 SQL**，故无 ``rewritten_sql``）。
        注意：任一判定失败即抛 ``SqlGuardError``（由链写入 deny 审计并触发 Pipeline 回退）。
        """
        _ = (ctx, scope)
        raw = sql or ""

        # 0) 危险构造（先于解析：否则 OUTFILE / /*! 会退化成可回退的 PARSE_ERROR）
        for pattern, label in _DANGEROUS_PATTERNS:
            if pattern.search(raw):
                raise sql_guard_error(
                    f"检测到高危构造：{label}", "SQL_DANGEROUS_CONSTRUCT", stage=self.name
                )
        # 1) 危险函数
        for pattern in _DANGEROUS_FUNCTIONS:
            if pattern.search(raw):
                raise sql_guard_error(
                    f"检测到高危函数：{pattern.pattern}", "SQL_DANGEROUS_FUNCTION", stage=self.name
                )

        # 2) 解析 + ★ 恰好一条语句
        try:
            statements = sqlglot.parse(raw, dialect="mysql")
        except Exception as exc:  # noqa: BLE001
            raise sql_guard_error(
                f"语法解析失败：{exc}", "SQL_PARSE_ERROR", stage=self.name
            ) from exc
        # sqlglot 的静态类型为 ``list[Expr | None]``；过滤后 mypy 无法自动收窄，
        # 这里显式标注 ``Any``——后续全部走 sqlglot 的运行时 API（find_all/args 等）。
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            raise sql_guard_error("仅允许单条只读语句", "SQL_MULTI_STATEMENT", stage=self.name)

        tree: Any = statements[0]

        # 3) 只读类型
        if not _is_readonly(tree):
            raise sql_guard_error("仅允许只读查询", "SQL_NOT_READONLY", stage=self.name)

        # 4) AST 层高危构造（FOR UPDATE / LOCK IN SHARE MODE）
        if any(True for _ in tree.find_all(exp.Lock)):
            raise sql_guard_error(
                "检测到高危构造：锁读（FOR UPDATE / LOCK IN SHARE MODE）",
                "SQL_DANGEROUS_CONSTRUCT",
                stage=self.name,
            )

        # 5) 系统库 + 表白名单
        for tbl in tree.find_all(exp.Table):
            db = (tbl.db or "").lower()
            if db in _SYSTEM_SCHEMAS:
                raise sql_guard_error(
                    f"禁止访问系统库：{db}", "SQL_TABLE_NOT_ALLOWED", stage=self.name
                )
        allowed = await self._allowed()
        referenced = physical_tables(tree)
        if allowed is not None:
            allowed_lower = {t.lower() for t in allowed}
            illegal = {t for t in referenced if t.lower() not in allowed_lower}
            if illegal:
                raise sql_guard_error(
                    f"引用了未登记的物理表：{sorted(illegal)}",
                    "SQL_TABLE_NOT_ALLOWED",
                    stage=self.name,
                )
        elif referenced and not self._warned_unrestricted:
            self._warned_unrestricted = True
            logger.warning(
                "未配置表级白名单（chatbi_allowed_tables 为空）——开发模式放行，生产必须配置"
            )

        # 6) 函数白名单
        illegal_funcs = {n for n in _function_names(tree) if n.upper() not in self._functions}
        if illegal_funcs:
            raise sql_guard_error(
                f"使用了白名单外的函数：{sorted(illegal_funcs)}",
                "SQL_FUNCTION_NOT_ALLOWED",
                stage=self.name,
            )

        return GuardResult(ok=True)


def _is_readonly(tree: Any) -> bool:
    """是否为只读语句类型（SELECT / UNION / EXCEPT / INTERSECT）。"""
    return isinstance(tree, _READONLY_TYPES)


def _function_names(tree: Any) -> set[str]:
    """SQL 中用到的函数名集合（含匿名函数）。

    ★ 必须**排除优化器提示**（``/*+ ... */``，如 ④ 号护栏注入的 ``MAX_EXECUTION_TIME``）：
    sqlglot 会把提示内容解析成 ``exp.Anonymous`` 函数并挂在 ``exp.Hint`` 节点下。
    若不排除，护栏链**第二次**执行（如 ``sql_guard`` 通过后 ``sql_exec`` 再次校验同一
    条已改写 SQL）会把提示误判成「白名单外函数」而误杀——护栏链因此**不幂等**。

    ★ 必须**排除谓词构造**（``AND`` / ``OR`` / ``XOR`` / ``NOT`` / ``EXISTS``）：sqlglot 把
    ``exp.And`` 等（``exp.Binary``/``exp.Unary`` 子类）与 ``exp.Exists``（``exp.Func`` 子类）
    都归入 ``exp.Func``，其 ``sql_name()`` 分别是 ``'AND'`` / ``'OR'`` / ``'EXISTS'`` 等。
    若不排除，**任何带多条件 WHERE 的 SQL**（``WHERE a = 1 AND b = 2``）或**任何子查询存在
    谓词**（``WHERE EXISTS (SELECT ...)``）都会被误判成「使用了白名单外的函数」而拒绝——
    这是行级注入后第二次校验必然踩中的坑（注入结果几乎总含 ``AND``）。
    它们是**谓词/运算符**，不是函数，不应参与函数白名单判定。
    """
    names: set[str] = set()
    for node in tree.find_all(exp.Func):
        if isinstance(node, _PREDICATE_CONSTRUCTS_NOT_FUNCTIONS):
            continue  # AND / OR / XOR / NOT / EXISTS 是谓词构造，不是函数
        if node.find_ancestor(exp.Hint) is not None:
            continue  # 优化器提示不是 SQL 函数
        if isinstance(node, exp.Anonymous):
            names.add(node.name)
        else:
            names.add(node.sql_name())
    return names
