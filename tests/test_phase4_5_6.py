"""Tests for Phase 4, 5, 6 modules."""
from __future__ import annotations

import pytest

from agent_runtime.data_ingestion import (
    Document, Chunk, DocumentReader, FixedSizeChunker, SentenceChunker,
    MarkdownChunker, ETLPipeline, strip_whitespace, truncate, add_metadata,
    filter_empty_chunks,
)
from agent_runtime.code_sandbox import (
    ASTChecker, DiffPreview, CodeSandbox, SandboxResult,
)
from agent_runtime.aware_engine import (
    FileEvent, AwareResult, DebounceLayer, ASTDiffLayer, ModelGateLayer, AwareEngine,
)
from agent_runtime.fmp_router import (
    AgentInfo, FMPMessageV2, AgentCircuitBreaker, MessageDedup,
    TurnManager, MentionRouter, FMProtocol,
)


# ── Data Ingestion (Phase 4) ────────────────────────────

class TestDocument:
    def test_auto_id(self):
        d = Document(content="hello")
        assert d.id

    def test_to_dict_roundtrip(self):
        d = Document(id="x", content="hi", source="test", metadata={"k": 1})
        restored = Document.from_dict(d.to_dict())
        assert restored.id == "x"
        assert restored.content == "hi"
        assert restored.metadata["k"] == 1


class TestChunk:
    def test_auto_id(self):
        c = Chunk(content="chunk text", document_id="doc1")
        assert c.id

    def test_to_dict_roundtrip(self):
        c = Chunk(id="c1", content="text", document_id="d1", index=0, start_char=0, end_char=4)
        restored = Chunk.from_dict(c.to_dict())
        assert restored.id == "c1"
        assert restored.index == 0


