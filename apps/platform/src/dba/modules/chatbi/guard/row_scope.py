"""行级权限：注入（权威防线）+ 覆盖性断言（兜底）+ scope 编译。

对齐《设计方案 v2》§6.3.2 ③ / ③' / §6.3.3 与《实现要点清单》§5.8、P0-10。

★ 为什么这是整条链路的**安全红线**（照抄 v1 会怎样错）
------------------------------------------------------
1) v1 把「三行 SQL 文本」当作权限依据，且把「未配置规则」与「规则解析出的值域为空」
   同等放行。后者意味着「该用户一行都不该看见」时反而**放行整张表**；
   且 `col IN ()` 在部分优化器下会退化成「不过滤」。v2 区分二者：未配置 → 放行；
   值域为空 → ``SQL_SCOPE_EMPTY`` 拒绝。
2) v1 的 `_and_where` 未定义**作用域归属**，把谓词统一加到最外层 WHERE，
   在 CTE / 子查询 / UNION 分支上会改变语义。v2 按「每个 SELECT 自身的作用域」注入。
3) v1 的兜底断言是「该 SELECT 的谓词列里出现过 scope 列名」——**假断言**，两种绕过都能过::

       WHERE region_code = 'EAST' OR 1 = 1     ← 列名出现了，条件被 OR 短路
       WHERE region_code IS NOT NULL           ← 列名出现了，但没有任何限制

   v2 改为**合取项断言**：顶层 AND 链上必须存在一个「取值等于 scope 值域」的谓词，
   且不得位于任何 OR 分支内。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import sqlglot
from dba_runtime.context import RunContext
from sqlglot import exp

from dba.capabilities.memory.scope import build_scope_hash

from .base import GuardResult, sql_guard_error

__all__ = [
    "ScopePredicate",
    "CompiledScope",
    "RowScopeCompiler",
    "JoinPath",
    "JoinPathResolver",
    "FieldMappingJoinResolver",
    "RowScopeInjector",
    "RowScopeVerifier",
]

logger = logging.getLogger("dba.modules.chatbi.guard.row_scope")


# ═════════════════════════════════════════════════════════════════════
# 接口契约
# ═════════════════════════════════════════════════════════════════════
@runtime_checkable
class ScopePredicate(Protocol):
    """编译后的权限子域（供各护栏只读消费）。

    ★ 签名按 §6.3.2 调用点反推：``is_empty`` / ``protected_tables`` /
    ``column_for`` / ``values_for``。
    """

    def is_empty(self) -> bool:
        """``True`` 表示「有规则但值域为空」——该用户一行都不该看见，必须拒绝。"""
        ...

    def protected_tables(self) -> set[str]:
        """受行级权限保护的物理表集合。"""
        ...

    def column_for(self, table: str) -> str:
        """该受保护表对应的权限列名。"""
        ...

    def values_for(self, table: str) -> list[Any]:
        """该受保护表允许的取值集合。"""
        ...


@dataclass
class CompiledScope:
    """一次权限编译的产物（实现 ``ScopePredicate``）。

    ``per_table``：受保护表 → ``(权限列名, 允许取值)``。
    """

    per_table: dict[str, tuple[str, list[Any]]] = field(default_factory=dict)
    rule_version: str | None = None

    def is_empty(self) -> bool:
        # 无规则时调用方会拿到 None（见 RowScopeCompiler.compile），故此处：
        # 只要存在「有列但取值集合为空」的表，即视为空值域。
        return any(len(values) == 0 for _col, values in self.per_table.values())

    def protected_tables(self) -> set[str]:
        return set(self.per_table)

    def column_for(self, table: str) -> str:
        return self.per_table[table][0]

    def values_for(self, table: str) -> list[Any]:
        return list(self.per_table[table][1])


@dataclass(frozen=True)
class JoinPath:
    """维表 join 路径（权限列不在事实表上时使用）。"""

    dim_table: str
    on: str | None = None


@runtime_checkable
class JoinPathResolver(Protocol):
    """按受保护表取 join 路径；无配置返回 ``None``。"""

    async def join_path_for(self, table: str) -> JoinPath | None: ...


# ═════════════════════════════════════════════════════════════════════
# scope 编译
# ═════════════════════════════════════════════════════════════════════
def _values_of(rule: dict[str, Any], user_vars: dict[str, Any]) -> list[Any]:
    """由一条 ``row_scope_rule`` 解析出允许取值集合。

    ★ 权限变量缺失 → 返回**空集合**（后续拒绝执行），绝不因缺失而放行全表。
    """
    value_type = str(rule.get("value_type", "STATIC"))
    if value_type == "CTX_VAR":
        var = str(rule.get("ctx_var") or "")
        raw = user_vars.get(var)
        if raw is None:
            return []
        return list(raw) if isinstance(raw, (list, tuple)) else [raw]

    raw_json = rule.get("value_json")
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("row_scope_rule.value_json 非法 JSON，按单值处理")
    if isinstance(raw_json, (list, tuple)):
        return list(raw_json)
    if raw_json is None:
        return []
    return [raw_json]


def _predicate_text(table: str, column: str, values: list[Any]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{table}.{column} IN ({rendered})"


class RowScopeCompiler:
    """把 ``row_scope_rule`` 行集合编译为 ``CompiledScope``。

    * 无任何适用规则 → 返回 ``None``（等价于「该用户未配置行级权限」→ 不注入）；
    * 规则版本取该角色集 ``row_scope_rule`` 的最大 ``updated_at``（★ P0-10），
      参与 ``scope_hash``；否则改了规则但 hash 不变 → 继续命中旧权限缓存。
    """

    def __init__(self, scope_rule_repo: Any) -> None:
        self._rules = scope_rule_repo

    async def compile(
        self, *, role_ids: list[int], user_vars: dict[str, Any] | None = None
    ) -> CompiledScope | None:
        """编译权限子域；无规则返回 ``None``。"""
        rules = await self._rules.rules_for_roles(list(role_ids))
        per_table: dict[str, tuple[str, list[Any]]] = {}
        for rule in rules:
            if int(rule.get("enabled", 1) or 0) != 1:
                continue
            table = str(rule.get("physical_table", "") or "")
            column = str(rule.get("scope_column", "") or "")
            if not table or not column:
                continue
            if table in per_table:  # 同表多规则：保留 repo 已按优先级排序后的第一条
                continue
            per_table[table] = (column, _values_of(rule, user_vars or {}))
        if not per_table:
            return None
        version = await self._rules.rule_version(list(role_ids))
        return CompiledScope(per_table=per_table, rule_version=version)

    @staticmethod
    def scope_hash(scope: CompiledScope) -> str:
        """``sha256(canonical_json(tables + predicates + rule_version))[:32]``（§6.6）。"""
        tables = sorted(scope.protected_tables())
        predicates = sorted(
            _predicate_text(t, scope.column_for(t), scope.values_for(t))
            for t in tables
            if scope.values_for(t)
        )
        return build_scope_hash(tables, predicates, scope.rule_version)


# ═════════════════════════════════════════════════════════════════════
# join 路径解析（维表情形）
# ═════════════════════════════════════════════════════════════════════
def _parse_join_path(raw: Any) -> JoinPath | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return _parse_join_path(json.loads(text))
        except json.JSONDecodeError:
            return JoinPath(dim_table=text, on=None)
    if isinstance(raw, dict):
        dim = raw.get("dim_table") or raw.get("table") or raw.get("dim")
        if not dim:
            return None
        on = raw.get("on") or raw.get("join_on")
        return JoinPath(dim_table=str(dim), on=str(on) if on else None)
    return None


class FieldMappingJoinResolver(JoinPathResolver):
    """从 ``sem_field_mapping.join_path`` 解析维表路径（带进程内缓存）。"""

    def __init__(self, field_mapping_repo: Any) -> None:
        self._repo = field_mapping_repo
        self._cache: dict[str, JoinPath | None] = {}

    async def join_path_for(self, table: str) -> JoinPath | None:
        if table in self._cache:
            return self._cache[table]
        rows = await self._repo.all_mappings()
        resolved: JoinPath | None = None
        for row in rows:
            if str(row.get("physical_table", "")) != table:
                continue
            resolved = _parse_join_path(row.get("join_path"))
            if resolved is not None:
                break
        self._cache[table] = resolved
        return resolved


# ═════════════════════════════════════════════════════════════════════
# AST 工具
# ═════════════════════════════════════════════════════════════════════
def _parse_tree(sql: str, stage: str) -> Any:
    try:
        return sqlglot.parse_one(sql, dialect="mysql")
    except Exception as exc:  # noqa: BLE001
        raise sql_guard_error(f"语法解析失败：{exc}", "SQL_PARSE_ERROR", stage=stage) from exc


def _literal(value: Any) -> Any:
    if isinstance(value, bool):
        return exp.Literal.number(1 if value else 0)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _literal_value(node: exp.Expression) -> str:
    if isinstance(node, exp.Literal):
        return str(node.this)
    return node.sql(dialect="mysql").strip("'")


def _in_predicate(table: str, column: str, values: list[Any]) -> Any:
    col = exp.column(column, table=table) if table else exp.column(column)
    return exp.In(this=col, expressions=[_literal(v) for v in values])


def _tables_in(tree: Any) -> set[str]:
    """SQL 中出现的物理表（**排除 CTE 别名**，否则会把 CTE 名当物理表）。"""
    cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    return {t.name for t in tree.find_all(exp.Table) if t.name and t.name not in cte_names}


def _alias_map(tree: Any) -> dict[str, str]:
    """``别名 / 表名 → 物理表名``（用于把 ``d.region`` 解析到它真正所属的表）。"""
    mapping: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        if not table.name:
            continue
        mapping[table.name] = table.name
        alias = table.alias
        if alias:
            mapping[alias] = table.name
    return mapping


def _column_usage(tree: Any, column: str) -> tuple[bool, set[str]]:
    """统计名为 ``column`` 的列被如何引用。

    返回 ``(是否存在无限定引用, 被显式限定的所有者物理表集合)``。

    ★ 这是修复 QA-CRITICAL-1/-2 的关键：v1 只问「列名是否在整个 AST 里出现」——
      当同名权限列出现在**维表**（``d.region``）上时被误判为「该列在主表上」，
      于是把谓词挂到主表的同名列（不存在 → 误拒；恰好同名 → 越权泄露）。
      这里按 ``exp.Column.table`` 把引用**解析回它真正所属的表**，才能区分「主表自身列」
      与「维表权限列」。
    """
    aliases = _alias_map(tree)
    unqualified = False
    owners: set[str] = set()
    for col in tree.find_all(exp.Column):
        if col.name != column:
            continue
        qualifier = col.table
        if not qualifier:
            unqualified = True
            continue
        owners.add(aliases.get(qualifier, qualifier))
    return unqualified, owners


def _scope_target_table(tree: Any, table: str, column: str, path: JoinPath | None) -> str | None:
    """判定行级权限谓词应注入到**哪张表**；``None`` 表示无法安全注入（必须拒绝）。

    优先级（QA 建议：有 join_path 且维表已在查询中 → 优先维表路径）：

    1. 列被**显式限定**在受保护表上（``f.region``）→ 注入 ``f``；
    2. 配置了 join_path 且维表**已出现在本次查询**里 → 注入维表（权威语义在维表）；
    3. 列以**无限定**形式出现（``region``）→ 注入受保护表（MySQL 会自行解析到实际表）；
    4. 其余（列完全没出现、或只出现在别的表上却没有可用 join 路径）→ ``None`` → 拒绝。

    ★ 不再「默认落到情形 B」：只有当维表**确实在查询里**时才允许注入维表，
      否则会产出引用不存在表的 SQL（QA-MAJOR-1 的假绿根因）。
    """
    unqualified, owners = _column_usage(tree, column)
    dim_joined = path is not None and path.dim_table in _tables_in(tree)
    if table in owners:
        return table
    if dim_joined:
        return path.dim_table  # type: ignore[union-attr]
    if unqualified:
        return table
    return None


def _selects_referencing(tree: Any, table: str) -> list[Any]:
    """ "自身作用域内引用该表的 SELECT"（就近归属，避免跨作用域重复注入）。"""
    found: list[Any] = []
    seen: set[int] = set()
    for tbl in tree.find_all(exp.Table):
        if tbl.name != table:
            continue
        sel = tbl.find_ancestor(exp.Select)
        if sel is not None and id(sel) not in seen:
            seen.add(id(sel))
            found.append(sel)
    return found


def _qualifier_in(select: Any, table: str) -> str:
    """取 ``table`` 在**该 SELECT** 内可用的限定名：被起了别名就用别名，否则用表名。

    ★ 为什么必须这样：MySQL 里 ``FROM t_dim AS d`` 之后，``t_dim.region`` 会变成
    「未知列」——原表名被别名覆盖。若一律用物理表名限定，一旦查询给维表起了别名，
      注入谓词就会让整条 SQL 不可执行（等价于 QA-MAJOR-1 的故障形态）。
    """
    for node in select.find_all(exp.Table):
        if node.name == table and node.alias:
            return str(node.alias)
    return table


def _scope_conjunct_present(
    select: Any, *, column: str, expected: set[str], target: str | None
) -> bool:
    """该 SELECT 的**顶层 AND 链**上是否已存在「等价」的 scope 合取项。

    ★ 这是注入器与 Verifier 的**同源判据**：``_conjunct_matches`` 已比对
      「表限定 + 列名 + 值域」三者，正是「等价」的定义。注入器直接复用它判断
      「是否已经注入过」，**不另写一套等价判据**——两侧判据一旦漂移，就会出现
      「注入器认为已存在、Verifier 认为缺失」的假绿／假红（本次会话已多次证明
      「同源判据」是这套护栏自洽的关键，见 ``_scope_target_table`` 的复用）。

    等价语义边界（刻意为之，宁可重复也不漏判）：只有当合取项**显式限定到一张
    真实存在于本 SELECT 的表**、且（已知 target 时）与该 target 一致、且取值域
    完全相等时才算「已存在」。无法证明等价 → 视为不存在 → 仍会追加，
    由 Verifier 兜底（fail-closed 方向）。
    """
    aliases = _alias_map(select)
    tables = _tables_in(select)
    return any(
        _conjunct_matches(
            conjunct,
            column=column,
            expected=expected,
            aliases=aliases,
            tables=tables,
            target=target,
        )
        for conjunct in _top_level_and_conjuncts(select)
    )


def _inject(
    tree: Any,
    table: str,
    make_predicate: Any,
    *,
    column: str,
    values: list[Any],
    target: str | None,
) -> tuple[int, int]:
    """把谓词 AND 到「引用该表的每个 SELECT」的 WHERE 上。返回 ``(作用域数, 新注入数)``。

    ``make_predicate(select)`` 按**每个 SELECT 自身**构造谓词（限定名可能不同）。

    ★ 幂等（必须）：注入前先判断该 SELECT 的顶层 AND 链上是否**已存在等价 scope
      合取项**，有则跳过。否则护栏链「跑两遍」会重复追加谓词——``sql_guard`` 步骤
      改写过一次，``sql_exec`` 经 ``QueryExecutor`` 再校验**同一条已改写 SQL** 时
      又追加一次。SQL 语义虽不变（``X AND X`` ⇔ ``X``），但**文本不再幂等**：
      重复谓词白白膨胀审计/回放内容，也让「链条可重复校验」这一不变量失效。
      跳过的判据直接复用 ``_scope_conjunct_present``（与 Verifier 同源）。

    ★ 必踩的坑：sqlglot 的 ``Select.where()`` 默认 ``copy=True``，**返回新对象而不改原树**。
    照直写 ``sel.where(pred, append=True)`` 会「调用成功但注入被丢弃」——SQL 原样放行，
    行级权限彻底失效且**没有任何报错**。因此这里显式 ``copy=False`` 就地 AND 到该
    SELECT 的 WHERE 上（``sel`` 是 ``find_ancestor`` 命中的原树节点，就地改会反映到根树）。
    """
    targets = _selects_referencing(tree, table)
    expected = {str(v) for v in values}
    appended = 0
    for sel in targets:
        if _scope_conjunct_present(sel, column=column, expected=expected, target=target):
            continue  # ★ 幂等：该作用域已具备等价权限条件，跳过，不再重复追加
        sel.where(make_predicate(sel), append=True, copy=False)
        appended += 1
    return len(targets), appended


def _top_level_and_conjuncts(select: Any) -> list[Any]:
    """取顶层 AND 链的合取项（OR 节点整体作为一个合取项，不会被展开）。"""
    where = select.args.get("where")
    if where is None or not isinstance(where, exp.Where):
        return []

    def _flatten(node: Any) -> list[Any]:
        if isinstance(node, exp.And):
            return _flatten(node.this) + _flatten(node.expression)
        return [node]

    return _flatten(where.this)


def _conjunct_matches(
    conjunct: Any,
    *,
    column: str,
    expected: set[str],
    aliases: dict[str, str],
    tables: set[str],
    target: str | None,
) -> bool:
    """合取项是否「在**正确的表**上、于 scope 列取值等于 scope 值域」。

    ★ 注入器（幂等判据）与 Verifier（覆盖断言）**共用**此函数，不得各写一份：
      两侧对「等价」的理解一旦漂移，就会出现「注入器认为已存在而 Verifier 认为缺失」
      （或反之）的假绿/假红。

    ★ 修复 QA 指出的「兜底断言只比对列名、不比对所属表」：合取项必须把列**显式限定**
      到一张真实存在于本 SELECT 的表上；已知目标表时还必须与该目标表一致。
      否则 ``WHERE t_other.same_name_col IN (...)`` 之类的同名跨表谓词能骗过断言。
    """
    if isinstance(conjunct, exp.In):
        col = conjunct.this
        got = {_literal_value(e) for e in conjunct.expressions}
    elif isinstance(conjunct, exp.EQ):
        col = conjunct.this
        got = {_literal_value(conjunct.expression)}
    else:
        return False

    if not (isinstance(col, exp.Column) and col.name == column):
        return False
    qualifier = col.table
    if not qualifier:
        return False  # 无限定 → 无法证明作用在正确的表上
    owner = aliases.get(qualifier, qualifier)
    if owner not in tables:
        return False  # 引用了不在本 SELECT 里的表（悬空引用，如 QA-MAJOR-1）
    if target is not None and owner != target:
        return False  # 谓词挂错了表
    return got == expected


# ═════════════════════════════════════════════════════════════════════
# ③ 权威防线：注入
# ═════════════════════════════════════════════════════════════════════
class RowScopeInjector:
    """行级权限注入（**权威防线**，§6.3.2 ③ / §6.3.3 防线二）。"""

    name = "row_scope"

    def __init__(self, scope_compiler: RowScopeCompiler, mapping: JoinPathResolver) -> None:
        self._compiler = scope_compiler
        self._mapping = mapping

    async def check(self, sql: str, ctx: RunContext, scope: ScopePredicate | None) -> GuardResult:
        if scope is None:
            return GuardResult(ok=True)  # 未配置任何行级规则 → 不注入（放行）
        if scope.is_empty():
            raise sql_guard_error("权限值域为空，拒绝执行", "SQL_SCOPE_EMPTY", stage=self.name)

        tree = _parse_tree(sql, self.name)
        protected = _tables_in(tree) & scope.protected_tables()
        if not protected:
            return GuardResult(ok=True, rewritten_sql=tree.sql(dialect="mysql"))

        for table in sorted(protected):
            column = scope.column_for(table)
            values = scope.values_for(table)
            if not values:
                raise sql_guard_error("权限值域为空，拒绝执行", "SQL_SCOPE_EMPTY", stage=self.name)

            # ★ 表感知的目标解析（替代 v1 的「列名是否出现」）：
            #   区分「权限列就在受保护表上」与「权限列在维表上」，避免
            #   ① 挂到主表不存在的同名列 → dry-run 误拒（QA-CRITICAL-2）
            #   ② 挂到主表恰好同名的列 → 真实越权泄露（QA-CRITICAL-1）
            path = await self._mapping.join_path_for(table)
            target = _scope_target_table(tree, table, column, path)
            if target is None:
                raise sql_guard_error(
                    f"表 {table} 受行级权限保护，但无法安全注入：权限列 {column} "
                    f"未出现在查询中且缺少可用的 join 路径配置",
                    "SQL_SCOPE_JOIN_MISSING",
                    stage=self.name,
                )
            # 注入点始终是「引用受保护表的 SELECT」；谓词挂到 target 表的权限列上，
            # 且限定名按每个 SELECT 自身的别名解析（见 _qualifier_in）。
            injected_scopes, _appended = _inject(
                tree,
                table,
                lambda sel, _t=target, _c=column, _v=values: _in_predicate(
                    _qualifier_in(sel, _t), _c, _v
                ),
                column=column,
                values=values,
                target=target,
            )
            # ★ fail-closed（缺陷 3「静默空注入」）：受保护表已在查询中解析出 target，
            #   但若**没有任何引用该表的 SELECT 作用域**（0 个，例如非 SELECT 语句
            #   或 AST 形态变化导致找不到 Select 祖先），**绝不能**返回 ok=True 放行——
            #   那等于「宣称已注入却一个谓词都没加」。此处不依赖 RowScopeVerifier 兜底
            #   （纵深防御）。
            #   ★ 判据是「**作用域数** == 0」而非「本次新注入数 == 0」：幂等重跑时新注入数
            #     本就有 0（等价谓词已存在），那属于**已满足**，不是「静默空注入」。
            if injected_scopes == 0:
                raise sql_guard_error(
                    f"受保护表 {table} 在本次查询中未定位到可注入的 SELECT 作用域"
                    f"（0 个作用域），按 fail-closed 拒绝执行",
                    "SQL_SCOPE_NOT_INJECTED",
                    stage=self.name,
                )

        return GuardResult(ok=True, rewritten_sql=tree.sql(dialect="mysql"), scope_injected=True)


# ═════════════════════════════════════════════════════════════════════
# ③' 兜底：覆盖性断言
# ═════════════════════════════════════════════════════════════════════
class RowScopeVerifier:
    """注入覆盖性断言（**兜底防线**，§6.3.2 ③' / §6.3.3 防线三）。

    判据：对每个引用受保护表的 SELECT，其 WHERE 的**顶层 AND 链**上必须存在一个
    **作用在正确表上**、且取值等于 scope 值域的合取项；值域为空直接 deny。

    ★ 与注入器共用 ``_scope_target_table`` 解析「应该注入到哪张表」，二者**同源**——
      否则断言与注入对「正确表」的理解不一致，就会出现「注入错了但断言通过」的假绿。
    """

    name = "row_scope_verify"

    def __init__(self, mapping: JoinPathResolver | None = None) -> None:
        self._mapping = mapping

    async def check(self, sql: str, ctx: RunContext, scope: ScopePredicate | None) -> GuardResult:
        if scope is None:
            return GuardResult(ok=True)

        tree = _parse_tree(sql, self.name)
        for table in sorted(scope.protected_tables()):
            expected = {str(v) for v in scope.values_for(table)}
            if not expected:
                raise sql_guard_error("权限值域为空，拒绝执行", "SQL_SCOPE_EMPTY", stage=self.name)
            column = scope.column_for(table)
            path = await self._mapping.join_path_for(table) if self._mapping is not None else None
            target = _scope_target_table(tree, table, column, path)
            for select in _selects_referencing(tree, table):
                # ★ 与注入器**同一判据** `_scope_conjunct_present`（内部复用 `_conjunct_matches`），
                #   避免两侧对「等价 scope 条件」的理解漂移而出现假绿/假红。
                if not _scope_conjunct_present(
                    select, column=column, expected=expected, target=target
                ):
                    raise sql_guard_error(
                        f"受保护表 {table} 未在顶层 AND 链上注入行级权限条件",
                        "SQL_SCOPE_NOT_INJECTED",
                        stage=self.name,
                    )
        return GuardResult(ok=True)
