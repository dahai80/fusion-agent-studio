import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.daemon_server import DaemonServer


@pytest.fixture
def daemon():
    ds = DaemonServer.__new__(DaemonServer)
    ds._mlx_process = None
    ds._kb_manager = None
    ds._audit_logger = None
    ds._version_store = None
    ds._offline_mode = False
    ds._cowork_manager = None
    ds._langgraph_engine = None
    ds._artifact_manager = None
    ds._agents = {}
    ds._gateway = None
    ds._runtime = None
    ds._chat_engine = None
    ds._apikey_mgr = None
    ds._style_mgr = None
    ds._workflow_engine = None
    ds._session_manager = None
    ds._telemetry_engine = None
    ds._status_tracker = None
    ds._fmp = None
    ds._swarm = None
    ds._plaza = None
    ds._orchestrator = None
    ds._marketplace = None
    ds._hooks_engine = None
    ds._rate_limiter = None
    ds._store = None
    ds._connector_mgr = None
    ds._safety = None
    ds._rag = None
    ds._memory = None
    ds._planner = None
    ds._active_executions = {}
    ds._code_tasks = {}
    ds._running = False
    ds._sub_dispatchers = ds._init_sub_dispatchers()
    return ds


class TestModelStatus:
    @pytest.mark.asyncio
    async def test_model_status_not_running(self, daemon):
        result = await daemon._handle_model_status({})
        assert result["connected"] is False
        assert result["models"] == []
        assert result["loaded"] == []
        assert "url" in result

    @pytest.mark.asyncio
    async def test_model_status_running_but_unhealthy(self, daemon):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        daemon._mlx_process = mock_proc
        with (
            patch.object(
                daemon, "_check_mlx_health", new_callable=AsyncMock, return_value=False
            ),
            patch.object(
                daemon, "_list_mlx_models", new_callable=AsyncMock, return_value=[]
            ),
        ):
            result = await daemon._handle_model_status({})
        assert result["connected"] is False
        assert result["models"] == []

    @pytest.mark.asyncio
    async def test_model_status_running_healthy(self, daemon):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        daemon._mlx_process = mock_proc
        fake_models = [{"name": "test-model", "loaded": True}]
        with (
            patch.object(
                daemon, "_check_mlx_health", new_callable=AsyncMock, return_value=True
            ),
            patch.object(
                daemon,
                "_list_mlx_models",
                new_callable=AsyncMock,
                return_value=fake_models,
            ),
        ):
            result = await daemon._handle_model_status({})
        assert result["connected"] is True
        assert len(result["models"]) == 1
        assert len(result["loaded"]) == 1


class TestKbBuild:
    @pytest.mark.asyncio
    async def test_kb_build_missing_path(self, daemon):
        result = await daemon._handle_kb_build({})
        assert result["status"] == "error"
        assert "path" in result["message"]

    @pytest.mark.asyncio
    async def test_kb_build_path_not_found(self, daemon):
        result = await daemon._handle_kb_build({"path": "/nonexistent/path"})
        assert result["status"] == "error"
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    async def test_kb_build_success(self, daemon):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, "readme.md")
            with open(test_file, "w") as f:
                f.write("# Test KB\nHello world")
            mock_mgr = MagicMock()
            mock_kb = MagicMock()
            mock_kb.kb_id = "kb-test123"
            mock_mgr.create_kb.return_value = mock_kb
            mock_mgr.add_file.return_value = MagicMock()
            daemon._kb_manager = mock_mgr
            result = await daemon._handle_kb_build({"path": tmpdir})
        assert result["status"] == "built"
        assert "kb_id" in result
        assert result["file_count"] >= 1


