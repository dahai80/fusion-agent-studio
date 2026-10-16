"""N12 session fork deep-copy + summary-resume (issue #346)."""



from agent_runtime.persistence import AgentStore, ChatSession
from agent_runtime.session_manager import SessionManager, SessionStatus


def _store(tmp_path):
    return AgentStore(str(tmp_path / "store.db"))


def _chat(store, session_id, msgs):
    s = ChatSession.from_dict({
        "id": session_id,
        "title": "parent",
        "mode": "chat",
        "messages": msgs,
    })
    store.save_chat_session(s)
    return s


class _StubRuntime:
    def __init__(self):
        self.captured = []

    async def execute_graph(self, graph, initial_input, context, **kw):
        self.captured.append(list(context.messages))
        from agent_runtime.context import AgentEvent, AgentEventType

        ctx = context
        ctx.add_message("assistant", "done: " + initial_input)
        yield AgentEvent(type=AgentEventType.START, content="start")
        yield AgentEvent(type=AgentEventType.THINK, content="final answer")


async def test_fork_deep_copy_seeds_parent_history(tmp_path):
    store = _store(tmp_path)
    _chat(store, "s1", [
        {"role": "user", "content": "hello from parent"},
        {"role": "assistant", "content": "parent replied"},
    ])
    rt = _StubRuntime()
    mgr = SessionManager(runtime=rt, store=store)
    bg = await mgr.fork("s1", "new question")
    await bg._task
    assert bg.status == SessionStatus.COMPLETED
    assert bg.metadata["fork_mode"] == "deep_copy"
    seeded = rt.captured[0]
    assert any(m["role"] == "user" and "hello from parent" in m["content"] for m in seeded)
    assert any(m["role"] == "assistant" and "parent replied" in m["content"] for m in seeded)


async def test_fork_summary_mode_seeds_single_summary(tmp_path):
    store = _store(tmp_path)
    _chat(store, "s2", [
        {"role": "user", "content": "question A"},
        {"role": "assistant", "content": "answer A"},
    ])
    rt = _StubRuntime()
    mgr = SessionManager(runtime=rt, store=store)
    bg = await mgr.fork("s2", "follow-up", summarize=True)
    await bg._task
    assert bg.metadata["fork_mode"] == "summary"
    seeded = rt.captured[0]
    assert len(seeded) == 1
    assert seeded[0]["role"] == "system"
    assert "Compacted prior context" in seeded[0]["content"]
    assert "question A" in seeded[0]["content"]


async def test_fork_unknown_session_fresh(tmp_path):
    store = _store(tmp_path)
    rt = _StubRuntime()
    mgr = SessionManager(runtime=rt, store=store)
    bg = await mgr.fork("nope", "hi")
    await bg._task
    assert bg.metadata["fork_mode"] == "fresh"
    assert rt.captured[0] == []


async def test_fork_from_graph_checkpoint_fallback(tmp_path):
    store = _store(tmp_path)
    state = {
        "messages": [
            {"role": "user", "content": "graph run input"},
            {"role": "assistant", "content": "graph run output"},
        ],
        "iteration_count": 2,
    }
    store.save_checkpoint(
        graph_id="g1",
        session_id="gs1",
        node_id="llm",
        state=state,
    )
    rt = _StubRuntime()
    mgr = SessionManager(runtime=rt, store=store)
    bg = await mgr.fork("gs1", "continue this")
    await bg._task
    assert bg.metadata["fork_mode"] == "deep_copy"
    seeded = rt.captured[0]
    assert any(m["role"] == "user" and "graph run input" in m["content"] for m in seeded)


async def test_fork_summarize_true_no_history_falls_back_fresh(tmp_path):
    store = _store(tmp_path)
    rt = _StubRuntime()
    mgr = SessionManager(runtime=rt, store=store)
    bg = await mgr.fork("missing", "hi", summarize=True)
    await bg._task
    assert bg.metadata["fork_mode"] == "fresh"


async def test_compactor_summarize_messages_public_api():
    from agent_runtime.compactor import Compactor

    msgs = [
        {"role": "user", "content": "find the bug in parser"},
        {"role": "assistant", "content": "fixed offset calc"},
        {"role": "user", "content": "add tests"},
        {"role": "assistant", "content": "tests added"},
    ]
    c = Compactor()
    s = c.summarize_messages(msgs)
    assert isinstance(s, str)
    assert "Compacted prior context" in s
    assert "parser" in s


async def test_session_fork_rpc_passes_summarize(tmp_path):
    from agent_runtime.daemon_server import DaemonServer

    store = _store(tmp_path)
    _chat(store, "s9", [
        {"role": "user", "content": "parent context"},
    ])
    rt = _StubRuntime()

    class _Daemon:
        def __init__(self):
            self.mgr = SessionManager(runtime=rt, store=store)

        def _get_session_manager(self):
            return self.mgr

    d = _Daemon()
    handler = DaemonServer._handle_session_fork.__get__(d)
    out = await handler({"session_id": "s9", "input": "go", "summarize": True})
    await d.mgr._sessions[out["id"]]._task
    assert out["metadata"]["fork_mode"] == "summary"
    assert d.mgr._sessions[out["id"]].status == SessionStatus.COMPLETED
