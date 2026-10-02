"""真实浏览器联调发现项的隔离回归测试。"""

from __future__ import annotations

from typing import Any

from dba.api.deps import Principal
from dba.api.v1.chatbi import _run_terminal_status, delete_session
from dba.api.v1.finops import patch_budget
from dba.api.v1.observability import register_agent, resolve_anomaly
from dba.api.v1.semantics import create_metric, patch_metric
from dba.capabilities.messaging.progress import ProgressPublisher
from dba.storage.es.indices import INDEX_SPECS
from dba.storage.es.repo import EventIndexRepo
from dba.storage.mongo.repo import ChatSessionRepo, FinopsRecommendationRepo
from dba.storage.mysql.repo import PriceBookRepo, SkillRegistryRepo


class _Container:
    def __init__(self, **values: Any) -> None:
        self.values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class _RecommendationCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs

    def sort(self, *_args: Any) -> _RecommendationCursor:
        return self

    def limit(self, _limit: int) -> _RecommendationCursor:
        return self

    async def to_list(self, *, length: int) -> list[dict[str, Any]]:
        return self.docs[:length]


class _RecommendationCollection:
    def __init__(self) -> None:
        self.query: dict[str, Any] | None = None

    def find(self, query: dict[str, Any]) -> _RecommendationCursor:
        self.query = query
        return _RecommendationCursor([])


class _SessionCollection:
    def __init__(self) -> None:
        self.filter: dict[str, Any] | None = None
        self.update: dict[str, Any] | None = None

    async def update_one(self, filter_: dict[str, Any], update: dict[str, Any]) -> Any:
        self.filter = filter_
        self.update = update
        return type("Result", (), {"matched_count": 1})()


async def _await(coro: Any) -> Any:
    return await coro


def test_recommendation_filter_applies_scope_fields() -> None:
    repo = FinopsRecommendationRepo(None)  # type: ignore[arg-type]
    collection = _RecommendationCollection()
    repo._coll = lambda _name: collection  # type: ignore[method-assign]
    repo._run = _await  # type: ignore[method-assign]

    import asyncio

    asyncio.run(
        repo.list(
            {
                "scope_type": "BIZ_LINE",
                "scope_id": "synthetic-scope",
                "status": "pending",
                "biz_line_id": 7,
            }
        )
    )

    assert collection.query == {
        "scope_type": "BIZ_LINE",
        "scope_id": "synthetic-scope",
        "status": "pending",
        "biz_line_id": 7,
    }


def test_chat_session_soft_delete_is_scoped_to_owner() -> None:
    import asyncio

    repo = ChatSessionRepo(None)  # type: ignore[arg-type]
    collection = _SessionCollection()
    repo._coll = lambda _name: collection  # type: ignore[method-assign]
    repo._run = _await  # type: ignore[method-assign]

    assert asyncio.run(repo.soft_delete("synthetic-session", 23)) is True
    assert collection.filter == {"session_id": "synthetic-session", "user_id": 23}
    assert collection.update is not None and collection.update["$set"]["deleted"] is True


def test_delete_session_enforces_owner_and_persists_soft_delete() -> None:
    import asyncio

    class SessionRepo:
        def __init__(self, owner: int) -> None:
            self.owner = owner
            self.delete_args: tuple[str, int] | None = None

        async def get(self, _session_id: str) -> dict[str, Any]:
            return {"user_id": self.owner}

        async def soft_delete(self, session_id: str, user_id: int) -> bool:
            self.delete_args = session_id, user_id
            return True

    principal = Principal(user_id=23, username="owner", role_ids=[])
    owned = SessionRepo(23)
    success = asyncio.run(
        delete_session(
            "synthetic-session",
            principal,
            _Container(mongo_repos=type("Mongo", (), {"chat_session": owned})()),  # type: ignore[arg-type]
        )
    )
    assert success == {"ok": True}
    assert owned.delete_args == ("synthetic-session", 23)

    foreign = SessionRepo(24)
    forbidden = asyncio.run(
        delete_session(
            "synthetic-session",
            principal,
            _Container(mongo_repos=type("Mongo", (), {"chat_session": foreign})()),  # type: ignore[arg-type]
        )
    )
    assert forbidden.status_code == 403  # type: ignore[union-attr]
    assert foreign.delete_args is None


