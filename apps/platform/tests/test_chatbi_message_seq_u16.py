"""U16 回归：``chat_message.seq`` 必须经 ``chat_session.next_message_seq($inc)`` 原子分配。

并发逐条 ``count+1`` 会撞 ``(session_id, seq)`` 唯一键；本用例锁定「走原子分配器」这一契约。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from dba.api.v1.chatbi import _answer_text, _append_message


class _SessionRepo:
    def __init__(self) -> None:
        self.calls = 0

    async def next_message_seq(self, session_id: str) -> int:
        self.calls += 1
        return self.calls  # 模拟 find_one_and_update($inc) 的原子自增返回


class _MessageRepo:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def append(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.docs.append(doc)
        return doc


async def test_append_message_allocates_atomic_seq() -> None:
    mongo = SimpleNamespace(chat_session=_SessionRepo(), chat_message=_MessageRepo())

    await _append_message(mongo, "s1", "t" * 32, "user", "近 30 天 GMV")
    await _append_message(mongo, "s1", "t" * 32, "assistant", "答案")

    assert mongo.chat_session.calls == 2  # 每次都经原子分配器
    assert [d["seq"] for d in mongo.chat_message.docs] == [1, 2]
    assert [d["role"] for d in mongo.chat_message.docs] == ["user", "assistant"]
    assert all(d["session_id"] == "s1" for d in mongo.chat_message.docs)


async def test_append_message_is_best_effort_on_failure() -> None:
    class _Bad:
        async def next_message_seq(self, session_id: str) -> int:
            raise RuntimeError("mongo down")

    mongo = SimpleNamespace(chat_session=_Bad(), chat_message=_MessageRepo())

    # 不得抛出（会话落库为 best-effort，不能中断 Run）
    await _append_message(mongo, "s1", "t" * 32, "user", "x")


async def test_append_message_noop_without_mongo() -> None:
    await _append_message(None, "s1", "t" * 32, "user", "x")  # 不应抛


def test_answer_text_extraction() -> None:
    assert _answer_text({"answer": "直答"}) == "直答"
    assert _answer_text({"data": {"text": "嵌套"}}) == "嵌套"
    assert "step" in _answer_text({"step": "sql"})  # 退化 JSON 摘要