class TestDocumentReader:
    def test_read_txt(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        reader = DocumentReader()
        doc = reader.read_file(f)
        assert doc.content == "hello world"
        assert doc.metadata["file_extension"] == ".txt"

    def test_read_md(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Title\n\nSome text")
        reader = DocumentReader()
        doc = reader.read_file(f)
        assert "Title" in doc.content

    def test_read_json(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        reader = DocumentReader()
        doc = reader.read_file(f)
        assert "key" in doc.content
        assert doc.metadata["json_type"] == "dict"

    def test_read_csv(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text("name,age\nAlice,30\nBob,25")
        reader = DocumentReader()
        doc = reader.read_file(f)
        assert "Alice" in doc.content
        assert doc.metadata["row_count"] == 2

    def test_read_html(self, tmp_path):
        f = tmp_path / "test.html"
        f.write_text("<html><body><h1>Title</h1><p>Text</p></body></html>")
        reader = DocumentReader()
        doc = reader.read_file(f)
        assert "Title" in doc.content
        assert "<" not in doc.content

    def test_read_unsupported(self, tmp_path):
        f = tmp_path / "test.xyz"
        f.write_text("data")
        reader = DocumentReader()
        with pytest.raises(ValueError, match="Unsupported"):
            reader.read_file(f)

    def test_read_not_found(self):
        reader = DocumentReader()
        with pytest.raises(FileNotFoundError):
            reader.read_file("/nonexistent/file.txt")

    def test_read_text_inline(self):
        reader = DocumentReader()
        doc = reader.read_text("inline content", source="test")
        assert doc.content == "inline content"
        assert doc.source == "test"


class TestFixedSizeChunker:
    def test_basic_chunking(self):
        doc = Document(content="A" * 1200, source="test")
        chunker = FixedSizeChunker(chunk_size=500, overlap=50)
        chunks = chunker.chunk(doc)
        assert len(chunks) >= 2
        assert chunks[0].content == "A" * 500
        assert chunks[0].metadata["strategy"] == "fixed"

    def test_short_content(self):
        doc = Document(content="short", source="test")
        chunker = FixedSizeChunker(chunk_size=500)
        chunks = chunker.chunk(doc)
        assert len(chunks) == 1
        assert chunks[0].content == "short"


class TestSentenceChunker:
    def test_basic_sentence(self):
        text = "First sentence. Second sentence. Third sentence. Fourth one here. Fifth and final."
        doc = Document(content=text, source="test")
        chunker = SentenceChunker(max_chunk_size=50, min_chunk_size=10)
        chunks = chunker.chunk(doc)
        assert len(chunks) >= 1

    def test_single_sentence(self):
        doc = Document(content="Just one sentence here.", source="test")
        chunker = SentenceChunker()
        chunks = chunker.chunk(doc)
        assert len(chunks) == 1


class TestMarkdownChunker:
    def test_with_headings(self):
        text = "# Intro\nSome intro text.\n## Section 1\nContent 1.\n## Section 2\nContent 2."
        doc = Document(content=text, source="test")
        chunker = MarkdownChunker()
        chunks = chunker.chunk(doc)
        assert len(chunks) >= 2
        headings = [c.metadata.get("heading") for c in chunks if c.metadata.get("heading")]
        assert "Intro" in headings
        assert "Section 1" in headings

    def test_no_headings_fallback(self):
        doc = Document(content="Just plain text without headings.", source="test")
        chunker = MarkdownChunker()
        chunks = chunker.chunk(doc)
        assert len(chunks) >= 1


class TestETLPipeline:
    def test_basic_pipeline(self):
        pipeline = ETLPipeline()
        pipeline.add_doc_transform(strip_whitespace)
        doc = Document(content="  hello   world  ", source="test")
        chunks = pipeline.process(doc, chunk_size=100)
        assert len(chunks) >= 1
        assert chunks[0].content == "hello world"

    def test_with_chunk_transform(self):
        pipeline = ETLPipeline()
        pipeline.add_chunk_transform(filter_empty_chunks)
        doc = Document(content="hello", source="test")
        chunks = pipeline.process(doc, chunk_size=500)
        assert len(chunks) == 1

    def test_truncate_transform(self):
        pipeline = ETLPipeline()
        pipeline.add_doc_transform(truncate(max_chars=10))
        doc = Document(content="A" * 100, source="test")
        chunks = pipeline.process(doc, chunk_size=500)
        assert len(chunks) == 1
        assert len(chunks[0].content) == 10

    def test_add_metadata_transform(self):
        pipeline = ETLPipeline()
        pipeline.add_doc_transform(add_metadata("source_type", "test"))
        doc = Document(content="hello", source="test")
        chunks = pipeline.process(doc, chunk_size=500)
        assert chunks[0].document_id == doc.id

    def test_process_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world from file")
        pipeline = ETLPipeline()
        chunks = pipeline.process_file(f, chunk_size=500)
        assert len(chunks) >= 1
        assert "hello" in chunks[0].content

    def test_process_text(self):
        pipeline = ETLPipeline()
        chunks = pipeline.process_text("hello world", chunk_size=500)
        assert len(chunks) >= 1


# ── Code Sandbox (Phase 5) ──────────────────────────────

class TestASTChecker:
    def test_safe_code(self):
        checker = ASTChecker()
        result = checker.analyze("x = 1 + 2\nprint(x)")
        assert result.safe
        assert "print" in result.function_calls

    def test_dangerous_import(self):
        checker = ASTChecker()
        result = checker.analyze("import os")
        assert not result.safe
        assert any("Dangerous import" in i for i in result.issues)

    def test_dangerous_call(self):
        checker = ASTChecker()
        result = checker.analyze("eval('1+1')")
        assert not result.safe
        assert any("Dangerous call" in i for i in result.issues)

    def test_syntax_error(self):
        checker = ASTChecker()
        result = checker.analyze("def (")
        assert not result.safe
        assert any("Syntax error" in i for i in result.issues)

    def test_from_import(self):
        checker = ASTChecker()
        result = checker.analyze("from subprocess import run")
        assert not result.safe

    def test_file_write_detection(self):
        checker = ASTChecker()
        result = checker.analyze("f.write('data')")
        assert result.has_file_write

    def test_network_detection(self):
        checker = ASTChecker()
        result = checker.analyze("s.connect(('host', 80))")
        assert result.has_network

    def test_analysis_to_dict(self):
        checker = ASTChecker()
        result = checker.analyze("x = 1")
        d = result.to_dict()
        assert "safe" in d
        assert "issues" in d


class TestDiffPreview:
    def test_no_changes(self):
        dp = DiffPreview()
        result = dp.diff("hello", "hello", "test.py")
        assert not result.has_changes

    def test_with_changes(self):
        dp = DiffPreview()
        result = dp.diff("hello", "world", "test.py")
        assert result.has_changes
        assert result.additions >= 1
        assert result.deletions >= 1

    def test_diff_files(self, tmp_path):
        orig = tmp_path / "orig.txt"
        mod = tmp_path / "mod.txt"
        orig.write_text("old content")
        mod.write_text("new content")
        dp = DiffPreview()
        result = dp.diff_files(orig, mod)
        assert result.has_changes

    def test_diff_result_to_dict(self):
        dp = DiffPreview()
        result = dp.diff("a", "b", "f.py")
        d = result.to_dict()
        assert "file_path" in d
        assert "has_changes" in d


class TestCodeSandbox:
    def test_safe_execution(self):
        sandbox = CodeSandbox(use_sandbox=False, timeout=10)
        result = sandbox.execute("x = 1 + 2\nprint(x)")
        assert result.success
        assert "3" in result.stdout

    def test_dangerous_code_blocked(self):
        sandbox = CodeSandbox(use_sandbox=False)
        result = sandbox.execute("import os\nos.system('ls')")
        assert not result.success
        assert "Safety check failed" in result.stderr

    def test_unsupported_language(self):
        sandbox = CodeSandbox(use_sandbox=False)
        result = sandbox.execute("print('hi')", language="ruby")
        assert not result.success

    def test_check_safety(self):
        sandbox = CodeSandbox(use_sandbox=False)
        analysis = sandbox.check_safety("x = 1")
        assert analysis.safe

    def test_sandbox_result_to_dict(self):
        r = SandboxResult(success=True, exit_code=0, stdout="ok", execution_id="abc")
        d = r.to_dict()
        assert d["success"]
        assert d["execution_id"] == "abc"


# ── Aware Engine (Phase 5) ──────────────────────────────

class TestFileEvent:
    def test_auto_fields(self):
        e = FileEvent(path="/test.py", event_type="modified")
        assert e.event_id
        assert e.timestamp > 0

    def test_to_dict(self):
        e = FileEvent(path="/test.py")
        d = e.to_dict()
        assert d["path"] == "/test.py"


class TestAwareResult:
    def test_to_dict(self):
        r = AwareResult(path="/test.py", tier=1, significant=True, reason="test")
        d = r.to_dict()
        assert d["tier"] == 1
        assert d["significant"]


class TestDebounceLayer:
    def test_first_event_passes(self):
        layer = DebounceLayer(debounce_seconds=5.0)
        event = FileEvent(path="/test.py")
        result = layer.process(event)
        assert result is not None
        assert result.significant

    def test_rapid_event_debounced(self):
        layer = DebounceLayer(debounce_seconds=5.0)
        event1 = FileEvent(path="/test.py")
        event2 = FileEvent(path="/test.py")
        layer.process(event1)
        result = layer.process(event2)
        assert result is None

    def test_flush_pending(self):
        layer = DebounceLayer(debounce_seconds=60.0)
        event1 = FileEvent(path="/a.py")
        layer.process(event1)
        event2 = FileEvent(path="/a.py")
        layer.process(event2)
        pending = layer.flush_pending()
        assert len(pending) >= 1


class TestASTDiffLayer:
    def test_first_content_passes(self):
        layer = ASTDiffLayer()
        event = FileEvent(path="/test.py")
        result = layer.process(event, "x = 1")
        assert result.significant

    def test_same_ast_blocked(self):
        layer = ASTDiffLayer()
        event1 = FileEvent(path="/test.py")
        event2 = FileEvent(path="/test.py")
        layer.process(event1, "x = 1 + 2")
        result = layer.process(event2, "x = 1 + 2")
        assert not result.significant

    def test_changed_ast_passes(self):
        layer = ASTDiffLayer()
        event1 = FileEvent(path="/test.py")
        event2 = FileEvent(path="/test.py")
        layer.process(event1, "x = 1")
        result = layer.process(event2, "y = 2")
        assert result.significant

    def test_clear_cache(self):
        layer = ASTDiffLayer()
        event = FileEvent(path="/test.py")
        layer.process(event, "x = 1")
        layer.clear_cache()
        assert layer.get_cached_hash("/test.py") is None


class TestModelGateLayer:
    def test_heuristic_new_content(self):
        layer = ModelGateLayer()
        event = FileEvent(path="/test.py")
        result = layer.process(event, "", "new content")
        assert result.significant

    def test_heuristic_small_change(self):
        layer = ModelGateLayer()
        event = FileEvent(path="/test.py")
        result = layer.process(event, "same content", "same content")
        assert not result.significant

    def test_heuristic_large_change(self):
        layer = ModelGateLayer()
        event = FileEvent(path="/test.py")
        result = layer.process(event, "line1\nline2\nline3\nline4\n", "line1\nchanged\nline3\nline4\n")
        assert result.significant


class TestAwareEngine:
    def test_full_cascade(self):
        engine = AwareEngine(debounce_seconds=0.0)
        result = engine.process_file_change("/test.py", content="x = 1")
        assert result.significant

    def test_ast_block(self):
        engine = AwareEngine(debounce_seconds=0.0)
        engine.process_file_change("/test.py", content="x = 1")
        result = engine.process_file_change("/test.py", content="x = 1")
        assert not result.significant or result.tier >= 2

    def test_stats(self):
        engine = AwareEngine(debounce_seconds=0.0)
        engine.process_file_change("/test.py", content="x = 1")
        stats = engine.get_stats()
        assert "tier1_passed" in stats

    def test_reset_stats(self):
        engine = AwareEngine(debounce_seconds=0.0)
        engine.process_file_change("/test.py", content="x = 1")
        engine.reset_stats()
        stats = engine.get_stats()
        assert stats["tier1_passed"] == 0


# ── FMP Router (Phase 6) ────────────────────────────────

class TestAgentInfo:
    def test_auto_id(self):
        a = AgentInfo(name="test-agent")
        assert a.id

    def test_to_dict_roundtrip(self):
        a = AgentInfo(id="a1", name="reviewer", capabilities=["code"], priority=3)
        restored = AgentInfo.from_dict(a.to_dict())
        assert restored.name == "reviewer"
        assert restored.priority == 3


class TestFMPMessageV2:
    def test_auto_fields(self):
        m = FMPMessageV2(sender="a1", recipient="a2")
        assert m.message_id
        assert m.timestamp > 0

    def test_to_dict_roundtrip(self):
        m = FMPMessageV2(sender="a1", mention_targets=["reviewer"], priority=3)
        restored = FMPMessageV2.from_dict(m.to_dict())
        assert restored.mention_targets == ["reviewer"]
        assert restored.priority == 3


class TestAgentCircuitBreaker:
    def test_trips(self):
        cb = AgentCircuitBreaker(threshold=2, reset_time=60.0)
        cb.record_failure("a1")
        cb.record_failure("a1")
        assert cb.is_open("a1")

    def test_success_resets(self):
        cb = AgentCircuitBreaker(threshold=2, reset_time=60.0)
        cb.record_failure("a1")
        cb.record_failure("a1")
        cb.record_success("a1")
        assert not cb.is_open("a1")

    def test_get_status(self):
        cb = AgentCircuitBreaker()
        status = cb.get_status()
        assert "tripped_agents" in status


class TestMessageDedup:
    def test_not_duplicate_first(self):
        dedup = MessageDedup()
        assert not dedup.is_duplicate("msg1")

    def test_duplicate_detected(self):
        dedup = MessageDedup()
        dedup.is_duplicate("msg1")
        assert dedup.is_duplicate("msg1")

    def test_different_messages(self):
        dedup = MessageDedup()
        dedup.is_duplicate("msg1")
        assert not dedup.is_duplicate("msg2")


class TestTurnManager:
    def test_round_robin(self):
        tm = TurnManager()
        tm.set_order(["a1", "a2", "a3"])
        assert tm.next_turn() == "a1"
        assert tm.next_turn() == "a2"
        assert tm.next_turn() == "a3"
        assert tm.next_turn() == "a1"

    def test_exclude(self):
        tm = TurnManager()
        tm.set_order(["a1", "a2"])
        result = tm.next_turn(exclude={"a1"})
        assert result == "a2"

    def test_empty(self):
        tm = TurnManager()
        assert tm.next_turn() is None

    def test_reset(self):
        tm = TurnManager()
        tm.set_order(["a1", "a2"])
        tm.next_turn()
        tm.next_turn()
        tm.reset()
        assert tm.next_turn() == "a1"


class TestMentionRouter:
    def test_parse_mentions(self):
        mr = MentionRouter()
        mentions = mr.parse_mentions("@reviewer @coder please help")
        assert mentions == ["reviewer", "coder"]

    def test_route_by_name(self):
        mr = MentionRouter()
        agents = {"a1": AgentInfo(id="a1", name="reviewer")}
        msg = FMPMessageV2(mention_targets=["reviewer"])
        targets = mr.route_by_mention(msg, agents)
        assert "a1" in targets

    def test_route_by_id(self):
        mr = MentionRouter()
        agents = {"a1": AgentInfo(id="a1", name="reviewer")}
        msg = FMPMessageV2(mention_targets=["a1"])
        targets = mr.route_by_mention(msg, agents)
        assert "a1" in targets

    def test_no_match(self):
        mr = MentionRouter()
        agents = {"a1": AgentInfo(id="a1", name="reviewer")}
        msg = FMPMessageV2(mention_targets=["nonexistent"])
        targets = mr.route_by_mention(msg, agents)
        assert not targets


class TestFMProtocol:
    def test_register_and_send(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", name="reviewer", status="online"))
        msg = fmp.send(recipient="a1", payload={"text": "review this"})
        assert msg.sender == "local"
        assert msg.recipient == "a1"

    def test_receive_routes(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", name="reviewer", status="online"))
        msg = fmp.send(recipient="a1")
        result = fmp.receive(msg)
        assert result["action"] == "route"

    def test_receive_duplicate(self):
        fmp = FMProtocol(local_agent_id="local")
        msg = FMPMessageV2(sender="remote", recipient="local", message_id="dup1")
        fmp.receive(msg)
        result = fmp.receive(msg)
        assert result["action"] == "drop"
        assert result["reason"] == "duplicate"

    def test_max_rounds(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", status="online"))
        msg = FMPMessageV2(sender="remote", round_number=3)
        result = fmp.receive(msg)
        assert result["action"] == "drop"

    def test_mention_routing(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", name="coder", status="online"))
        msg = FMPMessageV2(sender="local", mention_targets=["coder"])
        targets = fmp.route(msg)
        assert "a1" in targets

    def test_turn_routing(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", name="agent1", status="online"))
        fmp.register_agent(AgentInfo(id="a2", name="agent2", status="online"))
        msg = FMPMessageV2(sender="local")
        targets = fmp.route(msg)
        assert len(targets) >= 1

    def test_circuit_breaker_failure(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", name="agent1", status="online"))
        fmp.record_failure("a1")
        fmp.record_failure("a1")
        fmp.record_failure("a1")
        msg = FMPMessageV2(sender="local", recipient="a1")
        targets = fmp.route(msg)
        assert "a1" not in targets

    def test_stats(self):
        fmp = FMProtocol(local_agent_id="local")
        stats = fmp.get_stats()
        assert "sent" in stats
        assert "agents" in stats

    def test_unregister_agent(self):
        fmp = FMProtocol(local_agent_id="local")
        fmp.register_agent(AgentInfo(id="a1", name="reviewer"))
        fmp.unregister_agent("a1")
        msg = FMPMessageV2(sender="local", recipient="a1")
        result = fmp.receive(msg)
        assert result["action"] == "drop"