def test_price_and_dead_skill_queries_include_requested_filters() -> None:
    import asyncio

    statements: list[Any] = []

    async def capture(stmt: Any) -> list[dict[str, Any]]:
        statements.append(stmt)
        return []

    prices = PriceBookRepo(None)  # type: ignore[arg-type]
    prices._fetch = capture  # type: ignore[method-assign]
    skills = SkillRegistryRepo(None)  # type: ignore[arg-type]
    skills._fetch = capture  # type: ignore[method-assign]

    asyncio.run(prices.list_active("provider-x", "model-y"))
    asyncio.run(skills.list_for_observability(7, True))

    price_sql = str(statements[0])
    skill_sql = str(statements[1])
    assert "price_book.provider =" in price_sql
    assert "price_book.model =" in price_sql
    assert "skill_registry.is_dead =" in skill_sql
    assert "skill_registry.biz_line_id =" in skill_sql


def test_semantic_metric_routes_persist_canonical_fields_and_version() -> None:
    import asyncio

    class MetricRepo:
        created: dict[str, Any] | None = None
        patched: tuple[str, int | None, dict[str, Any]] | None = None

        async def insert(self, row: dict[str, Any]) -> int:
            self.created = row
            return 9

        async def patch(
            self, code: str, biz_line_id: int | None, values: dict[str, Any]
        ) -> dict[str, Any]:
            self.patched = code, biz_line_id, values
            return {"version": 2}

    metrics = MetricRepo()
    container = _Container(repos=type("Repos", (), {"sem_metric": metrics})())
    admin = Principal(user_id=23, username="admin", role_ids=[1], biz_line_id=7)
    created = asyncio.run(
        create_metric(
            {
                "code": "synthetic_metric",
                "name": "Synthetic Metric",
                "description": "test definition",
                "expression": "SUM(amount)",
                "biz_line_id": 7,
            },
            admin,
            container,  # type: ignore[arg-type]
        )
    )
    updated = asyncio.run(
        patch_metric("synthetic_metric", {"name": "Updated Name"}, admin, container)  # type: ignore[arg-type]
    )

    assert created == {"metric_id": "synthetic_metric", "created": True}
    assert metrics.created is not None
    assert metrics.created["metric_name"] == "Synthetic Metric"
    assert metrics.created["caliber_desc"] == "test definition"
    assert metrics.created["sql_expr"] == "SUM(amount)"
    assert metrics.created["created_by"] == 23
    assert updated == {"version": 2}
    assert metrics.patched == ("synthetic_metric", 7, {"metric_name": "Updated Name"})


def test_platform_admin_semantic_patch_is_not_limited_to_null_biz_line() -> None:
    import asyncio

    from dba.storage.mysql.repo import SemMetricRepo

    statements: list[Any] = []

    async def capture_update(stmt: Any, *, many: list[dict[str, Any]] | None = None) -> Any:
        _ = many
        statements.append(stmt)
        return type("Result", (), {"rowcount": 1})()

    async def capture_read(stmt: Any) -> dict[str, Any]:
        statements.append(stmt)
        return {"metric_code": "synthetic_metric", "version": 2}

    repo = SemMetricRepo(None)  # type: ignore[arg-type]
    repo._execute = capture_update  # type: ignore[method-assign]
    repo._fetch_one = capture_read  # type: ignore[method-assign]

    row = asyncio.run(repo.patch("synthetic_metric", None, {"metric_name": "Updated"}))

    assert row == {"metric_code": "synthetic_metric", "version": 2}
    assert len(statements) == 2
    assert "biz_line_id" not in str(statements[0].whereclause)
    assert "biz_line_id" not in str(statements[1].whereclause)


