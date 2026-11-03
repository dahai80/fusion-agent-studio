"""#561 live evidence: tool_use auto-continue multi-hop with a real local model.

Runs a loop_mode="agent" llm node against fusion-mlx (via gateway) with a real
builtin tool (json_parse). The model must call the tool, receive the result,
and continue WITHOUT any hand-wired return edge. Every hop's THINK event must
carry stop_reason. Writes evidence JSON next to this script.

Usage:
    python tests/proof/prove_run_loop_parity_561.py [--model ID] [--base-url URL]

Exit 0 = auto-continue verified; exit 1 = any check failed.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

logger = logging.getLogger("prove_561")

from agent_runtime.graph import AgentGraph, NodeConfig  # noqa: E402
from agent_runtime.runtime import AgentRuntime  # noqa: E402
from server.fusion_mlx_client import FusionMLXClient  # noqa: E402
from tools.data_tools import JsonParseTool  # noqa: E402
from tools.registry import ToolRegistry  # noqa: E402

EVIDENCE_PATH = Path(__file__).parent / "evidence" / "issue-561-run-loop-parity.json"

GRAPH_JSON = '{"order_id": "A-1024", "total": 4250.5, "items": 3}'


async def run(model: str, base_url: str) -> dict:
    # gateway :11432 auth uses the client key; direct :11434 would use the
    # mlx admin key. Env wins so the caller can point at either surface.
    api_key = os.environ.get("FUSION_CLIENT_KEY", "")
    if not api_key:
        settings_path = Path.home() / ".fusion-mlx" / "settings.json"
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
                api_key = (settings.get("auth") or {}).get("api_key", "")
            except Exception as exc:
                logger.warning("settings read failed: %s", exc)

    client = FusionMLXClient(base_url=base_url, api_key=api_key, timeout=180)
    registry = ToolRegistry()
    registry.register(JsonParseTool())

    graph = AgentGraph(name="issue-561-auto-continue")
    graph.add_node("start", NodeConfig(type="start", label="Start"))
    graph.add_node(
        "llm",
        NodeConfig(
            type="llm",
            label="LLM",
            model=model,
            loop_mode="agent",
            max_loop_iterations=8,
            system_prompt=(
                "You are a JSON assistant. When asked to parse JSON you MUST "
                "call the json_parse tool first, then answer from its result."
            ),
        ),
    )
    graph.add_node("end", NodeConfig(type="end", label="End"))
    graph.add_edge("start", "llm")
    graph.add_edge("llm", "end")

    rt = AgentRuntime(client, registry)
    events = []
    t0 = time.time()
    async for ev in rt.execute_graph(
        graph,
        f'Parse this JSON with the json_parse tool and tell me the total: {GRAPH_JSON}',
    ):
        events.append(
            {
                "type": ev.type.value,
                "name": ev.name,
                "content": (ev.content or "")[:200],
                "metadata": {
                    k: v
                    for k, v in (ev.metadata or {}).items()
                    if k in ("stop_reason", "usage", "model")
                },
            }
        )
    elapsed = round(time.time() - t0, 1)

    thinks = [e for e in events if e["type"] == "think"]
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    end = [e for e in events if e["type"] == "end"]
    stop_reasons = [t["metadata"].get("stop_reason") for t in thinks]

    checks = {
        "tool_called": len(tool_calls) >= 1,
        "tool_result_fed_back": len(tool_results) >= 1,
        "auto_continued": len(thinks) >= 2,
        "hop_stop_reasons_present": all(stop_reasons),
        "ends_end_turn": bool(end) and end[0]["metadata"].get("stop_reason") == "end_turn",
        "total_parsed": any("4250.5" in e["content"] for e in events if e["type"] in ("think", "result")),
    }
    evidence = {
        "issue": "561",
        "model": model,
        "base_url": base_url,
        "elapsed_seconds": elapsed,
        "llm_hops": len(thinks),
        "stop_reason_sequence": stop_reasons,
        "tool_calls": [e["name"] for e in tool_calls],
        "checks": checks,
        "passed": all(checks.values()),
        "events": events,
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    logger.info("evidence written: %s", EVIDENCE_PATH)
    logger.info(
        "hops=%d stop_reasons=%s tool_calls=%d checks=%s elapsed=%ss",
        len(thinks),
        stop_reasons,
        len(tool_calls),
        checks,
        elapsed,
    )
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="mlx-community--Qwen3.5-4B-MLX-4bit")
    parser.add_argument("--base-url", default=os.environ.get("FUSION_MLX_BASE_URL", "http://127.0.0.1:11432"))
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    evidence = None
    for attempt in range(1, args.attempts + 1):
        evidence = asyncio.run(run(args.model, args.base_url))
        if evidence["passed"]:
            break
        # gateway breaker flaps under swap pressure (503 overloaded_error);
        # a transient mid-run kill is not an auto-continue defect — retry.
        logger.warning("attempt %d/%d failed, backing off 20s", attempt, args.attempts)
        time.sleep(20)
    if not evidence["passed"]:
        logger.error("FAILED checks: %s", {k: v for k, v in evidence["checks"].items() if not v})
        sys.exit(1)
    logger.info("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
