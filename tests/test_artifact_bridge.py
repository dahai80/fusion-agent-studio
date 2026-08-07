"""Tests for artifact_bridge (AS-1~7 remote RPC) and artifact dispatcher."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.artifact_bridge import ArtifactBridge
from agent_runtime.artifact_tools import ARTIFACT_SYSTEM_PROMPT, ArtifactManager
from agent_runtime.dispatchers.artifact import ArtifactDispatcher


class TestArtifactBridgeLocalFallback:
    def setup_method(self):
        self.mgr = ArtifactManager()
        self.bridge = ArtifactBridge(local_manager=self.mgr, remote_url="http://127.0.0.1:1")
        self.bridge._remote_available = False

    @pytest.mark.asyncio
    async def test_create_local_fallback(self):
        result = await self.bridge.create(
            name="test_doc",
            artifact_type="document",
            content="hello world",
            agent_id="agent1",
        )
        assert result["status"] == "ok"
        assert result["source"] == "local"

    @pytest.mark.asyncio
    async def test_load_local_fallback(self):
        create_result = self.mgr.create_artifact(
            name="doc1", artifact_type="document", content="content here", agent_id="a1"
        )
        aid = create_result["artifact_id"]
        result = await self.bridge.load(artifact_id=aid, preview_only=True)
        assert result["status"] == "ok"
        assert result["source"] == "local"

    @pytest.mark.asyncio
    async def test_patch_local_fallback(self):
        create_result = self.mgr.create_artifact(
            name="doc2", artifact_type="document", content="original", agent_id="a1"
        )
        aid = create_result["artifact_id"]
        result = await self.bridge.patch(
            artifact_id=aid, operation="append", content=" appended"
        )
        assert result["status"] == "ok"
        assert result["source"] == "local"

    @pytest.mark.asyncio
    async def test_list_all_local_fallback(self):
        self.mgr.create_artifact(
            name="d1", artifact_type="document", content="c", agent_id="a1"
        )
        result = await self.bridge.list_all(agent_id="a1")
        assert result["status"] == "ok"
        assert result["source"] == "local"

    @pytest.mark.asyncio
    async def test_context_budget_local_fallback(self):
        result = await self.bridge.context_budget(agent_id="a1")
        assert result["status"] == "ok"
        assert result["source"] == "local"

    @pytest.mark.asyncio
    async def test_snapshot_no_remote(self):
        result = await self.bridge.snapshot(artifact_id="nonexistent")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_auto_compact_no_remote(self):
        result = await self.bridge.auto_compact(artifact_id="nonexistent")
        assert result["status"] == "error"


class TestArtifactBridgeRemoteRPC:
    def setup_method(self):
        self.mgr = ArtifactManager()
        self.bridge = ArtifactBridge(local_manager=self.mgr, remote_url="http://127.0.0.1:11451")
        self.bridge._remote_available = True

    @pytest.mark.asyncio
    async def test_create_remote_success(self):
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"artifact": {"id": "art_1"}, "version": {"num": 1}}
            result = await self.bridge.create(
                name="rdoc", artifact_type="code", content="x=1", agent_id="a1"
            )
            assert result["status"] == "ok"
            assert result["source"] == "remote"
            mock_rpc.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_remote_success(self):
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"content": "full text", "token_count": 2}
            result = await self.bridge.load(artifact_id="art_1", preview_only=False)
            assert result["status"] == "ok"
            assert result["source"] == "remote"

    @pytest.mark.asyncio
    async def test_patch_remote_success(self):
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"version": {"num": 2}, "patch_info": {}}
            result = await self.bridge.patch(
                artifact_id="art_1", operation="append", content=" more"
            )
            assert result["status"] == "ok"
            assert result["source"] == "remote"

    @pytest.mark.asyncio
    async def test_patch_remote_maps_operations(self):
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"version": {"num": 2}, "patch_info": {}}
            await self.bridge.patch(
                artifact_id="art_1", operation="section_replace", content="new", section="intro"
            )
            call_params = mock_rpc.call_args[0][1]
            assert call_params["operation"] == "replace_section"
            assert call_params["anchor"] == "intro"

    @pytest.mark.asyncio
    async def test_context_budget_remote_success(self):
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.return_value = {"total_tokens": 1000, "by_type": {"code": 500}}
            result = await self.bridge.context_budget(agent_id="a1", context_window=32768)
            assert result["status"] == "ok"
            assert result["source"] == "remote"

    @pytest.mark.asyncio
    async def test_remote_failure_falls_back(self):
        self.bridge._remote_available = True
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.side_effect = RuntimeError("connection refused")
            result = await self.bridge.create(
                name="fbdoc", artifact_type="document", content="fb", agent_id="a1"
            )
            assert result["source"] == "local"

    @pytest.mark.asyncio
    async def test_check_remote_failure(self):
        with patch.object(self.bridge, "_rpc", new_callable=AsyncMock) as mock_rpc:
            mock_rpc.side_effect = RuntimeError("no connection")
            available = await self.bridge.check_remote()
            assert available is False


class TestArtifactBridgePassThrough:
    def setup_method(self):
        self.mgr = ArtifactManager()
        self.bridge = ArtifactBridge(local_manager=self.mgr)

    def test_get_active_artifacts_context(self):
        self.mgr.create_artifact(
            name="d1", artifact_type="document", content="content", agent_id="a1"
        )
        ctx = self.bridge.get_active_artifacts_context("a1")
        assert "d1" in ctx

    def test_get_active_artifacts_context_budget_aware(self):
        self.mgr.create_artifact(
            name="d1", artifact_type="document", content="content", agent_id="a1"
        )
        result = self.bridge.get_active_artifacts_context_budget_aware("a1", context_window=32768)
        assert result["mode"] in ("full", "preview", "blocked", "none")


class TestArtifactDispatcher:
    def setup_method(self):
        self.daemon = MagicMock()
        self.bridge = ArtifactBridge(local_manager=ArtifactManager())
        self.daemon._get_artifact_manager.return_value = self.bridge
        self.dispatcher = ArtifactDispatcher(self.daemon)

    def test_get_handlers(self):
        handlers = self.dispatcher.get_handlers()
        assert "artifact.create" in handlers
        assert "artifact.load" in handlers
        assert "artifact.patch" in handlers
        assert "artifact.list_all" in handlers
        assert "artifact.snapshot" in handlers
        assert "artifact.context_budget" in handlers
        assert "artifact.auto_compact" in handlers
        assert "artifact.ping_remote" in handlers
        assert "artifact.advance_phase" in handlers
        assert len(handlers) == 9

    @pytest.mark.asyncio
    async def test_handle_create(self):
        self.bridge._remote_available = False
        result = await self.dispatcher._handle_create({
            "name": "test", "type": "document", "content": "hello"
        })
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_handle_load(self):
        create_result = self.bridge.local.create_artifact(
            name="d", artifact_type="document", content="text", agent_id="a"
        )
        result = await self.dispatcher._handle_load({
            "artifact_id": create_result["artifact_id"], "preview_only": True
        })
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_handle_patch(self):
        create_result = self.bridge.local.create_artifact(
            name="d", artifact_type="document", content="orig", agent_id="a"
        )
        result = await self.dispatcher._handle_patch({
            "artifact_id": create_result["artifact_id"],
            "operation": "append",
            "content": " more",
        })
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_handle_context_budget(self):
        result = await self.dispatcher._handle_context_budget({"session_id": "a1"})
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_handle_ping_remote(self):
        with patch.object(self.bridge, "check_remote", new_callable=AsyncMock) as mock:
            mock.return_value = True
            result = await self.dispatcher._handle_ping_remote({})
            assert result["remote_available"] is True


class TestAS8ArtifactSystemPrompt:
    def test_artifact_system_prompt_exists(self):
        assert len(ARTIFACT_SYSTEM_PROMPT) > 100
        assert "patch_artifact" in ARTIFACT_SYSTEM_PROMPT
        assert "artifact_load" in ARTIFACT_SYSTEM_PROMPT
        assert "artifact-ref" in ARTIFACT_SYSTEM_PROMPT

    def test_artifact_long_text_template(self):
        from agent_runtime.prompt_templates import (
            PromptTemplateManager,
            register_default_prompt_templates,
        )
        mgr = PromptTemplateManager()
        register_default_prompt_templates(mgr)
        rendered = mgr.render(
            "artifact-long-text",
            artifact_guidelines=ARTIFACT_SYSTEM_PROMPT,
            artifact_count=3,
            artifact_list="- doc1 (document)\n- code1 (code)",
        )
        assert "Artifact Long-Text Guidelines" in rendered
        assert "3 active artifact" in rendered
        assert "doc1" in rendered
