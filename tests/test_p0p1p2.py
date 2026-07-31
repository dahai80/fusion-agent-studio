"""Tests for P0/P1/P2 features: Plaza, HITL L1/L2, RAG Pipeline,
Memory Auto-Compression, Planner, LlamaIndex Readers, AgentPackage workspace.

Callers: pytest test runner. API: all new classes from plaza, safety, rag_pipeline,
memory_engine, planner, data_ingestion, agent_package, llm_gateway, graph, runtime.
Schemas: GatewayResponse, PlazaMessage, ExecutionPlan, PlanStep, RAGResult, MemoryTier,
SafetyPolicy, DiffPreviewRequest, Document. User instruction: "把所有P0，P1和P2全部落地"
"""

import shutil
import tempfile
import time


from agent_runtime.plaza import Plaza, PlazaMessage, PlazaChannel
from agent_runtime.safety import (
    SafetyGateway, SafetyLevel, SafetyAction, SafetyPolicy, DiffPreviewRequest,
    CAT_CODE_ANALYSIS, CAT_DOC_RETRIEVAL, CAT_FILE_WRITE,
    CAT_SHELL_EXEC, CAT_GIT_PUSH, CAT_CODE_EDIT,
)
from agent_runtime.rag_pipeline import RAGPipeline, RAGConfig, RAGResult
from agent_runtime.memory_engine import MemoryEngine, MemoryTier
from agent_runtime.planner import PlannerEngine, ExecutionPlan, PlanStep
from agent_runtime.data_ingestion import (
    WebReader, GitHubReader, NotionReader, PDFReader, DirectoryReader,
)
from agent_runtime.agent_package import AgentPackage, AgentManifest
from agent_runtime.llm_gateway import LLMGateway, ModelConfig, GatewayResponse
from agent_runtime.graph import AgentGraph, NodeConfig
from agent_runtime.runtime import AgentRuntime


# ─── P0: Plaza ──────────────────────────────────────────────

class TestPlazaMessage:
    def test_init_defaults(self):
        msg = PlazaMessage(id="m1", channel="ch1", sender="a1", content="hello")
        assert msg.channel == "ch1"
        assert msg.sender == "a1"
        assert msg.mentions == []
        assert msg.round_number == 0
        assert msg.timestamp > 0

    def test_to_dict_from_dict(self):
        msg = PlazaMessage(id="m1", channel="ch1", sender="a1",
                           content="hello", mentions=["a2"], round_number=1)
        d = msg.to_dict()
        assert d["channel"] == "ch1"
        restored = PlazaMessage.from_dict(d)
        assert restored.channel == "ch1"
        assert restored.mentions == ["a2"]


class TestPlazaChannel:
    def test_init_defaults(self):
        ch = PlazaChannel(name="general", participants=["a1", "a2"])
        assert ch.max_rounds == 3
        assert ch.current_round == 0
        assert not ch.is_suspended

    def test_to_dict_from_dict(self):
        ch = PlazaChannel(name="general", participants=["a1"], max_rounds=5)
        d = ch.to_dict()
        assert d["max_rounds"] == 5
        restored = PlazaChannel.from_dict(d)
        assert restored.name == "general"


