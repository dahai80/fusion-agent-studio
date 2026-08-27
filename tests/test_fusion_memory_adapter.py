"""Wire-contract tests for FusionMemoryAdapter — httpx.MockTransport, 100% offline。

验证 9 个 MemoryEngine-surface 方法映射到 fm-server JSON-RPC 2.0 协议:
envelope {jsonrpc,method,params,id} -> {result|error,id}, Bearer 鉴权。
失败 fail-empty (log + 空返回), 不抛。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agent_runtime.fusion_memory_adapter import FusionMemoryAdapter

ENV_KEYS = ["FUSION_MEMORY_BASE_URL", "FUSION_MEMORY_API_KEY"]


@pytest.fixture
def env_snap():
    snap = {k: __import__("os").environ.get(k) for k in ENV_KEYS}
    yield snap
    for k in ENV_KEYS:
        v = snap[k]
        if v is None:
            __import__("os").environ.pop(k, None)
        else:
            __import__("os").environ[k] = v


def make_adapter(
    handler,
    *,
    base_url: str = "http://fm.test",
    api_key: str = "test-key",
) -> tuple[FusionMemoryAdapter, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(transport_handler)
    client = httpx.Client(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        transport=transport,
    )
    adapter = FusionMemoryAdapter(base_url=base_url, api_key=api_key)
    adapter._client = client
    return adapter, captured


def rpc_ok(result: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "result": result, "id": 1},
    )


def rpc_err(code: int = -32602, message: str = "bad params") -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": 1},
    )


def http_err(status: int = 500) -> httpx.Response:
    return httpx.Response(status, text="server error")


def parse_body(req: httpx.Request) -> dict[str, Any]:
    return json.loads(req.content)


# ── 构造 / env ──────────────────────────────────────────────


class TestConstruct:
    def test_requires_api_key(self, env_snap, monkeypatch):
        monkeypatch.delenv("FUSION_MEMORY_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="FUSION_MEMORY_API_KEY"):
            FusionMemoryAdapter()

    def test_reads_env(self, env_snap, monkeypatch):
        monkeypatch.setenv("FUSION_MEMORY_BASE_URL", "http://127.0.0.1:11435")
        monkeypatch.setenv("FUSION_MEMORY_API_KEY", "env-key")
        adapter = FusionMemoryAdapter()
        assert adapter._api_key == "env-key"
        assert "11435" in adapter._base_url
        adapter.close()


# ── store -> commit ─────────────────────────────────────────


class TestStore:
    def test_commits_interaction_returns_first_id(self, env_snap):
        adapter, captured = make_adapter(lambda r: rpc_ok(["mem-1", "mem-2"]))
        try:
            entry_id = adapter.store(content="hello world", scope="sess-A")
            assert entry_id == "mem-1"
            assert len(captured) == 1
            body = parse_body(captured[0])
            assert body["method"] == "commit"
            assert captured[0].url.path == "/v1/memory/commit"
            ix = body["params"]["interaction"]
            assert ix["session_id"] == "sess-A"
            assert ix["turns"][0]["assistant_message"] == "hello world"
            assert ix["metadata"]["scope"] == "sess-A"
            assert ix["metadata"]["importance"] == 5
        finally:
            adapter.close()

    def test_bearer_auth_header(self, env_snap):
        adapter, captured = make_adapter(lambda r: rpc_ok(["x"]))
        try:
            adapter.store("c")
            assert captured[0].headers["authorization"] == "Bearer test-key"
        finally:
            adapter.close()

    def test_fail_empty_on_rpc_error(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_err())
        try:
            assert adapter.store("c") == ""
        finally:
            adapter.close()

    def test_fail_empty_on_http_500(self, env_snap):
        adapter, _ = make_adapter(lambda r: http_err(500))
        try:
            assert adapter.store("c") == ""
        finally:
            adapter.close()


# ── recall / list_recent -> retrieve ────────────────────────


def _block(**kw: Any) -> dict[str, Any]:
    base = {
        "interaction_id": "ix-1",
        "turns": [],
        "memory_type": "Episodic",
        "turns_text": "prior chat",
        "score": 0.9,
        "source_entities": ["E1"],
    }
    base.update(kw)
    return base


class TestRecall:
    def test_maps_blocks_to_entries(self, env_snap):
        ctx = {"blocks": [_block(turns_text="hello recall")], "total_tokens": 10}
        adapter, captured = make_adapter(lambda r: rpc_ok(ctx))
        try:
            entries = adapter.recall(query="hello", scope="sess-A", limit=5)
            assert len(entries) == 1
            assert entries[0].content == "hello recall"
            assert entries[0].id == "ix-1"
            body = parse_body(captured[0])
            assert body["method"] == "retrieve"
            assert body["params"]["text"] == "hello"
            assert body["params"]["top_k"] == 5
            assert body["params"]["session_id"] == "sess-A"
        finally:
            adapter.close()

    def test_empty_on_failure(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_err())
        try:
            assert adapter.recall("q") == []
        finally:
            adapter.close()

    def test_list_recent_degraded(self, env_snap):
        ctx = {"blocks": [_block(turns_text="recent")], "total_tokens": 5}
        adapter, captured = make_adapter(lambda r: rpc_ok(ctx))
        try:
            entries = adapter.list_recent(scope="sess-A", limit=20)
            assert len(entries) == 1
            assert entries[0].content == "recent"
            body = parse_body(captured[0])
            assert body["params"]["top_k"] == 20
        finally:
            adapter.close()


# ── get / delete ────────────────────────────────────────────


class TestGetDelete:
    def test_get_returns_entry(self, env_snap):
        # fm-server get 走 GET /v1/memory/{id} 路径参 (http.rs get_memory),
        # 非 POST JSON-RPC body。断言 method=GET + path=/v1/memory/m-9。
        item = {
            "id": "m-9",
            "content": "fact",
            "scope": "default",
            "tags": "",
            "weight": 7,
            "last_accessed_timestamp": 1000.0,
            "metadata": {"k": "v"},
            "tier": "long_term",
            "memory_type": "Semantic",
        }
        adapter, captured = make_adapter(lambda r: rpc_ok(item))
        try:
            entry = adapter.get("m-9")
            assert entry is not None
            assert entry.id == "m-9"
            assert entry.content == "fact"
            assert entry.importance == 7
            assert entry.metadata == {"k": "v"}
            req = captured[0]
            assert req.method == "GET"
            assert req.url.path == "/v1/memory/m-9"
        finally:
            adapter.close()

    def test_get_miss_returns_none(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_ok(None))
        try:
            assert adapter.get("nope") is None
        finally:
            adapter.close()

    def test_get_http_404_returns_none(self, env_snap):
        # 无 POST /v1/memory/get 路由 -> 活服务 404; fail-empty 不抛。
        adapter, _ = make_adapter(lambda r: http_err(404))
        try:
            assert adapter.get("m-9") is None
        finally:
            adapter.close()

    def test_delete_sends_confirm_true(self, env_snap):
        adapter, captured = make_adapter(lambda r: rpc_ok("deleted"))
        try:
            assert adapter.delete("m-9") is True
            body = parse_body(captured[0])
            assert body["method"] == "delete"
            assert body["params"]["id"] == "m-9"
            assert body["params"]["confirm"] is True
        finally:
            adapter.close()

    def test_delete_fail_returns_false(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_err())
        try:
            assert adapter.delete("m-9") is False
        finally:
            adapter.close()


# ── count / delete_scope / auto_forget ──────────────────────


class TestCountScopeForget:
    def test_count_approximates_via_retrieve(self, env_snap):
        ctx = {"blocks": [_block(), _block(), _block()], "total_tokens": 30}
        adapter, captured = make_adapter(lambda r: rpc_ok(ctx))
        try:
            assert adapter.count() == 3
            body = parse_body(captured[0])
            assert body["method"] == "retrieve"
            assert body["params"]["top_k"] == 1000
            assert body["params"]["aggregate"] is False
        finally:
            adapter.close()

    def test_count_zero_on_failure(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_err())
        try:
            assert adapter.count() == 0
        finally:
            adapter.close()

    def test_delete_scope_noop(self, env_snap):
        adapter, captured = make_adapter(lambda r: rpc_ok("x"))
        try:
            assert adapter.delete_scope("sess-A") == 0
            # 不应发任何请求
            assert captured == []
        finally:
            adapter.close()

    def test_auto_forget_returns_dropped(self, env_snap):
        report = {
            "dropped": 5,
            "promoted": 1,
            "merged": 2,
            "summarized": 3,
            "reextracted": 0,
            "reconciled": 0,
        }
        adapter, captured = make_adapter(lambda r: rpc_ok(report))
        try:
            assert adapter.auto_forget() == 5
            body = parse_body(captured[0])
            assert body["method"] == "consolidate"
        finally:
            adapter.close()


# ── recall_relevant ─────────────────────────────────────────


class TestRecallRelevant:
    def test_formats_context_string(self, env_snap):
        ctx = {
            "blocks": [
                _block(score=0.8, turns_text="fact A", memory_type="Semantic"),
                _block(score=0.6, turns_text="fact B", memory_type="Episodic"),
            ],
            "total_tokens": 50,
        }
        adapter, _ = make_adapter(lambda r: rpc_ok(ctx))
        try:
            out = adapter.recall_relevant("query")
            assert "fact A" in out
            assert "fact B" in out
            assert "80%" in out
            assert "Semantic" in out
        finally:
            adapter.close()

    def test_empty_string_on_failure(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_err())
        try:
            assert adapter.recall_relevant("q") == ""
        finally:
            adapter.close()

    def test_empty_string_on_no_blocks(self, env_snap):
        adapter, _ = make_adapter(lambda r: rpc_ok({"blocks": [], "total_tokens": 0}))
        try:
            assert adapter.recall_relevant("q") == ""
        finally:
            adapter.close()


# ── store_summary (Compactor 兼容) ──────────────────────────


class TestStoreSummary:
    def test_store_summary_is_summary_flag(self, env_snap):
        adapter, captured = make_adapter(lambda r: rpc_ok(["sum-1"]))
        try:
            entry_id = adapter.store_summary("summary text", "sess-A", 10)
            assert entry_id == "sum-1"
            body = parse_body(captured[0])
            ix = body["params"]["interaction"]
            assert ix["metadata"]["is_summary"] is True
            assert ix["metadata"]["original_count"] == 10
            assert ix["metadata"]["type"] == "summary"
        finally:
            adapter.close()
