from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.compactor import CompactionConfig, Compactor
from agent_runtime.funnels import LifecycleBus, LLMFunnel, ToolFunnel

logger = logging.getLogger("prove_lifecycle_compaction")


class Ctx:
    def __init__(self, session_id="lc-proof"):
        self.session_id = session_id
        self.plan_mode = False


def prove_seq_and_replay():
    bus = LifecycleBus(maxlen=100)
    for i in range(5):
        bus.emit("agent", "e" + str(i), {"i": i}, session_id="A")
    bus.emit("agent", "solo", {}, session_id="B")
    seqs = [e.seq for e in bus.replay()]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    by_a = [e.event for e in bus.replay(session_id="A")]
    assert by_a == ["e0", "e1", "e2", "e3", "e4"], by_a
    limited = bus.replay(limit=3)
    assert len(limited) == 3 and limited[-1].event == "solo"
    assert bus.session_count("A") == 5 and bus.session_count("B") == 1
    logger.info("PASS seq monotonic + per-session replay + limit")


def prove_subscriber_fail_open():
    bus = LifecycleBus()
    seen = []

    def bad(ev):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda ev: seen.append(ev.event))
    ev = bus.emit("agent", "evt_ok", session_id="A")
    assert ev.seq == 1 and seen == ["evt_ok"]
    bus.unsubscribe(bad)
    bus.emit("agent", "evt_ok2", session_id="A")
    assert seen == ["evt_ok", "evt_ok2"]
    logger.info("PASS subscriber error fail-open")


def prove_jsonl_persistence():
    tmpdir = tempfile.mkdtemp(prefix="proof_lifecycle_")
    try:
        bus = LifecycleBus(jsonl_dir=tmpdir)
        bus.emit("agent", "evt_a", {"k": 1}, session_id="S1")
        bus.emit("agent", "evt_b", {}, session_id="")
        s1_line = (Path(tmpdir) / "S1.jsonl").read_text().strip().splitlines()[0]
        g_line = (Path(tmpdir) / "global.jsonl").read_text().strip().splitlines()[0]
        s1 = json.loads(s1_line)
        g = json.loads(g_line)
        assert s1["event"] == "evt_a" and s1["payload"] == {"k": 1}
        assert g["event"] == "evt_b" and g["session_id"] == ""
        assert not (Path(tmpdir) / "missing.jsonl").exists()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    logger.info("PASS jsonl persistence + cleanup")


def prove_tool_funnel_events():
    bus = LifecycleBus()
    funnel = ToolFunnel(permissions=None, lifecycle_bus=bus)
    outcome_holder = {}

    async def _run():
        out = await funnel.admit(ctx=Ctx(), tool_name="t1", args={"a": 1})
        outcome_holder["out"] = out

    asyncio.run(_run())
    out = outcome_holder["out"]
    assert out.allowed is True
    events = [e.event for e in bus.replay(session_id="lc-proof")]
    assert events == ["tool_before"], events
    logger.info("PASS tool funnel lifecycle events")


def prove_llm_funnel_compaction_events():
    bus = LifecycleBus()
    compactor = Compactor(
        CompactionConfig(
            context_window=200,
            warning_buffer=100,
            error_buffer=40,
            manual_buffer=150,
            keep_recent_rounds=1,
        )
    )
    funnel = LLMFunnel(compactor=compactor, lifecycle_bus=bus)
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "U" * 400},
        {"role": "assistant", "content": "A" * 400},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "final"},
    ]
    holder = {}

    async def _run():
        proceed, reason, out = await funnel.pre_llm(Ctx(), messages=msgs)
        holder["result"] = (proceed, reason, out)

    asyncio.run(_run())
    proceed, reason, out = holder["result"]
    assert proceed is True and reason == ""
    assert out is not msgs
    assert any("[Compacted prior context]" in m.get("content", "") for m in out if m.get("role") == "system")
    events = [e.event for e in bus.replay(session_id="lc-proof", domain="agent")]
    for expected in ("llm_before", "compact_before", "compact_after"):
        assert expected in events, expected
    funnel.post_llm(Ctx(), node_label="n", model="m")
    assert funnel.calls == 1
    logger.info("PASS llm funnel compaction events + metering")


def main():
    prove_seq_and_replay()
    prove_jsonl_persistence()
    prove_subscriber_fail_open()
    prove_tool_funnel_events()
    prove_llm_funnel_compaction_events()
    logger.info("ALL LIFECYCLE COMPACTION PROOFS PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
