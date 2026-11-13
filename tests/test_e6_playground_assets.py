"""E6: asset version registry (immutable chain, idempotent checkout)
+ prompt playground replay/diff + RPCs."""

from __future__ import annotations

import json

import pytest

from agent_runtime.asset_registry import AssetRegistry
from agent_runtime.playground import Playground


class TestAssetRegistry:
    def test_put_bumps_version_chain(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        v1 = reg.put("prompt", "greet", "hello", created_by="tester")
        v2 = reg.put("prompt", "greet", "hello world", created_by="tester")
        assert (v1["version"], v2["version"]) == (1, 2)
        assert v1["content_hash"] != v2["content_hash"]
        # immutable: v1 unchanged after v2
        assert reg.checkout("prompt", "greet", 1)["content"] == "hello"

    def test_put_rejects_bad_kind(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        with pytest.raises(ValueError):
            reg.put("nope", "x", "y")

    def test_put_identical_content_idempotent(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        v1 = reg.put("skill", "deploy", "steps...")
        v2 = reg.put("skill", "deploy", "steps...")
        assert v1["version"] == v2["version"] == 1
        assert v2["unchanged"] is True

    def test_checkout_latest_default_and_idempotent(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        reg.put("agent_template", "writer", "v1 body")
        reg.put("agent_template", "writer", "v2 body")
        reg.put("agent_template", "writer", "v3 body")
        a = reg.checkout("agent_template", "writer")
        b = reg.checkout("agent_template", "writer")
        assert a == b
        assert a["content"] == "v3 body"
        pinned = reg.checkout("agent_template", "writer", 2)
        assert pinned["content"] == "v2 body"

    def test_checkout_missing_returns_none(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        assert reg.checkout("prompt", "ghost") is None
        assert reg.checkout("prompt", "ghost", 3) is None

    def test_verify_detects_tamper(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        reg.put("prompt", "p", "original")
        assert reg.verify("prompt", "p", 1) is True
        import sqlite3

        conn = sqlite3.connect(reg.db_path)
        conn.execute("UPDATE asset_versions SET content='tampered' WHERE version=1")
        conn.commit()
        conn.close()
        assert reg.verify("prompt", "p", 1) is False

    def test_list_filters(self, tmp_path):
        reg = AssetRegistry(db_path=tmp_path / "a.db")
        reg.put("prompt", "a", "1")
        reg.put("skill", "a", "2")
        rows = reg.list(kind="skill")
        assert len(rows) == 1 and rows[0]["kind"] == "skill"
        assert len(reg.list()) == 2


class FakeClient:
    model = "m"

    def __init__(self, output="replayed output"):
        self._output = output
        self.calls = []

    async def chat(self, model, messages, **kw):
        self.calls.append({"model": model, "messages": messages, **kw})

        class R:
            content = self._output
            usage = {"prompt_tokens": 3, "completion_tokens": 4}

        return R()


class TestPlayground:
    async def test_replay_injects_system_and_params(self):
        client = FakeClient()
        pg = Playground(client)
        out = await pg.replay(
            [{"role": "user", "content": "hi"}],
            system="be terse",
            params={"temperature": 0.2, "max_tokens": 128},
        )
        assert out["output"] == "replayed output"
        call = client.calls[0]
        assert call["messages"][0]["role"] == "system"
        assert call["temperature"] == 0.2
        assert call["max_tokens"] == 128

    async def test_replay_keeps_existing_system(self):
        client = FakeClient()
        pg = Playground(client)
        await pg.replay(
            [
                {"role": "system", "content": "old system"},
                {"role": "user", "content": "hi"},
            ],
            system="new system",
        )
        msgs = client.calls[0]["messages"]
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert msgs[0]["content"] == "new system"

    def test_diff_changed_and_unchanged(self):
        d = Playground.diff("same", "same")
        assert d["changed"] is False and d["unified"] == ""
        d = Playground.diff("line1\n", "line2\n")
        assert d["changed"] is True and "-line1" in d["unified"] and "+line2" in d["unified"]


class TestPlaygroundRpcs:
    @pytest.fixture
    async def daemon(self, tmp_path, monkeypatch):
        import os

        from agent_runtime.daemon_server import DaemonServer

        monkeypatch.setenv("FUSION_ENABLE_WS", "0")
        sock = f"/tmp/test-e6-{os.getpid()}.sock"
        d = DaemonServer(
            socket_path=sock,
            ws_port=0,
            cluster_port=0,
            http_port=0,
            store_path=str(tmp_path / "s.db"),
        )
        await d.start()
        d.mlx_client = FakeClient()
        yield d
        await d.stop()
        if os.path.exists(sock):
            os.unlink(sock)

    async def test_asset_roundtrip_rpcs(self, daemon):
        put = daemon._get_handler("asset.put")
        r = await put({"kind": "prompt", "name": "n1", "content": "c1", "created_by": "t"})
        assert r["version"] == 1
        co = daemon._get_handler("asset.checkout")
        r2 = await co({"kind": "prompt", "name": "n1"})
        assert r2["content"] == "c1"
        lst = daemon._get_handler("asset.list")
        r3 = await lst({"kind": "prompt"})
        assert len(r3["assets"]) == 1
        r4 = await put({"kind": "bad-kind", "name": "n", "content": "x"})
        assert "error" in r4

    async def test_replay_rpc(self, daemon):
        h = daemon._get_handler("playground.replay")
        r = await h({"messages": [{"role": "user", "content": "hi"}]})
        assert r["output"] == "replayed output"
        r2 = await h({"messages": []})
        assert "error" in r2

    async def test_from_trace_rpc(self, daemon, tmp_path, monkeypatch):
        from agent_runtime import eval_runner as er

        traces = tmp_path / "traces"
        traces.mkdir()
        (traces / "t1.json").write_text(
            json.dumps(
                {
                    "trace_id": "tr-1",
                    "messages": [
                        {"role": "user", "content": "question"},
                        {"role": "assistant", "content": "baseline answer"},
                    ],
                }
            )
        )

        class FakeWriter:
            output_dir = traces

            def list_trajectories(self, limit=50):
                return [{"trace_id": "tr-1", "file": "t1.json"}]

        monkeypatch.setattr(er, "get_trajectory_writer", lambda: FakeWriter())
        h = daemon._get_handler("playground.fromTrace")
        r = await h({"trace_id": "tr-1", "prompt": "edited question"})
        assert r["baseline_output"] == "baseline answer"
        assert r["output"] == "replayed output"
        assert r["diff"]["changed"] is True
        r2 = await h({"trace_id": "missing"})
        assert "error" in r2
