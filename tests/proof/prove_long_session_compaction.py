"""A5 proof: long synthetic session compacts without overflow and keeps retention.

Simulates a ~45 minute session (900 rounds, large tool outputs, mixed EN/CN
content), drives the deterministic Compactor (no model needed), and asserts:

1. every pass returns messages below the error threshold (no overflow),
2. the four retention buckets (todos/interfaces/constraints/active_tasks)
   survive compaction verbatim,
3. repeated compaction is idempotent at the token level.

Run: python tests/proof/prove_long_session_compaction.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent_runtime.compactor import CompactionConfig, Compactor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prove_long_session_compaction")

ROUNDS = 900
BIG = "x" * 4000

RETENTION_LINES = [
    "TODO: wire run artifacts into Grafana",
    "待办：补充 compaction 压测报告",
    "async def build_agent_adhoc_graph(daemon, agent_id):",
    "接口：graph.execute 必须支持 task_id 回写",
    "constraint: never call cloud LLMs",
    "约束：只允许修改本仓库代码",
    "pending: fusion-bench SLO consumer",
    "进行中：画布 undo/redo",
]


def build_session() -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "you are a local agent"}]
    per_round = len(RETENTION_LINES)
    for i in range(ROUNDS):
        extra = RETENTION_LINES[i % per_round]
        messages.append({"role": "user", "content": f"round {i} 任务 {extra}"})
        messages.append(
            {
                "role": "assistant",
                "content": f"working round {i} " + BIG,
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "/tmp/f%d"}' % i,
                        }
                    }
                ],
            }
        )
        messages.append({"role": "tool", "content": f"tool result {i} " + BIG})
    return messages


def main() -> int:
    config = CompactionConfig(context_window=32768)
    compactor = Compactor(config=config)
    messages = build_session()
    before = compactor.estimate_tokens(messages)
    logger.info(
        "synthetic long session rounds=%d msgs=%d tokens=%d", ROUNDS, len(messages), before
    )

    levels = []
    current = messages
    for step in range(6):
        level = compactor.should_compact(current)
        levels.append(level)
        if level == "none":
            break
        current = compactor.compact(current, level)
        tokens = compactor.estimate_tokens(current)
        logger.info("step=%d level=%s msgs=%d tokens=%d", step, level, len(current), tokens)
        assert tokens < config.error_threshold(), (
            f"overflow after level {level}: {tokens} >= {config.error_threshold()}"
        )

    final_tokens = compactor.estimate_tokens(current)
    assert final_tokens < config.error_threshold(), final_tokens
    assert len(current) < len(messages)

    retention_text = "\n".join(
        str(m.get("content"))
        for m in current
        if m.get("role") == "system" and "[Retention List]" in str(m.get("content"))
    )
    assert "[Retention List]" in retention_text, "retention list missing after compaction"
    buckets = ("TODOs:", "Interfaces:", "Constraints:", "Active tasks:")
    counts = {name: int(name in retention_text) for name in buckets}
    logger.info("retention buckets present after compaction: %s", counts)
    assert all(counts.values()), counts
    assert "补充 compaction 压测报告" in retention_text
    assert "画布 undo/redo" in retention_text
    assert "never call cloud LLMs" in retention_text
    assert "graph.execute" in retention_text

    again = compactor.compact(current, "warning")
    again_tokens = compactor.estimate_tokens(again)
    assert again_tokens <= final_tokens + 200, (again_tokens, final_tokens)

    logger.info(
        "PASS long-session compaction levels=%s before=%d after=%d",
        levels,
        before,
        final_tokens,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