def test_invalid_agent_runtime_type_is_rejected_before_storage() -> None:
    import asyncio

    class AgentRepo:
        async def upsert(self, _row: dict[str, Any]) -> None:
            raise AssertionError("invalid runtime_type must not reach storage")

    response = asyncio.run(
        register_agent(
            {"agent_uid": "synthetic-agent", "name": "test", "runtime_type": "invalid"},
            _Container(repos=type("Repos", (), {"app_agent": AgentRepo()})()),  # type: ignore[arg-type]
            "agent-token",
        )
    )
    assert response.status_code == 400


def test_resolve_alert_uses_resolve_repository_operation() -> None:
    import asyncio

    class AlertRepo:
        resolved: list[int] = []

        async def resolve(self, alert_id: int) -> int:
            self.resolved.append(alert_id)
            return 1

    alerts = AlertRepo()
    result = asyncio.run(
        resolve_anomaly(
            914,
            {},
            _Container(repos=type("Repos", (), {"alert_event": alerts})()),  # type: ignore[arg-type]
            Principal(user_id=23, username="admin", role_ids=[1]),
        )
    )
    assert result == {"ok": True}
    assert alerts.resolved == [914]


def test_budget_patch_appends_next_version_without_reusing_primary_key() -> None:
    import asyncio

    class BudgetRepo:
        inserted: dict[str, Any] | None = None

        async def get(self, budget_id: int) -> dict[str, Any] | None:
            assert budget_id == 29
            return {
                "id": 29,
                "scope_type": "GLOBAL",
                "scope_id": "synthetic-budget",
                "period": "MONTH",
                "amount_micro_usd": 1_000_000,
                "version": 3,
                "created_at": "old-created",
                "updated_at": "old-updated",
            }

        async def upsert(self, row: dict[str, Any]) -> int:
            self.inserted = row
            return 30

    budget = BudgetRepo()
    result = asyncio.run(
        patch_budget(
            29,
            {"amount_micro_usd": 2_000_000, "scope_id": "must-not-move", "version": 99},
            Principal(user_id=23, username="admin", role_ids=[1]),
            _Container(repos=type("Repos", (), {"budget": budget})()),  # type: ignore[arg-type]
        )
    )

    assert result == {"version": 4}
    assert budget.inserted is not None
    assert "id" not in budget.inserted
    assert "created_at" not in budget.inserted
    assert "updated_at" not in budget.inserted
    assert budget.inserted["scope_id"] == "synthetic-budget"
    assert budget.inserted["version"] == 4


def test_es_http_audit_write_uses_non_conflicting_http_status_field() -> None:
    import asyncio

    class Storage:
        document: dict[str, Any] | None = None

        def alias(self, suffix: str) -> str:
            return f"test-{suffix}"

        async def index(self, _alias: str, document: dict[str, Any]) -> None:
            self.document = document

    storage = Storage()
    asyncio.run(
        EventIndexRepo(storage).write(
            {
                "method": "GET",
                "path": "/api/v1/test",
                "status": 401,
                "latency_ms": 4,
                "client": "127.0.0.1",
            }
        )
    )
    assert storage.document is not None
    assert storage.document["http_status"] == 401
    assert "status" not in storage.document
    mapping = INDEX_SPECS[0].properties
    assert {"method", "path", "http_status", "latency_ms", "client"} <= mapping.keys()


def test_es_startup_mapping_update_reaches_existing_write_index(monkeypatch: Any) -> None:
    import asyncio

    import dba.storage.es.client as es_client

    class Indices:
        mappings: dict[str, dict[str, Any]] = {}

        async def put_index_template(self, **_kwargs: Any) -> None:
            return None

        async def exists(self, *, index: str) -> bool:
            return True

        async def get_alias(self, *, name: str) -> dict[str, Any]:
            assert name == "synthetic-run-event"
            return {"synthetic-run-event-000001": {"aliases": {name: {}}}}

        async def put_mapping(self, *, index: str, properties: dict[str, Any]) -> None:
            self.mappings[index] = properties

        async def exists_alias(self, *, name: str) -> bool:
            return name == "synthetic-run-event"

    class Ilm:
        async def put_lifecycle(self, **_kwargs: Any) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.indices = Indices()
            self.ilm = Ilm()

    fake_client = Client()
    monkeypatch.setattr(es_client, "build_es", lambda _url: fake_client)
    storage = es_client.EsStorage("http://unused", prefix="synthetic")
    asyncio.run(storage.ensure_indices())

    properties = fake_client.indices.mappings["synthetic-run-event-000001"]
    assert {"method", "path", "http_status", "latency_ms", "client"} <= properties.keys()