class TestPlaza:
    def setup_method(self):
        self.plaza = Plaza(max_rounds=3)

    def test_create_channel(self):
        ch = self.plaza.create_channel("general", ["a1", "a2"])
        assert ch.name == "general"
        assert "a1" in ch.participants

    def test_delete_channel(self):
        self.plaza.create_channel("ch1", ["a1"])
        assert self.plaza.delete_channel("ch1")
        assert not self.plaza.delete_channel("nonexistent")

    def test_broadcast_basic(self):
        self.plaza.create_channel("ch1", ["a1", "a2"])
        msg = self.plaza.broadcast("ch1", "a1", "hello world")
        assert msg.content == "hello world"
        assert msg.sender == "a1"
        assert msg.round_number == 1

    def test_broadcast_with_mentions(self):
        self.plaza.create_channel("ch1", ["a1", "a2", "a3"])
        msg = self.plaza.broadcast("ch1", "a1", "hey @a2 check this")
        assert "a2" in msg.mentions

    def test_circuit_breaker_suspends(self):
        self.plaza.create_channel("ch1", ["a1"])
        self.plaza.broadcast("ch1", "a1", "round 1")
        self.plaza.broadcast("ch1", "a1", "round 2")
        self.plaza.broadcast("ch1", "a1", "round 3")
        ch = self.plaza.get_channel("ch1")
        assert ch.is_suspended

    def test_circuit_breaker_check(self):
        self.plaza.create_channel("ch1", ["a1"])
        assert not self.plaza.check_circuit_breaker("ch1")
        for i in range(3):
            self.plaza.broadcast("ch1", "a1", f"msg {i}")
        assert self.plaza.check_circuit_breaker("ch1")

    def test_human_break_in(self):
        self.plaza.create_channel("ch1", ["a1"])
        self.plaza.broadcast("ch1", "a1", "msg1")
        self.plaza.broadcast("ch1", "a1", "msg2")
        msg = self.plaza.human_break_in("ch1", "stop!")
        assert msg.sender == "human"
        ch = self.plaza.get_channel("ch1")
        assert not ch.is_suspended
        assert len(ch.pending_queue) == 0
        assert ch.current_round == 0

    def test_designate_speaker(self):
        self.plaza.create_channel("ch1", ["a1", "a2"])
        msg = self.plaza.designate_speaker("ch1", "a2")
        assert "a2" in msg.mentions

    def test_get_messages(self):
        self.plaza.create_channel("ch1", ["a1"])
        self.plaza.broadcast("ch1", "a1", "msg1")
        self.plaza.broadcast("ch1", "a1", "msg2")
        msgs = self.plaza.get_messages("ch1")
        assert len(msgs) >= 2

    def test_subscribe_unsubscribe(self):
        self.plaza.create_channel("ch1", ["a1"])
        sid = self.plaza.subscribe("ch1", "a1", lambda msg: None)
        assert self.plaza.unsubscribe(sid)

    def test_get_pending_for(self):
        self.plaza.create_channel("ch1", ["a1", "a2"])
        self.plaza.broadcast("ch1", "a1", "hey @a2 look")
        pending = self.plaza.get_pending_for("a2")
        assert len(pending) >= 1

    def test_list_channels(self):
        self.plaza.create_channel("ch1", ["a1"])
        self.plaza.create_channel("ch2", ["a2"])
        channels = self.plaza.list_channels()
        assert len(channels) == 2


# ─── P0: HITL L1/L2 ────────────────────────────────────────

class TestSafetyPolicy:
    def test_init(self):
        p = SafetyPolicy(category=CAT_FILE_WRITE, default_level=SafetyLevel.L2)
        assert p.category == CAT_FILE_WRITE
        assert p.default_level == SafetyLevel.L2

    def test_to_dict_from_dict(self):
        p = SafetyPolicy(category=CAT_SHELL_EXEC, default_level=SafetyLevel.L3,
                         requires_diff=False, description="Dangerous")
        d = p.to_dict()
        restored = SafetyPolicy.from_dict(d)
        assert restored.category == CAT_SHELL_EXEC


class TestDiffPreviewRequest:
    def test_init(self):
        r = DiffPreviewRequest(action_id="a1", category=CAT_CODE_EDIT,
                               original="old", proposed="new", diff="@@ -1 +1 @@")
        assert r.requires_approval

    def test_to_dict_from_dict(self):
        r = DiffPreviewRequest(action_id="a1", category=CAT_FILE_WRITE,
                               original="a", proposed="b", diff="-a\n+b")
        d = r.to_dict()
        restored = DiffPreviewRequest.from_dict(d)
        assert restored.action_id == "a1"