class TestKbStatus:
    @pytest.mark.asyncio
    async def test_kb_status_specific_kb_not_found(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.get_kb.return_value = None
        daemon._kb_manager = mock_mgr
        result = await daemon._handle_kb_status({"kb_id": "nonexistent"})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_kb_status_specific_kb_found(self, daemon):
        mock_mgr = MagicMock()
        mock_kb = MagicMock()
        mock_kb.to_dict.return_value = {"kb_id": "kb-1", "name": "test"}
        mock_mgr.get_kb.return_value = mock_kb
        mock_mgr.list_files.return_value = [MagicMock()]
        daemon._kb_manager = mock_mgr
        result = await daemon._handle_kb_status({"kb_id": "kb-1"})
        assert len(result["kbs"]) == 1
        assert result["file_count"] == 1

    @pytest.mark.asyncio
    async def test_kb_status_all_kbs(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.list_kbs.return_value = {"data": []}
        daemon._kb_manager = mock_mgr
        result = await daemon._handle_kb_status({})
        assert "kbs" in result
        assert result["building"] is False


class TestKbQuery:
    @pytest.mark.asyncio
    async def test_kb_query_missing_query(self, daemon):
        result = await daemon._handle_kb_query({})
        assert result["status"] == "error"
        assert "query" in result["message"]

    @pytest.mark.asyncio
    async def test_kb_query_with_kb_id(self, daemon):
        # 审计 P2/dim1: kb.query 现路由真实 mgr.search (async), 非 list_files 假相关.
        # mock 须 AsyncMock 可 await, 返回 search 结果格式.
        mock_mgr = MagicMock()
        mock_mgr.search = AsyncMock(
            return_value={"results": [{"file_id": "f1", "score": 0.9}], "count": 1}
        )
        daemon._kb_manager = mock_mgr
        result = await daemon._handle_kb_query(
            {"query": "test", "kb_id": "kb-1", "limit": 5}
        )
        assert len(result["results"]) == 1
        assert result["results"][0]["score"] == 0.9


class TestAuditList:
    @pytest.mark.asyncio
    async def test_audit_list_default(self, daemon):
        mock_logger = MagicMock()
        mock_logger.query_logs.return_value = {"data": [], "total": 0}
        daemon._audit_logger = mock_logger
        result = await daemon._handle_audit_list({})
        assert "data" in result

    @pytest.mark.asyncio
    async def test_audit_list_with_filters(self, daemon):
        mock_logger = MagicMock()
        mock_logger.query_logs.return_value = {"data": [{"action": "read"}], "total": 1}
        daemon._audit_logger = mock_logger
        await daemon._handle_audit_list({"tool": "file_read", "limit": 10})
        mock_logger.query_logs.assert_called_once()
        call_kwargs = mock_logger.query_logs.call_args[1]
        assert call_kwargs["tool"] == "file_read"
        assert call_kwargs["limit"] == 10


class TestSystemOffline:
    @pytest.mark.asyncio
    async def test_offline_status_default(self, daemon):
        result = await daemon._handle_system_offline_status({})
        assert result["offline"] is False
        assert result["reason"] is None

    @pytest.mark.asyncio
    async def test_offline_status_env_set(self, daemon):
        with patch.dict(os.environ, {"FUSION_CODE_OFFLINE": "1"}):
            result = await daemon._handle_system_offline_status({})
        assert result["offline"] is True
        assert "FUSION_CODE_OFFLINE" in (result["reason"] or "")

    @pytest.mark.asyncio
    async def test_set_offline_enable(self, daemon):
        result = await daemon._handle_system_set_offline({"enabled": True})
        assert result["offline"] is True
        assert daemon._offline_mode is True

    @pytest.mark.asyncio
    async def test_set_offline_disable(self, daemon):
        daemon._offline_mode = True
        result = await daemon._handle_system_set_offline({"enabled": False})
        assert result["offline"] is False
        assert daemon._offline_mode is False

    @pytest.mark.asyncio
    async def test_offline_status_manual(self, daemon):
        daemon._offline_mode = True
        result = await daemon._handle_system_offline_status({})
        assert result["offline"] is True
        assert "Manually" in (result["reason"] or "")


class TestAgentDiffReview:
    @pytest.mark.asyncio
    async def test_diff_review_missing_agent_id(self, daemon):
        result = await daemon._handle_agent_diff_review({})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_diff_review_agent_not_found(self, daemon):
        with patch.object(daemon, "_agent_dir", return_value=Path("/nonexistent")):
            result = await daemon._handle_agent_diff_review(
                {"agent_id": "no-such-agent"}
            )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_diff_review_success(self, daemon):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir)
            pkg_dir = agent_dir / ".fusion-agent"
            pkg_dir.mkdir()
            (pkg_dir / "manifest.json").write_text(json.dumps({"name": "test-agent"}))
            mock_vs = MagicMock()
            v1 = MagicMock()
            v1.to_dict.return_value = {
                "version_id": "v1",
                "label": "init",
                "created_at": "2025-01-01",
                "snapshot_data": {"tools": ["read"]},
            }
            mock_vs.list_versions.return_value = [v1]
            daemon._version_store = mock_vs
            with patch.object(daemon, "_agent_dir", return_value=agent_dir):
                result = await daemon._handle_agent_diff_review(
                    {"agent_id": "test-agent"}
                )
        assert len(result["entries"]) == 1
        assert "# Diff Review" in result["markdown"]


class TestPermissionList:
    @pytest.mark.asyncio
    async def test_permission_list_specific_agent(self, daemon):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir)
            pkg_dir = agent_dir / ".fusion-agent"
            pkg_dir.mkdir()
            (pkg_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "tools": ["file_read"],
                        "knowledge_base_ids": [],
                        "web_search_enabled": False,
                    }
                )
            )
            (agent_dir / "definition.json").write_text(
                json.dumps({"denied_tools": ["bash"], "permissions": {}})
            )
            with patch.object(daemon, "_agent_dir", return_value=agent_dir):
                result = await daemon._handle_permission_list(
                    {"agent_id": "test-agent"}
                )
        assert "permissions" in result
        assert "denied_tools" in result
        assert "bash" in result["denied_tools"]

    @pytest.mark.asyncio
    async def test_permission_list_all_agents(self, daemon):
        _result = await daemon._handle_permission_list({})
        assert "permissions" in _result