def test_startup_warmup_calls_es_mapping_initialization() -> None:
    import asyncio
    from types import SimpleNamespace

    from dba.di import Container, warmup

    class EsRepo:
        calls = 0

        async def ensure_indices(self) -> None:
            self.calls += 1

    async def exercise() -> int:
        repo = EsRepo()
        container = Container()
        container.set(
            "storage",
            SimpleNamespace(repos=None, redis=None, es_repo=repo),
        )
        await warmup(container)
        return repo.calls

    assert asyncio.run(exercise()) == 1


def test_run_terminal_status_preserves_supported_states() -> None:
    assert _run_terminal_status("success") == "success"
    assert _run_terminal_status("timeout") == "timeout"
    assert _run_terminal_status("aborted") == "aborted"
    assert _run_terminal_status("budget_exceeded") == "failed"


def test_sse_reports_old_cursor_gap_before_replaying_retained_terminal() -> None:
    import asyncio

    class Writer:
        async def replay_page(
            self, _trace_id: str
        ) -> tuple[list[dict[str, Any]], int | None, int | None]:
            rows = [
                {"seq": 1048, "event": "agent.step.finished", "data": {"step": "sql_gen"}},
                {"seq": 1247, "event": "run.finished", "data": {"status": "success"}},
            ]
            return rows, 1048, 1247

        async def replay(self, _trace_id: str, *, after_seq: int = 0) -> list[dict[str, Any]]:
            return []

    from dba.api.v1.stream import _stream

    async def frames() -> list[str]:
        publisher = ProgressPublisher(Writer())
        return [frame async for frame in _stream(publisher, "trace", 1)]

    result = asyncio.run(frames())
    assert "event: replay.gap" in result[0]
    assert '"after_seq": 1' in result[0]
    assert "id: 1048\nevent: agent.step.finished" in result[1]
    assert "id: 1247\nevent: run.finished" in result[2]


def test_stream_accepts_query_cursor_when_eventsource_cannot_send_header() -> None:
    import asyncio

    from starlette.requests import Request

    class Tickets:
        def verify(self, _ticket: str, *, trace_id: str) -> object:
            assert trace_id == "synthetic-trace"
            return object()

    class Writer:
        async def replay_page(
            self, _trace_id: str
        ) -> tuple[list[dict[str, Any]], int, int]:
            return (
                [
                    {"seq": 56, "event": "agent.step.finished", "data": {"step": "sql_gen"}},
                    {"seq": 57, "event": "run.finished", "data": {"status": "success"}},
                ],
                56,
                57,
            )

    from dba.api.v1.stream import stream_run

    async def read_stream() -> list[str]:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
            }
        )
        response = await stream_run(
            request,
            "synthetic-trace",
            _Container(
                stream_tickets=Tickets(), progress_publisher=ProgressPublisher(Writer())
            ),  # type: ignore[arg-type]
            ticket="synthetic-ticket",
            last_event_id=None,
            last_event_id_query="56",
        )
        frames = [
            chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            async for chunk in response.body_iterator
        ]
        return frames

    frames = asyncio.run(read_stream())
    assert len(frames) == 1
    assert "id: 57\nevent: run.finished" in frames[0]


def test_openapi_declares_bearer_agent_auth_and_runtime_400_contract() -> None:
    from dba.config import Settings
    from dba.main import create_app

    schema = create_app(Settings(env="test")).openapi()
    security = schema["components"]["securitySchemes"]
    assert security["HTTPBearer"]["type"] == "http"
    assert security["HTTPBearer"]["scheme"] == "bearer"
    assert security["AgentToken"]["in"] == "header"
    assert security["AgentToken"]["name"] == "X-Agent-Token"

    operation = schema["paths"]["/api/v1/semantics/search"]["get"]
    assert "400" in operation["responses"]
    assert "422" not in operation["responses"]
