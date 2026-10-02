"""ChatBI 与 Observability Run 详情接口共用 Mongo run_doc 仓储。"""

from __future__ import annotations

from typing import Any

import pytest
from dba.api.deps import Principal
from dba.api.v1.chatbi import get_run as get_chatbi_run
from dba.api.v1.observability import get_run as get_observability_run
from dba.di import Container


class _RunDocRepo:
    def __init__(self, document: dict[str, Any] | None) -> None:
        self.document = document
        self.requested_trace_ids: list[str] = []

    async def get(self, trace_id: str) -> dict[str, Any] | None:
        self.requested_trace_ids.append(trace_id)
        return self.document


@pytest.mark.parametrize("endpoint", [get_chatbi_run, get_observability_run])
async def test_run_detail_endpoints_read_mongo_repository(endpoint: Any) -> None:
    trace_id = "a" * 32
    document = {"trace_id": trace_id, "spans": [{"name": "sql_gen"}]}
    repo = _RunDocRepo(document)
    container = Container()
    container.set("mongo_repos", type("MongoRepos", (), {"run_doc": repo})())
    principal = Principal(user_id=7, username="tester", role_ids=[])

    if endpoint is get_chatbi_run:
        result = await endpoint(trace_id, principal, container)
    else:
        result = await endpoint(trace_id, container, principal)

    assert result == document
    assert repo.requested_trace_ids == [trace_id]


@pytest.mark.parametrize("endpoint", [get_chatbi_run, get_observability_run])
async def test_run_detail_endpoints_keep_empty_object_fallback(endpoint: Any) -> None:
    container = Container()
    principal = Principal(user_id=7, username="tester", role_ids=[])

    if endpoint is get_chatbi_run:
        result = await endpoint("missing", principal, container)
    else:
        result = await endpoint("missing", container, principal)

    assert result == {}