class TestPermissionUpdate:
    @pytest.mark.asyncio
    async def test_permission_update_missing_agent_id(self, daemon):
        result = await daemon._handle_permission_update({})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_permission_update_deny_tool(self, daemon):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir)
            pkg_dir = agent_dir / ".fusion-agent"
            pkg_dir.mkdir()
            (pkg_dir / "manifest.json").write_text(json.dumps({"name": "test"}))
            (agent_dir / "definition.json").write_text(json.dumps({"denied_tools": []}))
            with patch.object(daemon, "_agent_dir", return_value=agent_dir):
                result = await daemon._handle_permission_update(
                    {"agent_id": "test", "tool": "bash", "level": "deny"}
                )
        assert result["ok"] is True
        assert "bash" in result["denied_tools"]

    @pytest.mark.asyncio
    async def test_permission_update_allow_tool(self, daemon):
        with tempfile.TemporaryDirectory() as tmpdir:
            agent_dir = Path(tmpdir)
            pkg_dir = agent_dir / ".fusion-agent"
            pkg_dir.mkdir()
            (pkg_dir / "manifest.json").write_text(json.dumps({"name": "test"}))
            (agent_dir / "definition.json").write_text(
                json.dumps({"denied_tools": ["bash"]})
            )
            with patch.object(daemon, "_agent_dir", return_value=agent_dir):
                result = await daemon._handle_permission_update(
                    {"agent_id": "test", "tool": "bash", "level": "allow"}
                )
        assert result["ok"] is True
        assert "bash" not in result["denied_tools"]