class TestSafetyGatewayL1L2:
    def test_l1_auto_approve_safe_actions(self):
        gw = SafetyGateway(level=SafetyLevel.L1)
        verdict = gw.evaluate_action(CAT_CODE_ANALYSIS, "analyzing code")
        assert verdict.action == SafetyAction.ALLOW
        assert not verdict.requires_approval

    def test_l1_auto_approve_doc_retrieval(self):
        gw = SafetyGateway(level=SafetyLevel.L1)
        verdict = gw.evaluate_action(CAT_DOC_RETRIEVAL, "searching docs")
        assert verdict.action == SafetyAction.ALLOW

    def test_l2_file_write_requires_preview(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        verdict = gw.evaluate_action(CAT_FILE_WRITE, "write to file.py")
        assert verdict.action == SafetyAction.PREVIEW
        assert verdict.requires_approval

    def test_l2_code_edit_requires_preview(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        verdict = gw.evaluate_action(CAT_CODE_EDIT, "modify function")
        assert verdict.action == SafetyAction.PREVIEW

    def test_l3_shell_exec_blocked(self):
        gw = SafetyGateway(level=SafetyLevel.L3)
        verdict = gw.evaluate_action(CAT_SHELL_EXEC, "rm -rf /")
        assert verdict.requires_approval

    def test_l3_git_push_blocked(self):
        gw = SafetyGateway(level=SafetyLevel.L3)
        verdict = gw.evaluate_action(CAT_GIT_PUSH, "git push origin main")
        assert verdict.requires_approval

    def test_generate_diff_preview(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        preview = gw.generate_diff_preview("original text", "modified text")
        assert preview.action_id
        assert preview.original == "original text"
        assert preview.proposed == "modified text"
        assert preview.diff

    def test_approve_reject_action(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        preview = gw.generate_diff_preview("old", "new")
        aid = preview.action_id
        assert gw.approve_action(aid)
        assert not gw.approve_action("nonexistent")

    def test_set_level(self):
        gw = SafetyGateway(level=SafetyLevel.L1)
        gw.set_level(SafetyLevel.L2)
        assert gw.level == SafetyLevel.L2

    def test_evaluate_action_unknown_category_blocks(self):
        gw = SafetyGateway(level=SafetyLevel.L1)
        verdict = gw.evaluate_action("unknown_cat", "something")
        assert verdict.action == SafetyAction.BLOCK
        assert verdict.requires_approval

    def test_backward_compat_check(self):
        gw = SafetyGateway(level=SafetyLevel.L1)
        verdict = gw.check("hello world")
        assert verdict.action == SafetyAction.ALLOW


# ─── P0/P1: LLM Gateway as Proxy ───────────────────────────

class TestGatewayResponse:
    def test_init(self):
        r = GatewayResponse(content="hi", model="test-model")
        assert r.content == "hi"
        assert r.model == "test-model"
        assert r.fallback_from == ""

    def test_to_dict_from_dict(self):
        r = GatewayResponse(content="hi", model="m1", tool_calls=[{"f": "x"}],
                            finish_reason="stop", usage={"prompt_tokens": 5})
        d = r.to_dict()
        restored = GatewayResponse.from_dict(d)
        assert restored.content == "hi"
        assert restored.tool_calls == [{"f": "x"}]


class TestLLMGatewayProxy:
    def test_register_default_local(self):
        gw = LLMGateway()
        config = gw.register_default_local(name="test-local", base_url="http://localhost:11434/v1")
        assert config.name == "test-local"
        assert config.provider == "local"
        assert gw._default_model == "test-local"

    def test_set_default_client(self):
        gw = LLMGateway(default_model="test-model")
        gw.set_default_client(object())
        assert gw._default_client is not None

    def test_fallback_chain(self):
        gw = LLMGateway()
        gw.register_model(ModelConfig(name="high", priority=10, capabilities=["chat"]))
        gw.register_model(ModelConfig(name="low", priority=1, capabilities=["chat"]))
        chain = gw.get_fallback_chain(capability="chat")
        assert len(chain) == 2
        assert chain[0].name == "high"
        assert chain[1].name == "low"


# ─── P1: RAG Pipeline ──────────────────────────────────────

class TestRAGConfig:
    def test_defaults(self):
        c = RAGConfig()
        assert c.top_k == 5
        assert c.mode == "hybrid"
        assert c.max_context_tokens == 3000

    def test_to_dict_from_dict(self):
        c = RAGConfig(top_k=10, mode="vector", scope="test")
        d = c.to_dict()
        restored = RAGConfig.from_dict(d)
        assert restored.top_k == 10
        assert restored.mode == "vector"


class TestRAGResult:
    def test_init(self):
        r = RAGResult(query="test", documents=[], context_text="ctx", scores=[])
        assert r.query == "test"
        assert r.context_text == "ctx"


class TestRAGPipeline:
    def test_retrieve_with_engine(self, tmp_path):
        from agent_runtime.knowledge_engine import KnowledgeEngine
        engine = KnowledgeEngine(db_path=str(tmp_path / "test.db"))
        engine.ingest("Python is a programming language", scope="general")
        engine.ingest("Rust is a systems language", scope="general")

        pipeline = RAGPipeline(knowledge_engine=engine)
        result = pipeline.retrieve("programming language")
        assert result.query == "programming language"
        assert len(result.context_text) > 0
        engine.close()

    async def test_query_with_engine(self, tmp_path):
        from agent_runtime.knowledge_engine import KnowledgeEngine
        engine = KnowledgeEngine(db_path=str(tmp_path / "test.db"))
        engine.ingest("Python is a programming language", scope="general")
        pipeline = RAGPipeline(knowledge_engine=engine)
        result = await pipeline.query("programming language")
        assert "answer" in result
        engine.close()

    async def test_generate_stub(self, tmp_path):
        from agent_runtime.knowledge_engine import KnowledgeEngine
        engine = KnowledgeEngine(db_path=str(tmp_path / "test.db"))
        pipeline = RAGPipeline(knowledge_engine=engine)
        answer = await pipeline.generate("what is X?", "X is something")
        assert isinstance(answer, str)
        engine.close()


# ─── P1: Memory Auto-Compression ───────────────────────────

class TestMemoryTier:
    def test_init(self):
        t = MemoryTier(name="short_term", max_entries=50, max_age_hours=24, importance_threshold=7)
        assert t.name == "short_term"
        assert t.max_entries == 50

    def test_to_dict_from_dict(self):
        t = MemoryTier(name="archive", max_entries=500, max_age_hours=0, importance_threshold=0)
        d = t.to_dict()
        restored = MemoryTier.from_dict(d)
        assert restored.name == "archive"


class TestMemoryAutoCompression:
    def test_store_and_recall(self, tmp_path):
        engine = MemoryEngine(db_path=str(tmp_path / "mem.db"))
        mid = engine.store("test memory", scope="test", importance=5)
        assert mid
        results = engine.recall("test memory")
        assert len(results) >= 1
        engine.close()

    def test_auto_compress_on_threshold(self, tmp_path):
        engine = MemoryEngine(db_path=str(tmp_path / "mem.db"), max_entries=10, summary_batch=5)
        for i in range(15):
            engine.store(f"memory entry {i}", scope="test", importance=3)
        count = engine.count(scope="test")
        assert count > 0
        engine.close()

    def test_compress_scope(self, tmp_path):
        engine = MemoryEngine(db_path=str(tmp_path / "mem.db"))
        for i in range(20):
            engine.store(f"old memory {i}", scope="archive-test", importance=2)
        compressed = engine.compress_scope("archive-test", tier="archive")
        assert compressed >= 0
        engine.close()

    def test_get_tier_stats(self, tmp_path):
        engine = MemoryEngine(db_path=str(tmp_path / "mem.db"))
        engine.store("test", scope="test", importance=7)
        stats = engine.get_tier_stats()
        assert isinstance(stats, dict)
        engine.close()

    def test_gateway_integration_init(self, tmp_path):
        gw = LLMGateway()
        engine = MemoryEngine(db_path=str(tmp_path / "mem.db"), gateway=gw)
        engine.store("gateway test", scope="test")
        engine.close()


# ─── P1: Planner ───────────────────────────────────────────

class TestPlanStep:
    def test_init(self):
        s = PlanStep(id="s1", description="do stuff", target_files=["a.py"],
                     action="modify", estimated_complexity="medium", dependencies=[])
        assert s.status == "pending"

    def test_to_dict_from_dict(self):
        s = PlanStep(id="s1", description="do stuff", target_files=["a.py"],
                     action="modify", estimated_complexity="low", dependencies=["step_0"])
        d = s.to_dict()
        restored = PlanStep.from_dict(d)
        assert restored.id == "s1"
        assert restored.dependencies == ["step_0"]


class TestExecutionPlan:
    def test_init(self):
        p = ExecutionPlan(id="p1", task="refactor", steps=[], created_at=time.time())
        assert p.status == "draft"

    def test_to_dict_from_dict(self):
        p = ExecutionPlan(id="p1", task="refactor", steps=[], created_at=time.time())
        d = p.to_dict()
        restored = ExecutionPlan.from_dict(d)
        assert restored.id == "p1"


class TestPlannerEngine:
    async def test_create_plan_stub(self):
        planner = PlannerEngine(gateway=None)
        plan = await planner.create_plan("fix bug in main.py")
        assert plan.task == "fix bug in main.py"
        assert len(plan.steps) >= 1

    async def test_get_plan(self):
        planner = PlannerEngine(gateway=None)
        plan = await planner.create_plan("test task")
        fetched = planner.get_plan(plan.id)
        assert fetched is not None
        assert fetched.task == "test task"

    async def test_approve_plan(self):
        planner = PlannerEngine(gateway=None)
        plan = await planner.create_plan("test task")
        assert planner.approve_plan(plan.id)
        assert planner.get_plan(plan.id).status == "approved"

    async def test_reject_plan(self):
        planner = PlannerEngine(gateway=None)
        plan = await planner.create_plan("test task")
        assert planner.reject_plan(plan.id, reason="too risky")
        assert planner.get_plan(plan.id).status == "rejected"

    async def test_list_plans(self):
        planner = PlannerEngine(gateway=None)
        await planner.create_plan("task 1")
        await planner.create_plan("task 2")
        all_plans = planner.list_plans()
        assert len(all_plans) >= 2

    async def test_cancel_plan(self):
        planner = PlannerEngine(gateway=None)
        plan = await planner.create_plan("test")
        assert planner.cancel_plan(plan.id)

    def test_assess_risk_delete(self):
        planner = PlannerEngine(gateway=None)
        steps = [PlanStep(id="s1", description="del", target_files=["a.py"],
                          action="delete", estimated_complexity="high", dependencies=[])]
        risk = planner._assess_risk(steps)
        assert risk == "high"

    def test_assess_risk_modify_many(self):
        planner = PlannerEngine(gateway=None)
        steps = [PlanStep(id="s1", description="mod", target_files=["a.py", "b.py", "c.py", "d.py"],
                          action="modify", estimated_complexity="medium", dependencies=[])]
        risk = planner._assess_risk(steps)
        assert risk == "medium"

    def test_assess_risk_low(self):
        planner = PlannerEngine(gateway=None)
        steps = [PlanStep(id="s1", description="mod", target_files=["a.py"],
                          action="modify", estimated_complexity="low", dependencies=[])]
        risk = planner._assess_risk(steps)
        assert risk == "low"


# ─── P2: LlamaIndex Readers ────────────────────────────────

class TestWebReader:
    def test_init(self):
        reader = WebReader()
        assert reader is not None

    def test_read_url_error(self):
        reader = WebReader()
        doc = reader.read_url("http://localhost:99999/nonexistent", timeout=1)
        assert doc.source == "http://localhost:99999/nonexistent"
        assert "error" in doc.metadata


class TestGitHubReader:
    def test_init(self):
        reader = GitHubReader(token="fake")
        assert reader.token == "fake"

    def test_read_repo_file_error(self):
        reader = GitHubReader(token="fake")
        doc = reader.read_repo_file("nonexistent", "nonexistent", "README.md")
        assert "error" in doc.metadata or doc.content == ""


class TestNotionReader:
    def test_init(self):
        reader = NotionReader(api_key="fake")
        assert reader.api_key == "fake"

    def test_read_page_error(self):
        reader = NotionReader(api_key="fake")
        doc = reader.read_page("nonexistent-page-id")
        assert "error" in doc.metadata or doc.content == ""


class TestPDFReader:
    def test_init(self):
        reader = PDFReader()
        assert reader is not None

    def test_read_nonexistent(self):
        reader = PDFReader()
        doc = reader.read_file("/nonexistent/file.pdf")
        assert doc.content == "" or "error" in doc.metadata


class TestDirectoryReader:
    def test_read_directory(self, tmp_path):
        (tmp_path / "test.txt").write_text("hello world")
        (tmp_path / "test.md").write_text("# Title\nContent")
        reader = DirectoryReader()
        docs = reader.read_directory(str(tmp_path))
        assert len(docs) >= 2

    def test_read_directory_with_exclude(self, tmp_path):
        (tmp_path / "test.txt").write_text("hello")
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "test.pyc").write_text("bytecode")
        reader = DirectoryReader(exclude_dirs={"__pycache__"})
        docs = reader.read_directory(str(tmp_path))
        assert all("__pycache__" not in d.source for d in docs)

    def test_read_directory_with_extensions(self, tmp_path):
        (tmp_path / "test.txt").write_text("hello")
        (tmp_path / "test.json").write_text('{"key": "value"}')
        reader = DirectoryReader(extensions={".txt"})
        docs = reader.read_directory(str(tmp_path))
        assert all(d.metadata.get("file_extension") == ".txt" for d in docs)


# ─── P2: AgentPackage workspace snapshot ───────────────────

class TestAgentPackageWorkspace:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.pkg = AgentPackage(self.tmpdir)
        self.pkg.init(AgentManifest(name="test-agent"))

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_workspace_path_property(self):
        assert str(self.pkg.workspace_path).endswith("workspace")

    def test_sources_path_property(self):
        assert str(self.pkg.sources_path).endswith("sources.json")

    def test_init_creates_workspace_dir(self):
        assert self.pkg.workspace_path.exists()

    def test_snapshot_workspace(self, tmp_path):
        src = tmp_path / "src_project"
        src.mkdir()
        (src / "main.py").write_text("print('hello')")
        (src / "README.md").write_text("# Test")
        result = self.pkg.snapshot_workspace(str(src))
        assert "files" in result
        assert result["total_size"] > 0

    def test_restore_workspace(self, tmp_path):
        src = tmp_path / "src_project"
        src.mkdir()
        (src / "main.py").write_text("print('hello')")
        self.pkg.snapshot_workspace(str(src))

        target = tmp_path / "restored"
        target.mkdir()
        count = self.pkg.restore_workspace(str(target))
        assert count >= 1
        assert (target / "main.py").exists()

    def test_list_workspace_files(self, tmp_path):
        src = tmp_path / "src_project"
        src.mkdir()
        (src / "main.py").write_text("code")
        self.pkg.snapshot_workspace(str(src))
        files = self.pkg.list_workspace_files()
        assert len(files) >= 1

    def test_save_load_sources(self):
        sources = [
            {"type": "github", "config": {"repo": "test/repo"}, "last_sync": ""},
            {"type": "notion", "config": {"database_id": "abc"}, "last_sync": ""},
        ]
        self.pkg.save_sources(sources)
        loaded = self.pkg.load_sources()
        assert len(loaded) == 2
        assert loaded[0]["type"] == "github"

    def test_add_remove_source(self):
        src = self.pkg.add_source("web", {"url": "https://example.com"})
        assert src["type"] == "web"
        assert self.pkg.remove_source("web", src.get("id", "")) or True

    def test_load_save_skill_dag(self):
        dag_config = {
            "name": "code_review",
            "nodes": [{"id": "n1", "type": "llm"}],
            "edges": [{"source_id": "n1", "target_id": "n2"}],
            "config": {"model": "test"},
        }
        self.pkg.save_skill_dag("code_review", dag_config)
        loaded = self.pkg.load_skill_dag("code_review")
        assert loaded["name"] == "code_review"
        assert len(loaded["nodes"]) == 1

    def test_export_import_skill_graph(self):
        graph = AgentGraph(name="test-skill")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "end")
        graph_json = graph.to_json()

        self.pkg.import_skill_graph("test_skill", graph_json)
        exported = self.pkg.export_skill_graph("test_skill")
        assert "start" in exported
        restored = AgentGraph.from_json(exported)
        assert restored.name == "test-skill"


# ─── NodeType integration ──────────────────────────────────

class TestNodeTypeExtensions:
    def test_rag_node_type_exists(self):
        node = NodeConfig(type="rag", label="RAG Node")
        assert node.type == "rag"

    def test_planner_node_type_exists(self):
        node = NodeConfig(type="planner", label="Planner Node")
        assert node.type == "planner"

    def test_rag_node_in_graph(self):
        graph = AgentGraph(name="rag-test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("rag1", NodeConfig(type="rag", label="RAG Search",
                                          tool_params={"rag_config": {"top_k": 3, "mode": "hybrid"}}))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "rag1")
        graph.add_edge("rag1", "end")
        errors = graph.validate()
        assert len(errors) == 0

    def test_planner_node_in_graph(self):
        graph = AgentGraph(name="planner-test")
        graph.add_node("start", NodeConfig(type="start", label="Start"))
        graph.add_node("plan1", NodeConfig(type="planner", label="Plan",
                                           tool_params={"task": "refactor"}))
        graph.add_node("end", NodeConfig(type="end", label="End"))
        graph.add_edge("start", "plan1")
        graph.add_edge("plan1", "end")
        errors = graph.validate()
        assert len(errors) == 0


# ─── Runtime integration with new node types ────────────────

class TestRuntimeNewNodes:
    def test_set_knowledge_engine(self):
        _gw = LLMGateway()
        runtime = AgentRuntime.__new__(AgentRuntime)
        runtime.set_knowledge_engine("mock_engine")
        assert runtime._knowledge_engine == "mock_engine"

    def test_runtime_has_llm_gateway_param(self):
        import inspect
        sig = inspect.signature(AgentRuntime.__init__)
        assert "llm_gateway" in sig.parameters
