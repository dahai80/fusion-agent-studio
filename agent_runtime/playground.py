"""E6: prompt playground — replay a prompt (from a trace or raw messages)
with edited system/prompt/params against the local model and diff the
result against the baseline. Local-only inference (zero egress).
"""

from __future__ import annotations

import difflib
import logging
import time

logger = logging.getLogger(__name__)


class Playground:
    def __init__(self, mlx_client, model: str = ""):
        self.mlx_client = mlx_client
        self.model = model or getattr(mlx_client, "model", "") or ""

    async def replay(
        self,
        messages: list[dict],
        system: str = "",
        params: dict | None = None,
    ) -> dict:
        started = time.time()
        msgs = list(messages or [])
        if system:
            msgs = [{"role": "system", "content": system}] + [
                m for m in msgs if m.get("role") != "system"
            ]
        p = params or {}
        resp = await self.mlx_client.chat(
            model=self.model,
            messages=msgs,
            temperature=float(p.get("temperature", 0.7)),
            max_tokens=int(p.get("max_tokens", 4096)),
        )
        content = resp.content if hasattr(resp, "content") else str(resp)
        usage = resp.usage if hasattr(resp, "usage") else {}
        out = {
            "output": content,
            "usage": usage,
            "duration_ms": int((time.time() - started) * 1000),
            "model": self.model,
            "params": p,
        }
        logger.info(
            "playground replay model=%s msgs=%d out_len=%d", self.model, len(msgs), len(content)
        )
        return out

    @staticmethod
    def diff(base_output: str, new_output: str) -> dict:
        changed = base_output != new_output
        lines = list(
            difflib.unified_diff(
                str(base_output).splitlines(keepends=True),
                str(new_output).splitlines(keepends=True),
                fromfile="baseline",
                tofile="replay",
            )
        )
        return {"changed": changed, "unified": "".join(lines)}

    async def from_trace(
        self,
        trace_id: str,
        system: str = "",
        params: dict | None = None,
        prompt_override: str = "",
    ) -> dict:
        import json as _json

        from .eval_runner import get_trajectory_writer

        writer = get_trajectory_writer()
        data = None
        for meta in writer.list_trajectories(limit=200):
            if meta.get("trace_id") != trace_id:
                continue
            try:
                with open(writer.output_dir / meta["file"], "r", encoding="utf-8") as fh:
                    data = _json.load(fh)
            except (OSError, _json.JSONDecodeError) as e:
                logger.warning("trajectory load failed trace=%s: %s", trace_id, e)
                return {"error": f"trajectory load failed: {e}"}
            break
        if data is None:
            return {"error": f"trajectory not found: {trace_id}"}
        messages = data.get("messages") or []
        base_output = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
            "",
        )
        if not any(m.get("role") == "user" and m.get("content") for m in messages):
            return {"error": f"trajectory has no user input: {trace_id}"}
        if prompt_override:
            messages = [dict(m) for m in messages]
            for m in reversed(messages):
                if m.get("role") == "user":
                    m["content"] = prompt_override
                    break
        result = await self.replay(messages, system=system, params=params)
        if "error" in result:
            return result
        result["trace_id"] = trace_id
        result["baseline_output"] = base_output
        result["diff"] = self.diff(base_output, result["output"])
        return result