class TestDispatchTable:
    def test_all_new_methods_in_dispatch(self):
        ds = DaemonServer.__new__(DaemonServer)
        ds.__init__()
        required = [
            "model.status",
            "kb.build",
            "kb.status",
            "kb.query",
            "audit.list",
            "system.offline_status",
            "system.set_offline",
            "agent.diff_review",
            "permission.list",
            "permission.update",
            "kb.search",
            "kb.ask",
            "kb.scan",
            "kb.health",
        ]
        for method in required:
            handler = ds._get_handler(method)
            assert handler is not None, f"Missing dispatch entry: {method}"
            assert callable(handler), f"Handler not callable: {method}"


class TestKbSearch:
    @pytest.mark.asyncio
    async def test_kb_search_missing_params(self, daemon):
        result = await daemon._handle_kb_search({})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_kb_search_rag_unavailable(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.search = AsyncMock(
            return_value={
                "results": [],
                "kb_id": "kb1",
                "query": "test",
                "rag_available": False,
            }
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_search({"kb_id": "kb1", "query": "test"})
        assert result["rag_available"] is False

    @pytest.mark.asyncio
    async def test_kb_search_with_results(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.search = AsyncMock(
            return_value={
                "results": [{"content": "hello", "score": 0.9}],
                "kb_id": "kb1",
                "query": "test",
                "rag_available": True,
                "count": 1,
            }
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_search(
                {
                    "kb_id": "kb1",
                    "query": "test",
                    "hybrid": True,
                    "rerank": True,
                }
            )
        assert result["count"] == 1
        mock_mgr.search.assert_called_once_with(
            kb_id="kb1",
            query="test",
            hybrid=True,
            rerank=True,
        )


class TestKbAsk:
    @pytest.mark.asyncio
    async def test_kb_ask_missing_params(self, daemon):
        result = await daemon._handle_kb_ask({})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_kb_ask_rag_unavailable(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.ask = AsyncMock(
            return_value={
                "answer": "",
                "kb_id": "kb1",
                "question": "q",
                "rag_available": False,
            }
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_ask({"kb_id": "kb1", "question": "q"})
        assert result["rag_available"] is False

    @pytest.mark.asyncio
    async def test_kb_ask_with_answer(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.ask = AsyncMock(
            return_value={
                "answer": "42",
                "sources": [],
                "confidence": 0.9,
                "kb_id": "kb1",
                "question": "q",
                "rag_available": True,
            }
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_ask({"kb_id": "kb1", "question": "q"})
        assert result["answer"] == "42"


class TestKbScan:
    @pytest.mark.asyncio
    async def test_kb_scan_missing_params(self, daemon):
        result = await daemon._handle_kb_scan({})
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_kb_scan_rag_unavailable(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.scan_directory = AsyncMock(
            return_value={
                "kb_id": "kb1",
                "path": "/data",
                "rag_available": False,
            }
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_scan({"kb_id": "kb1", "path": "/data"})
        assert result["rag_available"] is False

    @pytest.mark.asyncio
    async def test_kb_scan_with_results(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.scan_directory = AsyncMock(
            return_value={
                "kb_id": "kb1",
                "path": "/data",
                "rag_available": True,
                "scanned": 10,
            }
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_scan(
                {
                    "kb_id": "kb1",
                    "path": "/data",
                    "recursive": True,
                }
            )
        assert result["scanned"] == 10


class TestKbHealth:
    @pytest.mark.asyncio
    async def test_kb_health_rag_available(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.is_rag_available = AsyncMock(return_value=True)
        mock_mgr.rag_status = AsyncMock(
            return_value={"available": True, "version": "0.1.0"}
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_health({})
        assert result["rag_available"] is True

    @pytest.mark.asyncio
    async def test_kb_health_rag_unavailable(self, daemon):
        mock_mgr = MagicMock()
        mock_mgr.is_rag_available = AsyncMock(return_value=False)
        mock_mgr.rag_status = AsyncMock(
            return_value={"available": False, "error": "connection refused"}
        )
        with patch.object(daemon, "_get_kb_manager", return_value=mock_mgr):
            result = await daemon._handle_kb_health({})
        assert result["rag_available"] is False
