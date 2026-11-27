"""Node executors mixin — extracted from AgentRuntime (audit 0826 P2-4).

Holds all node-execution methods (llm/tool/condition/loop/error_handler/
parallel/sub_graph/rag/planner/verify) + their static helpers + parallel
tool fan-out + tool hooks + output mapping + memory auto-store. Method
bodies are verbatim moves from runtime.py; AgentRuntime inherits this mixin.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, AsyncIterator

from tools.plan_tools import EXIT_PLAN_MODE_SENTINEL

from ._runtime_helpers import (
    _MAX_RETRY_CONTEXT_MESSAGES,
    _MAX_TOOL_CALL_CHAIN,
    _max_sub_graph_depth,
    _parallel_branch_concurrency,
    logger,
)
from .context import AgentContext, AgentEvent, AgentEventType
from .graph import AgentGraph, NodeConfig
from .guardrails_client import (
    REDACTION_NOTICE,
    check_llm_input,
    check_llm_output,
    guardrails_enabled,
)
from .json_schema import JsonSchemaValidator
from .variable_manager import VariableManager


def _llm_call_timeout() -> float:
    # #617: per-call wall clock for llm_gateway.chat. Hard-coded 120s killed
    # seat agents generating long documents on a local ~20 tok/s model.
    # FUSION_LLM_TIMEOUT (seconds) overrides; the gateway's own stream
    # timeout (default 300s) still bounds the underlying HTTP call.
    # #664: default raised 120 -> 900 — supervisor/launchd respawns drop the
    # env override, and 120s made long-document seat agents structurally
    # unable to finish without a persistent env home.
    raw = os.environ.get("FUSION_LLM_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else 900.0
    except ValueError:
        logger.warning("invalid FUSION_LLM_TIMEOUT=%r, using 900.0", raw)
        return 900.0


class _NodeExecutorsMixin:
    @staticmethod
    def _thinking_visible(ctx: AgentContext, node: NodeConfig) -> bool:
        # N9 #348: run-level ctx var AND node-level opt-out. Default visible.
        if not node.tool_params.get("thinking", True):
            return False
        if ctx.variables is None:
            return True
        try:
            return bool(ctx.variables.get("show_thinking", True))
        except Exception as exc:
            logger.debug("thinking visibility check failed: %s", exc)
            return True

    @staticmethod
    def _escalate_repeat_tool_error(ctx: AgentContext, tool_name: str, result: str) -> str:
        # #623: small models retry a deterministically-failing tool until
        # max_turns (observed: artifact_create x5 -> "Max iterations
        # exceeded"). After the same tool returns the same error twice,
        # append a fallback hint so the next LLM call pivots.
        counts = getattr(ctx, "_repeat_tool_errors", None)
        if counts is None:
            counts = {}
            ctx._repeat_tool_errors = counts
        key = f"{tool_name}::{result[:160]}"
        counts[key] = counts.get(key, 0) + 1
        if counts[key] < 2:
            return result
        logger.warning(
            "repeat tool error escalation tool=%s count=%d node_ctx=%s",
            tool_name,
            counts[key],
            getattr(ctx, "session_id", ""),
        )
        return (
            f"{result} (hint: this tool failed identically {counts[key]} times "
            "in this session and is likely unavailable in this runtime; stop "
            "retrying it and use an alternative such as file_write)"
        )

    @staticmethod
    def _detect_unclosed_artifacts(content: str) -> list[str]:
        opens = re.findall(r'<artifact[^>]*\bid=["\']([^"\']+)["\']', content)
        closes = re.findall(r"</artifact>", content)
        if len(opens) > len(closes):
            return opens[len(closes) :]
        tag_opens = len(re.findall(r"<artifact-ref[^>]*>", content))
        tag_closes = len(re.findall(r"</artifact-ref>", content))
        if tag_opens > tag_closes:
            return re.findall(r'<artifact-ref[^>]*\bid=["\']([^"\']+)["\']', content)[tag_closes:]
        return []

    @staticmethod
    def _extract_breakpoint(content: str) -> str:
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        if paragraphs:
            last = paragraphs[-1]
            return last[:300]
        lines = content.strip().split("\n")
        if lines:
            return lines[-1][:300]
        return content[-300:]

    async def _execute_llm_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        model: str,
        tools_schema: list[dict],
        system_prompt: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Execute an LLM node — call fusion-mlx via HTTP API. Supports streaming."""
        # Issue #176: per-node model override. Graph-level model is a fallback;
        # an explicit node.model wins (matches rag/augmented-node pattern).
        if node.model and node.model != model:
            logger.info(
                "LLM node %s overriding graph model %s -> %s",
                node.label or "?",
                model,
                node.model,
            )
            model = node.model
        # #283: optional model_router for fast/slow dual-core. A callable
        # (node, model, prior_result) -> model_id overrides the resolved model.
        # Empty/None = current single-model behavior. Non-fatal: a router that
        # raises or returns empty falls back to the resolved model.
        router = getattr(node, "model_router", None)
        if router:
            try:
                prior = ctx.messages[-1].get("content", "") if ctx.messages else ""
                routed = router(node, model, prior) if callable(router) else None
                if routed and routed != model:
                    logger.info(
                        "model_router %s -> %s for node %s",
                        model,
                        routed,
                        node.label or "?",
                    )
                    model = routed
            except Exception as e:
                logger.warning("model_router failed, falling back: %s", e)
        messages = []

        # #541 prefix-cache ordering contract: messages[0] is the STABLE
        # system head (system prompt + agent definition + constant suffixes
        # like artifact guidelines / schema instructions). It must stay
        # byte-identical across turns of one session so the fusion-mlx
        # block-aware prefix cache keeps hitting. Volatile per-turn content
        # (artifact context snapshots, WF-1 anti-forgetting summaries) goes
        # into its own system message AFTER the head — never appended or
        # inserted into messages[0]. Enforced by
        # tests/test_issue_541_prefix_cache.py.

        if node.disable_tools:
            tools_schema = None
            logger.info(
                "LLM node %s disable_tools=True, tool injection skipped",
                node.label,
            )
        # Agent tool-surface narrowing: an agent graph's LLM node may declare
        # `tools_allowlist` (tool_params) — without it the model sees the WHOLE
        # runtime registry (60+ tools), which drowns small models.
        allowlist = (node.tool_params or {}).get("tools_allowlist")
        if allowlist:
            want = set(allowlist)
            if os.environ.get("FUSION_TOOL_MASKING", "0") == "1":
                # #702 cache discipline: availability expressed as an
                # order/length-preserving mask (x-fusion-masked flag) so a
                # narrowing transition never invalidates the KV prefix.
                from fusion_harness.cache_discipline import mask_tools

                tools_schema = mask_tools(tools_schema or [], want)
            else:
                tools_schema = [
                    t for t in (tools_schema or []) if t.get("function", {}).get("name") in want
                ]

        node_prompt = node.system_prompt or system_prompt
        if node_prompt:
            template_name = self._extract_template_name(node_prompt)
            if template_name:
                try:
                    node_prompt = self.templates.render(template_name, **ctx.variables.to_dict())
                except KeyError:
                    pass
            node_prompt = ctx.variables.interpolate(node_prompt)
            messages.append({"role": "system", "content": node_prompt})

        if self.artifact_manager and hasattr(ctx, "agent_id"):
            # WF-1: anti-forgetting turn counter
            ctx.artifact_turn_count += 1
            if ctx.artifact_turn_count % 5 == 0 and ctx.artifact_turn_count > 0:
                summary = self.artifact_manager.get_active_artifacts_context(ctx.agent_id, limit=10)
                if summary:
                    ctx.add_message(
                        "system",
                        f"[Anti-Forgetting Artifact Summary — turn {ctx.artifact_turn_count}]\n{summary}",
                    )
                    logger.info(
                        "WF-1 anti-forgetting summary injected: turn=%d agent=%s",
                        ctx.artifact_turn_count,
                        ctx.agent_id,
                    )

            context_window = getattr(self, "_context_window_override", 32768)
            artifact_result = self.artifact_manager.get_active_artifacts_context_budget_aware(
                ctx.agent_id,
                context_window=context_window,
            )
            artifact_ctx = artifact_result.get("context_text", "")
            if artifact_ctx:
                # #541: volatile per-turn snapshot — own system message after
                # the stable head, never merged into messages[0].
                messages.append({"role": "system", "content": artifact_ctx})

            artifact_mode = artifact_result.get("mode", "none")
            if artifact_mode not in ("none", "full"):
                logger.info(
                    "artifact context injection mode=%s utilization=%.2f agent=%s",
                    artifact_mode,
                    artifact_result.get("utilization", 0.0),
                    ctx.agent_id,
                )

            if artifact_mode != "none":
                try:
                    from .artifact_tools import ARTIFACT_SYSTEM_PROMPT

                    artifact_count = artifact_result.get("artifact_count", 0)
                    rendered = self.templates.render(
                        "artifact-long-text",
                        artifact_guidelines=ARTIFACT_SYSTEM_PROMPT,
                        artifact_count=artifact_count,
                        artifact_list=artifact_ctx,
                    )
                    # #541: contains artifact_list snapshot (volatile) — own
                    # system message after the head.
                    messages.append({"role": "system", "content": rendered})
                    logger.info(
                        "AS-8 artifact-aware prompt injected: artifacts=%d mode=%s",
                        artifact_count,
                        artifact_mode,
                    )
                except (KeyError, ValueError) as e:
                    logger.warning("artifact-long-text template render failed: %s", e)

            if self.compactor and artifact_result.get("utilization", 0.0) > 0.7:
                compact_level = self.compactor.should_compact(ctx.messages)
                if compact_level != "none":
                    before_count = len(ctx.messages)
                    await self._fire_tool_hooks(
                        "PRE_COMPACT",
                        "",
                        {
                            "graph_id": graph.id,
                            "node_id": node.label or "",
                            "before": before_count,
                            "level": compact_level,
                            "reason": "artifact_utilization",
                        },
                    )
                    ctx.messages = self.compactor.compact(ctx.messages, compact_level)
                    logger.info(
                        "proactive artifact-aware compaction: mode=%s level=%s before_msgs=%d after_msgs=%d",
                        artifact_mode,
                        compact_level,
                        before_count,
                        len(ctx.messages),
                    )
                    yield AgentEvent(
                        type=AgentEventType.THINK,
                        content=f"Proactive compaction triggered (artifact budget at {artifact_result.get('utilization', 0.0):.0%})",
                        metadata={
                            "artifact_mode": artifact_mode,
                            "compact_level": compact_level,
                            "artifact_utilization": artifact_result.get("utilization", 0.0),
                        },
                    )

        if self.artifact_manager:
            artifact_system_suffix = (
                "\n\n[Artifact Guidelines]\n"
                "- Use artifact-ref IDs to reference existing artifacts rather than duplicating content.\n"
                "- Prefer incremental modification (artifact_update with operation=replace_section/append/prepend) over full content replacement.\n"
                "- For staged generation: create artifact with sections, then fill sections incrementally.\n"
                "- Use artifact_load with preview_only=true to inspect artifacts without consuming full context.\n"
                "- Use artifact_context_budget to monitor context usage before large operations.\n"
            )
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] += artifact_system_suffix
            else:
                messages.insert(0, {"role": "system", "content": artifact_system_suffix.strip()})

        messages.extend(ctx.messages)

        schema_validator = None
        if node.tool_params.get("output_schema"):
            schema_validator = JsonSchemaValidator(node.tool_params["output_schema"])
            schema_instruction = schema_validator.to_instruction()
            if schema_instruction:
                if messages and messages[0].get("role") == "system":
                    messages[0]["content"] += "\n\n" + schema_instruction
                else:
                    messages.insert(0, {"role": "system", "content": schema_instruction})

        if self.safety_gateway:
            # #615: after a tool executes, the last message is the tool result —
            # untrusted data, not an instruction channel. Injection patterns
            # found in it (e.g. "file system: '/app'" in an error string) are
            # logged as advisory instead of forcing a safety approval that
            # headless runs cannot answer.
            last_msg = messages[-1] if messages else {}
            is_tool_result = isinstance(last_msg, dict) and last_msg.get("role") == "tool"
            safety_result = self.safety_gateway.evaluate_action(
                category="llm_call",
                content=str(messages[-1]) if messages else "",
                context=f"model={model} node={node.label}",
                injection_advisory=is_tool_result,
            )
            if safety_result.action.value == "block" and not safety_result.requires_approval:
                ctx.error = f"SafetyGateway blocked LLM call: {safety_result.reason}"
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=safety_result.reason,
                    metadata={"action": "blocked", "category": "llm_call"},
                )
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return
            # #499: approval chokepoint handles both ask and auto-pass
            # verdicts — the confirmation policy may escalate or auto-approve.
            async for evt in self._await_safety_approval(
                ctx, safety_result, "llm_call", node.label
            ):
                yield evt
                if evt.type == AgentEventType.ERROR:
                    return

        capability = node.tool_params.get("capability", "")
        effort = node.effort or ""

        # #683: fusion-security guardrails input filter — opt-in
        # (FUSION_GUARDRAILS_ENABLED), fail-open on transport errors. Blocks
        # only on an explicit action=block verdict from the service.
        if guardrails_enabled():
            last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
            gi = await check_llm_input(str(last_user.get("content", "") or ""))
            if gi.get("action") == "block":
                ctx.error = (
                    f"Guardrails blocked user input: {gi.get('reason') or 'policy violation'}"
                )
                logger.warning(
                    "guardrails input block node=%s category=%s", node.label, gi.get("category")
                )
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=ctx.error,
                    metadata={"action": "blocked", "category": "guardrails_input"},
                )
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

        # N0 LLMFunnel: single LLM admission (lifecycle llm_before ->
        # LLM_BEFORE hook -> compaction check). Blocked -> node error.
        proceed, block_reason, messages = await self.llm_funnel.pre_llm(
            ctx, node.label or "", model, messages
        )
        if not proceed:
            ctx.error = block_reason
            logger.warning("llm_funnel blocked node=%s reason=%s", node.label, block_reason)
            yield AgentEvent(type=AgentEventType.ERROR, content=block_reason)
            return

        # C13: llm.call span — covers both streaming/non-streaming paths,
        # carries prompt/completion_tokens at end.
        llm_span = None
        if self.telemetry_engine is not None:
            try:
                llm_span = self.telemetry_engine.start_span(
                    "llm.call",
                    attributes={"model": model, "node": node.label, "stream": stream},
                )
            except Exception as e:
                logger.warning("telemetry start_span llm.call failed: %s", e)
        llm_status = "ok"
        try:
            if stream:
                content_parts: list[str] = []
                tool_calls: list[dict] = []
                usage: dict = {}
                resp_model = model
                current_tool_calls: dict[int, dict] = {}
                finish_reason: str | None = None

                async for chunk in self.llm_gateway.chat_stream(
                    messages=messages,
                    model=model,
                    capability=capability,
                    tools=tools_schema if tools_schema else None,
                    max_tokens=node.max_tokens,
                    temperature=node.temperature,
                    effort=effort or None,
                    tool_choice=node.tool_choice or None,
                ):
                    delta_content = chunk.get("delta_content", "")
                    delta_tool_calls = chunk.get("delta_tool_calls")
                    finish_reason = chunk.get("finish_reason")

                    # Gateway reports errors as an error chunk (no usable client/stream
                    # timeout etc.) and this used to be silently dropped — SSE only saw
                    # think""/done with no ERROR event. Raise explicitly so the unified
                    # except path shows the client the failure reason.
                    if chunk.get("error"):
                        raise RuntimeError(f"chat_stream error: {chunk['error']}")

                    if delta_content:
                        content_parts.append(delta_content)
                        yield AgentEvent(
                            type=AgentEventType.TOKEN,
                            content=delta_content,
                            node_id=node.label,
                        )

                    # N9 #348: thinking channel — separate event type so GUIs
                    # can toggle visibility without losing the answer stream.
                    delta_reasoning = chunk.get("delta_reasoning") or ""
                    if delta_reasoning and self._thinking_visible(ctx, node):
                        yield AgentEvent(
                            type=AgentEventType.THINKING_TOKEN,
                            content=delta_reasoning,
                            node_id=node.label,
                            metadata={"channel": "thinking"},
                        )

                    if delta_tool_calls:
                        for dtc in delta_tool_calls:
                            idx = dtc.get("index", 0)
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {
                                    "id": dtc.get("id", f"call_{idx}"),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            tc = current_tool_calls[idx]
                            if dtc.get("id"):
                                tc["id"] = dtc["id"]
                            func_delta = dtc.get("function", {})
                            if func_delta.get("name"):
                                tc["function"]["name"] += func_delta["name"]
                            if func_delta.get("arguments"):
                                tc["function"]["arguments"] += func_delta["arguments"]

                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if finish_reason:
                        if chunk.get("model"):
                            resp_model = chunk["model"]

                content = "".join(content_parts)
                tool_calls = list(current_tool_calls.values()) if current_tool_calls else []
                for tc in tool_calls:
                    args_str = tc.get("function", {}).get("arguments", "")
                    if args_str:
                        try:
                            json.loads(args_str)
                        except json.JSONDecodeError:
                            logger.warning(
                                "streaming tool_use incomplete arguments for %s, "
                                "falling back to non-streaming",
                                tc.get("function", {}).get("name", "?"),
                            )
                            try:
                                gw_resp = await asyncio.wait_for(
                                    self.llm_gateway.chat(
                                        messages=messages,
                                        model=model,
                                        capability=capability,
                                        tools=tools_schema if tools_schema else None,
                                        max_tokens=node.max_tokens,
                                        temperature=node.temperature,
                                        effort=effort or None,
                                    ),
                                    timeout=_llm_call_timeout(),
                                )
                                content = gw_resp.content or ""
                                tool_calls = gw_resp.tool_calls or []
                                if gw_resp.usage:
                                    usage = gw_resp.usage
                                if gw_resp.model:
                                    resp_model = gw_resp.model
                            except Exception as fallback_exc:
                                logger.error(
                                    "fallback non-streaming also failed: %s",
                                    fallback_exc,
                                )
            else:
                logger.debug("LLM call via gateway, model=%s", model)
                gw_resp = await asyncio.wait_for(
                    self.llm_gateway.chat(
                        messages=messages,
                        model=model,
                        capability=capability,
                        tools=tools_schema if tools_schema else None,
                        max_tokens=node.max_tokens,
                        temperature=node.temperature,
                        effort=effort or None,
                        tool_choice=node.tool_choice or None,
                    ),
                    timeout=_llm_call_timeout(),
                )
                if gw_resp.finish_reason == "error" and gw_resp.usage.get("error"):
                    raise RuntimeError(gw_resp.usage["error"])
                content = gw_resp.content
                tool_calls = gw_resp.tool_calls or []
                usage = gw_resp.usage
                resp_model = gw_resp.model or model
        except Exception as e:
            # #617: str(asyncio.TimeoutError()) is empty — render the timeout
            # explicitly instead of the useless "LLM call failed: ".
            if isinstance(e, asyncio.TimeoutError):
                ctx.error = (
                    f"LLM call failed: timeout after {_llm_call_timeout():.0f}s "
                    "(asyncio.TimeoutError)"
                )
            else:
                ctx.error = f"LLM call failed: {e}"
            llm_status = "error"
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return

        # C13: end llm.call span — counter+latency+token usage recorded as attributes.
        if llm_span is not None and self.telemetry_engine is not None:
            try:
                if isinstance(usage, dict):
                    llm_span.attributes["prompt_tokens"] = usage.get("prompt_tokens", 0)
                    llm_span.attributes["completion_tokens"] = usage.get("completion_tokens", 0)
                    # #483: reasoning split rides the span attributes so the
                    # telemetry counter picks it up in end_span.
                    _details = usage.get("completion_tokens_details")
                    llm_span.attributes["reasoning_tokens"] = usage.get("reasoning_tokens", 0) or (
                        _details.get("reasoning_tokens", 0) if isinstance(_details, dict) else 0
                    )
                    # #541: prefix-cache hit rides the span attributes when
                    # the serving engine reports prompt_tokens_details.
                    _ptd = usage.get("prompt_tokens_details")
                    _cached = _ptd.get("cached_tokens", 0) or 0 if isinstance(_ptd, dict) else 0
                    _prompt = usage.get("prompt_tokens", 0) or 0
                    if _cached:
                        llm_span.attributes["prefix_cache_hit_tokens"] = _cached
                        llm_span.attributes["prefix_cache_hit_rate"] = (
                            round(_cached / _prompt, 4) if _prompt else 0.0
                        )
                self.telemetry_engine.end_span(llm_span.span_id, status=llm_status)
            except Exception as e:
                logger.warning("telemetry end_span llm.call failed: %s", e)

        # #541: low prefix-cache hit rate on a long session means the stable
        # head is being churned (or the engine cache is cold/evicted). Log a
        # warning so the operator can check the prompt-ordering contract.
        if isinstance(usage, dict):
            _ptd = usage.get("prompt_tokens_details")
            _cached = _ptd.get("cached_tokens", 0) or 0 if isinstance(_ptd, dict) else 0
            _prompt = usage.get("prompt_tokens", 0) or 0
            if _prompt >= 4096 and len(messages) > 3 and _cached / _prompt < 0.5:
                logger.warning(
                    "low prefix-cache hit rate: %d/%d tokens (%.0f%%) node=%s model=%s messages=%d — check prompt ordering contract (#541)",
                    _cached,
                    _prompt,
                    100.0 * _cached / _prompt,
                    node.label or "?",
                    model,
                    len(messages),
                )

        # #683: fusion-security guardrails output filter — secret leakage /
        # tool-invocation text / exfil patterns. On block the model output is
        # replaced with a redaction notice; the turn itself completes.
        if guardrails_enabled() and content:
            go = await check_llm_output(content)
            if go.get("action") == "block":
                logger.warning(
                    "guardrails output block node=%s category=%s",
                    node.label,
                    go.get("category"),
                )
                content = REDACTION_NOTICE

        if schema_validator and content:
            extracted = schema_validator.extract_from_text(content)
            if extracted:
                errors = schema_validator.validate(extracted)
                if not errors:
                    content = json.dumps(extracted, ensure_ascii=False)
                    ctx.variables.set("structured_output", extracted)
                else:
                    logger.warning("Structured output schema validation failed: %s", errors)
            else:
                logger.warning("Structured output: could not extract JSON from LLM response")

        ctx.add_message("assistant", content, tool_calls=tool_calls or None, usage=usage or None)
        # N0 LLMFunnel: metering + llm_after lifecycle + LLM_AFTER hook.
        self.llm_funnel.post_llm(
            ctx, node.label or "", model, usage=usage or {}, content=content or ""
        )

        # WF-2: truncation detection — unclosed artifact tags
        if content and self.artifact_manager:
            unclosed = self._detect_unclosed_artifacts(content)
            if unclosed:
                bp = self._extract_breakpoint(content)
                for aid in unclosed:
                    ctx.add_message(
                        "system",
                        f"[Truncation Recovery] artifact(id:{aid}) output interrupted. "
                        f"Breakpoint: {bp}. Continue from breakpoint — do NOT regenerate from scratch.",
                    )
                logger.warning(
                    "WF-2 truncation detected: unclosed_artifacts=%s breakpoint='%s'",
                    unclosed,
                    bp[:80],
                )

        if usage.get("prompt_tokens") or usage.get("completion_tokens"):
            if ctx.messages:
                last_msg = ctx.messages[-1]
                if isinstance(last_msg, dict):
                    last_msg["usage"] = usage

        # #561: stop_reason semantics per LLM hop. Documented set:
        #   tool_use   — model requested tool calls (auto-continue feeds back)
        #   max_tokens — finish_reason=="length", truncated output
        #   end_turn   — model finished its turn without tool calls
        # pause_turn is stamped on SAFETY_APPROVAL pending events
        # (_runtime_safety.py); terminal reasons live on the END event.
        if stream:
            _finish = finish_reason or ""
        else:
            _finish = str(getattr(gw_resp, "finish_reason", "") or "")
        if tool_calls:
            think_stop = "tool_use"
        elif _finish == "length":
            think_stop = "max_tokens"
        else:
            think_stop = "end_turn"
        logger.info(
            "llm hop stop_reason=%s tool_calls=%d finish=%s node=%s",
            think_stop,
            len(tool_calls),
            _finish or "none",
            node.label,
        )

        yield AgentEvent(
            type=AgentEventType.THINK,
            content=content,
            node_id=node.label,
            metadata={
                "model": resp_model,
                "temperature": node.temperature,
                "usage": usage,
                "stop_reason": think_stop,
            },
        )

        if tool_calls:
            ctx.tool_call_chain_count += 1
            if ctx.tool_call_chain_count > _MAX_TOOL_CALL_CHAIN:
                ctx.error = f"Tool call chain exceeded {_MAX_TOOL_CALL_CHAIN}"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            tool_errors: list[str] = []

            # C1 parallel_tool_calls: pre-parse + control-flow tool detection.
            # Parallel only when parallel_tool_calls=True and plan_mode is off
            # (sensitive gates need ordering) and no control-flow tools are present
            # (__sub_graph__/register_tool/unregister_tool/exit_plan_mode).
            # Mixed or gated -> fall back to the existing sequential loop (backward
            # compatible, zero risk).
            _control_flow_tools = {
                "__sub_graph__",
                "register_tool",
                "unregister_tool",
                "exit_plan_mode",
            }
            can_parallel = bool(node.parallel_tool_calls) and not ctx.plan_mode
            parsed_calls: list[tuple[str, dict, str]] = []
            if can_parallel:
                for tc in tool_calls:
                    try:
                        fn = tc["function"]["name"]
                        fa = json.loads(tc["function"]["arguments"])
                    except (KeyError, json.JSONDecodeError) as e:
                        ctx.error = f"Invalid tool call: {e}"
                        yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
                        return
                    if fn in _control_flow_tools:
                        can_parallel = False
                        break
                    parsed_calls.append((fn, fa, tc.get("id", "")))

            if can_parallel and parsed_calls:
                # Parallel path: emit all TOOL_CALLs first, execute via gather, emit
                # TOOL_RESULTs in input order.
                for fn, fa, _tcid in parsed_calls:
                    # #4: durable session event log — tool_call before the
                    # parallel fan-out; result pairing by call_id.
                    self._session_log(ctx, "tool_call", {"call_id": _tcid, "tool": fn, "args": fa})
                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        name=fn,
                        args=fa,
                        node_id=node.label,
                    )
                results = await asyncio.gather(
                    *(self._exec_parallel_tool(ctx, node, fn, fa) for fn, fa, _tcid in parsed_calls)
                )
                for (fn, fa, tcid), res in zip(parsed_calls, results):
                    result_str, is_error = res
                    if is_error:
                        # #623: repeated identical failures get a fallback hint.
                        result_str = self._escalate_repeat_tool_error(ctx, fn, result_str)
                    ctx.add_message("tool", result_str, tool_call_id=tcid)
                    ctx.messages[-1]["_node_id"] = node.label
                    post_event = "POST_TOOL_USE_FAILURE" if is_error else "POST_TOOL_USE"
                    await self._fire_tool_hooks(post_event, fn, fa, result_str)
                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        content=result_str,
                        name=fn,
                        node_id=node.label,
                    )
                    if is_error:
                        tool_errors.append(f"{fn}: {result_str}")
            else:
                for tc in tool_calls:
                    try:
                        func_name = tc["function"]["name"]
                        func_args = json.loads(tc["function"]["arguments"])
                    except (KeyError, json.JSONDecodeError) as e:
                        ctx.error = f"Invalid tool call: {e}"
                        yield AgentEvent(type=AgentEventType.ERROR, content=str(e))
                        return

                    # #4: durable session event log — tool_call before
                    # admission; result pairing by call_id in add_message.
                    self._session_log(
                        ctx,
                        "tool_call",
                        {"call_id": tc.get("id", ""), "tool": func_name, "args": func_args},
                    )

                    # C6 plan-as-mode: gate write tools during read-only explore.
                    # exit_plan_mode is the transition primitive (handled below).
                    if (
                        ctx.plan_mode
                        and func_name != "exit_plan_mode"
                        and func_name not in self._plan_readonly_tools
                    ):
                        result = (
                            f"Blocked: plan_mode is active (read-only explore). "
                            f"Tool '{func_name}' writes state. Present your plan, "
                            f"then call exit_plan_mode to transition to execution."
                        )
                        logger.info(
                            "plan_mode blocked write tool=%s node=%s",
                            func_name,
                            node.label,
                        )
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                            metadata={"plan_mode_blocked": True},
                        )
                        continue

                    if func_name == "__sub_graph__":
                        async for event in self._execute_sub_graph(ctx, func_args, node):
                            yield event
                        continue

                    if func_name == "register_tool":
                        # Audit P1-3: register_tool is a write operation (registers a
                        # dynamic tool whose execute can exec subprocesses). N0 ToolFunnel
                        # brings in the registration-level gate (hooks -> permission
                        # deny-first -> safety). Dynamic tool _dyn_execute already covers
                        # execution-level L3; this is registration-level defense in depth.
                        funnel_outcome = await self.tool_funnel.admit(
                            ctx=ctx,
                            tool_name="register_tool",
                            args=func_args,
                            node_label=node.label,
                        )
                        if not funnel_outcome.allowed or funnel_outcome.needs_approval:
                            reason = funnel_outcome.reason or "denied"
                            if funnel_outcome.blocked_by == "safety":
                                result = f"SafetyGateway blocked tool call: {reason}"
                            else:
                                result = f"Blocked by {funnel_outcome.blocked_by}: {reason}"
                            logger.warning(
                                "funnel blocked register_tool node=%s gate=%s reason=%s",
                                node.label,
                                funnel_outcome.blocked_by,
                                reason,
                            )
                            ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=result,
                                name=func_name,
                                node_id=node.label,
                            )
                            continue
                        result = self._dynamic_register_tool(func_args)
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                        )
                        continue

                    if func_name == "unregister_tool":
                        # Audit P1-3: unregister_tool same as register_tool, N0 ToolFunnel
                        # brings in the registration-level gate (hooks -> permission
                        # deny-first -> safety).
                        funnel_outcome = await self.tool_funnel.admit(
                            ctx=ctx,
                            tool_name="unregister_tool",
                            args=func_args,
                            node_label=node.label,
                        )
                        if not funnel_outcome.allowed or funnel_outcome.needs_approval:
                            reason = funnel_outcome.reason or "denied"
                            if funnel_outcome.blocked_by == "safety":
                                result = f"SafetyGateway blocked tool call: {reason}"
                            else:
                                result = f"Blocked by {funnel_outcome.blocked_by}: {reason}"
                            logger.warning(
                                "funnel blocked unregister_tool node=%s gate=%s reason=%s",
                                node.label,
                                funnel_outcome.blocked_by,
                                reason,
                            )
                            ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=result,
                                name=func_name,
                                node_id=node.label,
                            )
                            continue
                        result = self._dynamic_unregister_tool(func_args)
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                        )
                        continue

                    yield AgentEvent(
                        type=AgentEventType.TOOL_CALL,
                        name=func_name,
                        args=func_args,
                        node_id=node.label,
                    )

                    # N0 ToolFunnel: single admission chain (hooks -> permission
                    # deny-first -> safety gateway backend -> budget). Replaces
                    # the scattered PRE_TOOL_USE hook fire + evaluate_action
                    # block here (audit D-2 alignment preserved via the funnel).
                    # #5 AC4 (fusion-harness): the funnel now runs the kernel
                    # ToolPipeline on every admit — the segmented single
                    # admission path. interactive=True surfaces asks from
                    # the pipeline policy stage (safety ask or #499
                    # escalation) as needs_approval with the verdict
                    # attached; the interactive flow below drives the user
                    # ask, then re-admits with the recorded decision
                    # (approved=True). Execution only happens on a pipeline
                    # allow — approving at the call site WITHOUT the phase-2
                    # re-admit would be a side door around pipeline stage 3.
                    funnel_outcome = await self.tool_funnel.admit(
                        ctx=ctx,
                        tool_name=func_name,
                        args=func_args,
                        node_label=node.label,
                        interactive=True,
                    )
                    if funnel_outcome.needs_approval:
                        escalation = getattr(funnel_outcome, "escalation", None)
                        if escalation is None:
                            self._emit_permission_ask(
                                ctx, func_name, funnel_outcome.reason, node.label
                            )
                        approval_result = (
                            funnel_outcome.safety_result
                            if funnel_outcome.safety_result is not None
                            else escalation
                        )
                        if approval_result is None:
                            # Permission ask with no approval backend wired.
                            result = (
                                f"Error: tool '{func_name}' requires approval "
                                f"(permission rule {funnel_outcome.reason}); "
                                f"no approval backend wired"
                            )
                            logger.info(
                                "funnel permission-ask no backend tool=%s node=%s",
                                func_name,
                                node.label,
                            )
                            ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=result,
                                name=func_name,
                                node_id=node.label,
                            )
                            tool_errors.append(f"{func_name}: {result}")
                            continue
                        async for evt in self._await_safety_approval(
                            ctx,
                            approval_result,
                            "tool_call",
                            node.label,
                            tool_name=func_name,
                            params=func_args,
                        ):
                            yield evt
                            if evt.type == AgentEventType.ERROR:
                                break
                        if ctx.error:
                            continue
                        # User approved — phase-2 re-admit through the
                        # pipeline with the recorded decision; a gate that
                        # flipped to deny since phase 1 still wins.
                        funnel_outcome = await self.tool_funnel.admit(
                            ctx=ctx,
                            tool_name=func_name,
                            args=func_args,
                            node_label=node.label,
                            interactive=True,
                            approved=True,
                        )
                    if not funnel_outcome.allowed or funnel_outcome.needs_approval:
                        reason = funnel_outcome.reason or "denied"
                        gate = funnel_outcome.blocked_by
                        if gate == "safety":
                            result = f"SafetyGateway blocked tool call: {reason}"
                        elif gate == "hook":
                            result = f"Blocked by hook: {reason}"
                        elif gate == "permission":
                            result = f"Permission denied tool call: {reason}"
                        else:
                            result = f"Blocked by {gate}: {reason}"
                        logger.warning(
                            "funnel blocked llm_func_call tool=%s gate=%s reason=%s",
                            func_name,
                            gate,
                            reason,
                        )
                        # #479: TOOL_DENY — organic fire point on funnel deny.
                        await self._fire_observability_hook(
                            "TOOL_DENY",
                            {
                                "tool_name": func_name,
                                "gate": gate,
                                "reason": reason,
                                "node_id": node.label,
                            },
                        )
                        if gate == "safety":
                            yield AgentEvent(
                                type=AgentEventType.SAFETY_APPROVAL,
                                content=reason,
                                metadata={"action": "blocked", "category": "tool_call"},
                                node_id=node.label,
                            )
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=result,
                            name=func_name,
                            node_id=node.label,
                        )
                        continue
                    if funnel_outcome.safety_result is not None:
                        yield AgentEvent(
                            type=AgentEventType.SAFETY_APPROVAL,
                            content=funnel_outcome.reason or "approved",
                            metadata={"action": "approved", "category": "tool_call"},
                            node_id=node.label,
                        )
                    if funnel_outcome.args != func_args:
                        func_args = funnel_outcome.args
                        logger.info(
                            "funnel hook rewrote args tool=%s node=%s",
                            func_name,
                            node.label,
                        )

                    # C13: tool.call span — counter+latency. Initialized before the try so
                    # the except stays safe when KeyError (tool missing) raises before start_span.
                    tool_span = None
                    try:
                        tool = self.tools.get(func_name)
                        if tool is None:
                            raise KeyError(func_name)
                        validated_args = self._validate_tool_args(tool, func_args)
                        validated_args = self._merge_tool_config_defaults(
                            func_name, validated_args, ctx
                        )
                        if self.telemetry_engine is not None:
                            try:
                                tool_span = self.telemetry_engine.start_span(
                                    "tool.call",
                                    attributes={"tool": func_name, "node": node.label},
                                )
                            except Exception as e:
                                logger.warning("telemetry start_span tool.call failed: %s", e)
                        result = await tool.execute(**validated_args)
                        if tool_span is not None and self.telemetry_engine is not None:
                            try:
                                self.telemetry_engine.end_span(tool_span.span_id, status="ok")
                            except Exception as e:
                                logger.warning("telemetry end_span tool.call failed: %s", e)
                    except KeyError:
                        result = f"Error: Tool '{func_name}' not found"
                        if tool_span is not None and self.telemetry_engine is not None:
                            try:
                                self.telemetry_engine.end_span(tool_span.span_id, status="error")
                            except Exception as e:
                                logger.warning("telemetry end_span tool.call failed: %s", e)
                    except Exception as e:
                        result = f"Error: {e}"
                        if tool_span is not None and self.telemetry_engine is not None:
                            try:
                                self.telemetry_engine.end_span(tool_span.span_id, status="error")
                            except Exception as e:
                                logger.warning("telemetry end_span tool.call failed: %s", e)

                    # C6: detect exit_plan_mode sentinel -> flip plan_mode off.
                    # The tool returns the sentinel prefix + plan content; we strip
                    # the sentinel so the stored message is the clean plan text.
                    if (
                        func_name == "exit_plan_mode"
                        and isinstance(result, str)
                        and result.startswith(EXIT_PLAN_MODE_SENTINEL)
                    ):
                        plan_text = result[len(EXIT_PLAN_MODE_SENTINEL) :]
                        ctx.plan_mode = False
                        result = f"Plan approved. Transitioning to execution.\n{plan_text}"
                        logger.info(
                            "exit_plan_mode: plan_mode->False node=%s plan_len=%d",
                            node.label,
                            len(plan_text),
                        )
                        ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                        ctx.messages[-1]["_node_id"] = node.label
                        yield AgentEvent(
                            type=AgentEventType.PLAN_MODE_EXIT,
                            content=plan_text,
                            name="exit_plan_mode",
                            node_id=node.label,
                            metadata={"plan_mode": False},
                        )
                        continue

                    if isinstance(result, str) and result.startswith("Error:"):
                        # #623: repeated identical failures get a fallback hint.
                        result = self._escalate_repeat_tool_error(ctx, func_name, result)
                    ctx.add_message("tool", result, tool_call_id=tc.get("id", ""))
                    ctx.messages[-1]["_node_id"] = node.label

                    post_event = (
                        "POST_TOOL_USE_FAILURE"
                        if str(result).startswith("Error:")
                        else "POST_TOOL_USE"
                    )
                    await self._fire_tool_hooks(post_event, func_name, func_args, str(result))

                    yield AgentEvent(
                        type=AgentEventType.TOOL_RESULT,
                        content=str(result),
                        name=func_name,
                        node_id=node.label,
                    )

                    if str(result).startswith("Error:"):
                        tool_errors.append(f"{func_name}: {result}")

            # Audit L-2: stop_on_tool_error on the LLM function-call path. The direct tool
            # node path (2082) sets ctx.error + emits ERROR + returns to stop the cascade
            # when a tool errors and graph.stop_on_tool_error=True; but this LLM path only
            # stuffed the error into tool_errors and kept going through retry_on_error,
            # never honoring stop_on_tool_error — the node-level switch silently no-oped
            # on the LLM-driven path.
            if tool_errors and getattr(graph, "stop_on_tool_error", False):
                ctx.error = f"Tool errors in LLM node '{node.label}': " + "; ".join(tool_errors)
                logger.warning(
                    "llm_func_call errors stop cascade (stop_on_tool_error=True) node=%s errors=%s",
                    node.label,
                    "; ".join(tool_errors)[:200],
                )
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    content=ctx.error,
                    node_id=node.label,
                    metadata={"tool_error": True, "node": node.label},
                )
                return

            if tool_errors and node.retry_on_error and node.max_retries > 0:
                max_retries = min(node.max_retries, 5)
                for retry_count in range(1, max_retries + 1):
                    logger.info(
                        "Self-repair retry %d/%d for node=%s",
                        retry_count,
                        max_retries,
                        node.label,
                    )
                    yield AgentEvent(
                        type=AgentEventType.RETRY,
                        content=f"Retrying due to tool errors (attempt {retry_count}/{max_retries})",
                        metadata={"retry_count": retry_count, "errors": tool_errors},
                        node_id=node.label,
                    )

                    retry_prompt = (
                        "[Self-repair] The previous tool calls failed:\n"
                        + "\n".join(f"- {e}" for e in tool_errors)
                        + "\n\nPlease try again with corrected arguments or a different approach."
                    )
                    ctx.add_message("system", retry_prompt)

                    # Audit D-4: the self-repair path's LLM call originally bypassed
                    # safety_gateway entirely — the main LLM node path (1055) goes through
                    # category=llm_call, but the self-heal retry called the gateway bare and
                    # L3 content checks silently no-oped here. Add the gate, matching the
                    # main path.
                    if self.safety_gateway:
                        safety_result = self.safety_gateway.evaluate_action(
                            category="llm_call",
                            content=retry_prompt,
                            context=f"model={model} node={node.label} path=self_repair",
                        )
                        if (
                            safety_result.action.value == "block"
                            and not safety_result.requires_approval
                        ):
                            ctx.error = f"SafetyGateway blocked self-repair LLM call: {safety_result.reason}"
                            yield AgentEvent(
                                type=AgentEventType.SAFETY_APPROVAL,
                                content=safety_result.reason,
                                metadata={
                                    "action": "blocked",
                                    "category": "llm_call",
                                    "path": "self_repair",
                                },
                                node_id=node.label,
                            )
                            yield AgentEvent(
                                type=AgentEventType.ERROR, content=ctx.error, node_id=node.label
                            )
                            return
                        # #499: unified approval chokepoint (policy-aware).
                        async for evt in self._await_safety_approval(
                            ctx, safety_result, "llm_call", node.label
                        ):
                            yield evt
                            if evt.type == AgentEventType.ERROR:
                                return

                    # Audit P-2: self-repair retry is a separate LLM call and
                    # must pass the same LLMFunnel (budget -> hooks ->
                    # compaction), not dial the gateway raw.
                    retry_messages = messages + ctx.messages[-_MAX_RETRY_CONTEXT_MESSAGES:]
                    proceed, block_reason, retry_messages = await self.llm_funnel.pre_llm(
                        ctx, node.label or "", model, retry_messages
                    )
                    if not proceed:
                        ctx.error = block_reason
                        logger.warning(
                            "llm_funnel blocked self-repair node=%s reason=%s",
                            node.label,
                            block_reason,
                        )
                        yield AgentEvent(
                            type=AgentEventType.ERROR, content=block_reason, node_id=node.label
                        )
                        return
                    try:
                        gw_resp = await asyncio.wait_for(
                            self.llm_gateway.chat(
                                messages=retry_messages,
                                model=model,
                                capability=node.tool_params.get("capability", ""),
                                tools=tools_schema if tools_schema else None,
                                max_tokens=node.max_tokens,
                                temperature=node.temperature,
                                effort=node.effort or None,
                                tool_choice=node.tool_choice or None,
                            ),
                            timeout=_llm_call_timeout(),
                        )
                    except Exception as e:
                        logger.warning(
                            "Self-repair LLM call failed on retry %d: %s",
                            retry_count,
                            e,
                        )
                        continue

                    retry_content = gw_resp.content
                    retry_tool_calls = gw_resp.tool_calls or []
                    self.llm_funnel.post_llm(
                        ctx,
                        node.label or "",
                        model,
                        usage=getattr(gw_resp, "usage", None) or {},
                        content=retry_content or "",
                    )
                    ctx.add_message("assistant", retry_content, tool_calls=retry_tool_calls or None)

                    if not retry_tool_calls:
                        yield AgentEvent(
                            type=AgentEventType.RETRY_SUCCESS,
                            content=f"Self-repair succeeded on attempt {retry_count} (no more tool calls)",
                            metadata={"retry_count": retry_count},
                            node_id=node.label,
                        )
                        break

                    tool_errors = []
                    for tc in retry_tool_calls:
                        try:
                            fn = tc["function"]["name"]
                            fa = json.loads(tc["function"]["arguments"])
                        except (KeyError, json.JSONDecodeError):
                            continue

                        yield AgentEvent(
                            type=AgentEventType.TOOL_CALL,
                            name=fn,
                            args=fa,
                            node_id=node.label,
                        )
                        # Audit D-4: the self-repair retry's tool execution originally bypassed
                        # plan_mode gating and safety_gateway L3 — the main LLM path's plan_mode
                        # block (1350) and D-2's tool_call gate never applied here. Fill the gap:
                        # block write tools during plan_mode read-only, and route L3 content
                        # checks through the tool_call category.
                        if (
                            ctx.plan_mode
                            and fn != "exit_plan_mode"
                            and fn not in self._plan_readonly_tools
                        ):
                            r = (
                                "Blocked: plan_mode active (read-only explore). "
                                "Tool writes state; call exit_plan_mode first."
                            )
                            logger.info(
                                "plan_mode blocked self-repair tool=%s node=%s",
                                fn,
                                node.label,
                            )
                            ctx.add_message("tool", r, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=r,
                                name=fn,
                                node_id=node.label,
                                metadata={"plan_mode_blocked": True},
                            )
                            tool_errors.append(f"{fn}: {r}")
                            continue
                        # #475: self-repair retry goes through the ToolFunnel unified
                        # admission chain (hooks -> permission deny-first -> safety ->
                        # budget), isomorphic with the main LLM path; no longer inlines
                        # evaluate_action.
                        funnel_outcome = await self.tool_funnel.admit(
                            ctx=ctx,
                            tool_name=fn,
                            args=fa,
                            node_label=node.label,
                        )
                        # #499: confirmation-policy escalation (see main
                        # tool-call funnel site above).
                        escalation = (
                            self._policy_escalation_verdict(ctx, fn, fa)
                            if funnel_outcome.allowed and not funnel_outcome.needs_approval
                            else None
                        )
                        if funnel_outcome.needs_approval or escalation is not None:
                            if funnel_outcome.needs_approval:
                                self._emit_permission_ask(
                                    ctx, fn, funnel_outcome.reason, node.label
                                )
                            approval_result = (
                                funnel_outcome.safety_result
                                if funnel_outcome.safety_result is not None
                                else escalation
                            )
                            if approval_result is not None:
                                async for evt in self._await_safety_approval(
                                    ctx,
                                    approval_result,
                                    "tool_call",
                                    node.label,
                                    tool_name=fn,
                                    params=fa,
                                ):
                                    yield evt
                                    if evt.type == AgentEventType.ERROR:
                                        return
                            else:
                                r = (
                                    f"Error: tool '{fn}' requires approval "
                                    f"(permission rule {funnel_outcome.reason}); "
                                    f"no approval backend wired"
                                )
                                logger.info(
                                    "funnel permission-ask no backend self_repair tool=%s node=%s",
                                    fn,
                                    node.label,
                                )
                                ctx.add_message("tool", r, tool_call_id=tc.get("id", ""))
                                yield AgentEvent(
                                    type=AgentEventType.TOOL_RESULT,
                                    content=r,
                                    name=fn,
                                    node_id=node.label,
                                )
                                tool_errors.append(f"{fn}: {r}")
                                continue
                        elif not funnel_outcome.allowed:
                            reason = funnel_outcome.reason or "denied"
                            gate = funnel_outcome.blocked_by
                            if gate == "safety":
                                r = f"SafetyGateway blocked tool call: {reason}"
                            elif gate == "hook":
                                r = f"Blocked by hook: {reason}"
                            elif gate == "permission":
                                r = f"Permission denied tool call: {reason}"
                            else:
                                r = f"Blocked by {gate}: {reason}"
                            logger.warning(
                                "funnel blocked self_repair tool=%s gate=%s reason=%s",
                                fn,
                                gate,
                                reason,
                            )
                            # #479: TOOL_DENY — organic fire point on funnel deny.
                            await self._fire_observability_hook(
                                "TOOL_DENY",
                                {
                                    "tool_name": fn,
                                    "gate": gate,
                                    "reason": reason,
                                    "node_id": node.label,
                                    "path": "self_repair",
                                },
                            )
                            if gate == "safety":
                                yield AgentEvent(
                                    type=AgentEventType.SAFETY_APPROVAL,
                                    content=reason,
                                    metadata={
                                        "action": "blocked",
                                        "category": "tool_call",
                                        "path": "self_repair",
                                    },
                                    node_id=node.label,
                                )
                            ctx.add_message("tool", r, tool_call_id=tc.get("id", ""))
                            yield AgentEvent(
                                type=AgentEventType.TOOL_RESULT,
                                content=r,
                                name=fn,
                                node_id=node.label,
                            )
                            tool_errors.append(f"{fn}: {r}")
                            continue
                        else:
                            if funnel_outcome.safety_result is not None:
                                yield AgentEvent(
                                    type=AgentEventType.SAFETY_APPROVAL,
                                    content=funnel_outcome.reason or "approved",
                                    metadata={
                                        "action": "approved",
                                        "category": "tool_call",
                                        "path": "self_repair",
                                    },
                                    node_id=node.label,
                                )
                            if funnel_outcome.args != fa:
                                fa = funnel_outcome.args
                                logger.info(
                                    "funnel hook rewrote args self_repair tool=%s node=%s",
                                    fn,
                                    node.label,
                                )
                        try:
                            t = self.tools.get(fn)
                            if t is None:
                                raise KeyError(fn)
                            r = await t.execute(**self._validate_tool_args(t, fa))
                        except Exception as e:
                            r = f"Error: {e}"

                        ctx.add_message("tool", str(r), tool_call_id=tc.get("id", ""))
                        yield AgentEvent(
                            type=AgentEventType.TOOL_RESULT,
                            content=str(r),
                            name=fn,
                            node_id=node.label,
                        )
                        if str(r).startswith("Error:"):
                            tool_errors.append(f"{fn}: {r}")

                    if not tool_errors:
                        yield AgentEvent(
                            type=AgentEventType.RETRY_SUCCESS,
                            content=f"Self-repair succeeded on attempt {retry_count}",
                            metadata={"retry_count": retry_count},
                            node_id=node.label,
                        )
                        break

    async def _exec_parallel_tool(
        self, ctx: AgentContext, node: NodeConfig, func_name: str, func_args: dict
    ) -> tuple[str, bool]:
        # C1 parallel tool execution: arg validation + config defaults + execute,
        # returns (result_str, is_error).
        # Audit E-3/P0-3: the old parallel path skipped the PRE_TOOL_USE hook and
        # SafetyGateway L3 content checks — parallel_tool_calls=True could bypass the
        # shell/network/write gates, an RCE vector. Now aligned with the sequential path
        # (llm_func_call 1434-1497):
        #   1. PRE_TOOL_USE hook — a block decision returns the blocked result directly
        #   2. SafetyGateway evaluate_action(category=tool_call) — content matching
        #      dangerous patterns (rm -rf / / DROP TABLE) -> block; L2/L3 needing
        #      approval -> fail-closed (parallel cannot yield the approval flow),
        #      returning error so the LLM falls back to the sequential approval path.
        # N0 ToolFunnel: parallel admission goes through the same funnel chain
        # (hooks -> permission -> safety). Parallel cannot pause for approval,
        # so needs_approval degrades to a sequential-retry error (unchanged).
        # Audit P-1: parallel admission MUST carry the real node ctx. Passing
        # ctx=None silently disabled the plan gate (ctx.plan_mode) and every
        # role-scoped rule (ctx.role) on this path.
        funnel_outcome = await self.tool_funnel.admit(
            ctx=ctx,
            tool_name=func_name,
            args=func_args,
            node_label=node.label,
        )
        if funnel_outcome.needs_approval:
            self._emit_permission_ask(ctx, func_name, funnel_outcome.reason, node.label)
            return (
                f"Error: tool '{func_name}' requires approval; "
                f"parallel path cannot pause for approval, retry sequentially",
                True,
            )
        # #499: confirmation-policy escalation can't pause here either —
        # degrade the same way; the sequential retry carries the ask.
        if (
            funnel_outcome.allowed
            and self._policy_escalation_verdict(ctx, func_name, func_args) is not None
        ):
            return (
                f"Error: confirmation policy requires approval for '{func_name}'; "
                f"parallel path cannot pause for approval, retry sequentially",
                True,
            )
        if not funnel_outcome.allowed:
            reason = funnel_outcome.reason or "denied"
            if funnel_outcome.blocked_by == "safety":
                blocked = f"SafetyGateway blocked parallel tool: {reason}"
            elif funnel_outcome.blocked_by == "hook":
                blocked = f"Blocked by hook: {reason}"
            else:
                blocked = f"Blocked by {funnel_outcome.blocked_by}: {reason}"
            logger.warning(
                "parallel tool blocked gate=%s tool=%s reason=%s",
                funnel_outcome.blocked_by,
                func_name,
                reason,
            )
            # #479: TOOL_DENY — organic fire point on funnel deny.
            await self._fire_observability_hook(
                "TOOL_DENY",
                {
                    "tool_name": func_name,
                    "gate": funnel_outcome.blocked_by,
                    "reason": reason,
                    "node_id": node.label,
                    "path": "parallel",
                },
            )
            return blocked, True
        if funnel_outcome.args != func_args:
            func_args = funnel_outcome.args
        try:
            tool = self.tools.get(func_name)
            if tool is None:
                raise KeyError(func_name)
            validated_args = self._validate_tool_args(tool, func_args)
            validated_args = self._merge_tool_config_defaults(func_name, validated_args)
            result = await tool.execute(**validated_args)
        except KeyError:
            result = f"Error: Tool '{func_name}' not found"
        except Exception as e:
            result = f"Error: {e}"
        result_str = str(result)
        is_error = result_str.startswith("Error:")
        if is_error:
            logger.warning(
                "parallel tool error tool=%s node=%s err=%s",
                func_name,
                node.label,
                result_str[:120],
            )
        return result_str, is_error

    def _emit_permission_ask(self, ctx, tool_name: str, reason: str, node_label: str) -> None:
        # #479: permission ask emits a lifecycle event at ask time so
        # subscribers (WS bridge / audit replay) see the ask, not just the
        # eventual deny/approve. Notify-only — security is enforced by the
        # funnel, not here.
        try:
            self.tool_funnel.lifecycle.emit(
                "tool",
                "permission_ask",
                {"tool_name": tool_name, "reason": reason, "node_id": node_label},
                session_id=getattr(ctx, "session_id", ""),
            )
        except Exception as e:
            logger.warning("permission_ask lifecycle emit failed tool=%s err=%s", tool_name, e)

    async def _fire_tool_hooks(
        self, event: str, tool_name: str, args: dict, result: str | None = None
    ):
        if self.hooks is None:
            return None
        payload = {"tool_name": tool_name, "args": args}
        if result is not None:
            payload["result"] = result
        try:
            return await self.hooks.fire(event, payload, tool_name=tool_name)
        except Exception as e:
            logger.warning("hook fire error event=%s tool=%s err=%s", event, tool_name, e)
            return None

    async def _execute_tool_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """Execute a standalone tool node."""
        if node.tool_name == "__sub_graph__":
            async for event in self._execute_sub_graph(ctx, node.tool_params, node):
                yield event
            return

        # Defense: direct calls (tests/error_handler delegation) may not have ctx.variables seeded.
        self._seed_ctx_variables(ctx)
        params = {}
        # Audit E-4/P0-4: interpolation -> terminal command injection. With `{{user_input}}`
        # wired straight into terminal `command`, attacker-controlled graph input becomes a
        # shell command (curl evil|sh / cat /etc/passwd / scp private keys), bypassing the
        # catastrophic blacklist. Fail-closed here: terminal commands containing variable
        # interpolation are always blocked; explicit opt-in via FUSION_TERMINAL_ALLOW_INTERP=1
        # (controlled CI). Escaping (shlex.quote) still misses multi-command semantic
        # injection, so a hard block is safer.
        if node.tool_name == "terminal":
            _allow_interp = os.environ.get("FUSION_TERMINAL_ALLOW_INTERP", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
            _cmd_template = node.tool_params.get("command", "")
            if isinstance(_cmd_template, str) and "{{" in _cmd_template and not _allow_interp:
                ctx.error = (
                    "Blocked: terminal 'command' contains variable interpolation "
                    "({{...}}); passing user-controlled input to a shell is an RCE "
                    "vector. Use a non-shell tool, or set FUSION_TERMINAL_ALLOW_INTERP=1 "
                    "for controlled environments."
                )
                logger.warning("E-4 blocked terminal command interpolation node=%s", node.label)
                yield AgentEvent(
                    type=AgentEventType.ERROR,
                    content=ctx.error,
                    node_id=node.label,
                    metadata={"e4_blocked": True},
                )
                return
        for k, v in node.tool_params.items():
            if isinstance(v, str):
                params[k] = ctx.variables.interpolate(v)
            else:
                params[k] = v

        # C6 plan-as-mode: gate write tools on the standalone tool-node path.
        if (
            ctx.plan_mode
            and node.tool_name not in self._plan_readonly_tools
            and node.tool_name != "exit_plan_mode"
        ):
            ctx.error = (
                f"Blocked: plan_mode active, tool '{node.tool_name}' writes state. "
                f"Call exit_plan_mode to transition to execution."
            )
            logger.info(
                "plan_mode blocked tool-node tool=%s node=%s",
                node.tool_name,
                node.label,
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=ctx.error,
                node_id=node.label,
                metadata={"plan_mode_blocked": True},
            )
            return

        # N0 ToolFunnel: tool-node admission via the single chain (hooks ->
        # permission deny-first -> safety backend -> budget). Event contract
        # preserved: SAFETY_APPROVAL blocked/approved, approval future flow.
        # #5 AC4 (fusion-harness): the funnel runs the kernel ToolPipeline on
        # every admit — interactive=True surfaces asks from the pipeline
        # policy stage (safety ask or #499 escalation) as needs_approval
        # with the verdict attached; the interactive flow below drives the
        # user ask, then re-admits with the recorded decision (approved=
        # True). Execution only happens on a pipeline allow — no side door
        # around pipeline stage 3.
        # #4: durable session event log — deterministic call_id pairs with
        # the add_message("tool", ..., tool_call_id=f"tool_{...}") below.
        self._session_log(
            ctx,
            "tool_call",
            {"call_id": f"tool_{node.tool_name}", "tool": node.tool_name, "args": params},
        )
        funnel_outcome = await self.tool_funnel.admit(
            ctx=ctx,
            tool_name=node.tool_name,
            args=params,
            node_label=node.label,
            interactive=True,
        )
        if funnel_outcome.needs_approval:
            escalation = getattr(funnel_outcome, "escalation", None)
            if escalation is None:
                self._emit_permission_ask(ctx, node.tool_name, funnel_outcome.reason, node.label)
            approval_result = (
                funnel_outcome.safety_result
                if funnel_outcome.safety_result is not None
                else escalation
            )
            if approval_result is None:
                # Unresolved ask — no approval backend wired. Fail closed.
                ctx.error = (
                    f"Permission denied tool call: tool '{node.tool_name}' requires "
                    f"approval ({funnel_outcome.reason}); no approval backend wired"
                )
                logger.info("funnel permission-ask no backend tool-node=%s", node.label)
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return
            async for evt in self._await_safety_approval(
                ctx,
                approval_result,
                "tool_call",
                node.label,
                tool_name=node.tool_name,
                params=params,
            ):
                yield evt
                if evt.type == AgentEventType.ERROR:
                    return
            if ctx.error:
                return
            # User approved — phase-2 re-admit through the pipeline with the
            # recorded decision; a gate that flipped to deny since phase 1
            # still wins.
            funnel_outcome = await self.tool_funnel.admit(
                ctx=ctx,
                tool_name=node.tool_name,
                args=params,
                node_label=node.label,
                interactive=True,
                approved=True,
            )
        if not funnel_outcome.allowed or funnel_outcome.needs_approval:
            reason = funnel_outcome.reason or "denied"
            gate = funnel_outcome.blocked_by
            if gate == "safety":
                ctx.error = f"SafetyGateway blocked tool call: {reason}"
            elif gate == "hook":
                ctx.error = f"Blocked by hook: {reason}"
            elif gate == "permission":
                ctx.error = f"Permission denied tool call: {reason}"
            else:
                ctx.error = f"Blocked by {gate}: {reason}"
            logger.warning(
                "funnel blocked tool-node tool=%s gate=%s reason=%s",
                node.tool_name,
                gate,
                reason,
            )
            # #479: TOOL_DENY — organic fire point on funnel deny.
            await self._fire_observability_hook(
                "TOOL_DENY",
                {
                    "tool_name": node.tool_name,
                    "gate": gate,
                    "reason": reason,
                    "node_id": node.label,
                    "path": "tool_node",
                },
            )
            if gate == "safety":
                yield AgentEvent(
                    type=AgentEventType.SAFETY_APPROVAL,
                    content=reason,
                    metadata={"action": "blocked", "category": "tool_call"},
                )
            yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
            return
        if funnel_outcome.safety_result is not None:
            yield AgentEvent(
                type=AgentEventType.SAFETY_APPROVAL,
                content=funnel_outcome.reason or "approved",
                metadata={"action": "approved", "category": "tool_call"},
            )
        if funnel_outcome.args != params:
            params = funnel_outcome.args
            logger.info("funnel hook rewrote tool-node args node=%s", node.label)

        try:
            tool = self.tools.get(node.tool_name)
            params = self._merge_tool_config_defaults(node.tool_name, params, ctx)
            result = await tool.execute(**params)
        except KeyError:
            result = f"Error: Tool '{node.tool_name}' not found"
        except Exception as e:
            result = f"Error: {e}"

        ctx.add_message("tool", str(result), tool_call_id=f"tool_{node.tool_name}")
        ctx.messages[-1]["_node_id"] = node.label

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=str(result),
            name=node.tool_name,
            node_id=node.label,
        )

        # #202: direct tool node error detection. Matches the LLM-driven tool
        # path's "Error:" prefix convention (line ~1461) AND the common tool
        # pattern of returning json.dumps({"error": ...}). When the graph opts
        # in via stop_on_tool_error, a tool error stops the cascade: set
        # ctx.error, emit a tagged ERROR event, and return BEFORE applying
        # output_mapping — so a downstream gate can't misread a stray value
        # (e.g. missing key -> None -> "false" -> wrong branch). Default off
        # keeps existing error-as-result behavior for graphs that handle it.
        is_tool_error = False
        error_detail = ""
        if isinstance(result, str):
            if result.startswith("Error:"):
                is_tool_error = True
                error_detail = result
            else:
                stripped = result.strip()
                if stripped.startswith("{"):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict) and "error" in parsed:
                            is_tool_error = True
                            error_detail = json.dumps(parsed, ensure_ascii=False)
                    except (ValueError, TypeError):
                        pass
        if is_tool_error and getattr(graph, "stop_on_tool_error", False):
            ctx.error = f"Tool '{node.tool_name}' (node '{node.label}') failed: {error_detail}"
            logger.warning(
                "tool node error stops cascade (stop_on_tool_error=True) tool=%s node=%s error=%s",
                node.tool_name,
                node.label,
                error_detail[:200],
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=ctx.error,
                name=node.tool_name,
                node_id=node.label,
                metadata={
                    "tool_error": True,
                    "tool": node.tool_name,
                    "node": node.label,
                },
            )
            return

        output_mapping = node.tool_params.get("output_mapping", {})
        if output_mapping:
            self._apply_tool_output_mapping(output_mapping, result, node.label, ctx)

        # #284: optional post-action screen capture + frame-diff assertion.
        # Default off (no behavior change). When node.post_action_capture is
        # set, capture a frame after the tool runs and, if an assertion fn is
        # registered on the runtime, run it. Capture failure is non-fatal.
        if getattr(node, "post_action_capture", False):
            async for evt in self._post_action_assert(ctx, node, result):
                yield evt

    def _apply_tool_output_mapping(
        self, output_mapping: dict, result: Any, node_label: str, ctx: AgentContext
    ) -> None:
        """Write the tool return value into variables per output_mapping, for downstream
        nodes' {{ var }} references.

        Convention {source_key: target_var}, consistent with sub_graph output_mapping:
        - source_key "" or "result": write the whole value into target_var
        - otherwise: try json.loads(result) and take source_key; on parse failure fall
          back to the whole value and log a warning
        """
        parsed = None
        for source_key, target_var in output_mapping.items():
            if not target_var:
                continue
            if source_key in ("", "result"):
                ctx.variables.set(target_var, result)
                continue
            if parsed is None and isinstance(result, str):
                try:
                    parsed = json.loads(result)
                except (ValueError, TypeError):
                    logger.warning(
                        "output_mapping: node=%s result not JSON, fallback to whole value into %s",
                        node_label,
                        target_var,
                    )
            if isinstance(parsed, dict) and source_key in parsed:
                ctx.variables.set(target_var, parsed[source_key])
            else:
                ctx.variables.set(target_var, result)

    async def _post_action_assert(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        tool_result: Any,
    ) -> AsyncIterator[AgentEvent]:
        """#284: capture a post-action frame and run the registered assertion fn.

        Capture is best-effort (ScreenCaptureTool). If a post_action_assertion_fn
        is registered on the runtime, call it with (ctx, node, tool_result,
        frame_b64, w, h) -> str; a non-empty return is an assertion failure
        emitted as a tagged event. No fn registered = capture-only. Capture
        failure is non-fatal (logs + skip).
        """
        frame_b64 = ""
        w = 0
        h = 0
        try:
            cap = self.tools.get("screenshot")
            raw = await cap.execute()
            # ScreenCaptureTool returns JSON {"path":..,"width":..,"height":..}
            # or "Error: ..."; parse defensively.
            if isinstance(raw, str) and raw.strip().startswith("{"):
                try:
                    parsed = json.loads(raw)
                    w = int(parsed.get("width", 0))
                    h = int(parsed.get("height", 0))
                    p = parsed.get("path", "")
                    if p:
                        import base64

                        with open(p, "rb") as fh:
                            frame_b64 = base64.b64encode(fh.read()).decode()
                except (ValueError, TypeError, OSError) as e:
                    logger.warning("post_action capture parse failed: %s", e)
            elif isinstance(raw, str) and raw.startswith("Error:"):
                logger.warning("post_action capture error: %s", raw[:200])
        except Exception as e:
            logger.warning("post_action capture skipped: %s", e)

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=frame_b64[:80] if frame_b64 else "captured",
            name="post_action_capture",
            node_id=node.label,
            metadata={
                "post_action_capture": True,
                "width": w,
                "height": h,
                "has_frame": bool(frame_b64),
            },
        )

        assertion_fn = getattr(self, "post_action_assertion_fn", None)
        if assertion_fn is None:
            return
        assertion_spec = getattr(node, "assertion", {}) or {}
        if not assertion_spec:
            return
        try:
            verdict = assertion_fn(ctx, node, tool_result, frame_b64, w, h)
        except Exception as e:
            logger.exception("post_action assertion fn raised: %s", e)
            verdict = f"assertion fn error: {e}"
        if verdict:
            logger.warning(
                "post_action assertion FAILED node=%s tool=%s: %s",
                node.label,
                node.tool_name,
                verdict[:200],
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"Post-action assertion failed: {verdict}",
                name="post_action_assertion",
                node_id=node.label,
                metadata={
                    "assertion_failed": True,
                    "tool": node.tool_name,
                    "spec": assertion_spec,
                },
            )

    def _execute_condition_node(self, ctx: AgentContext, node: NodeConfig) -> AgentEvent:
        """Evaluate a condition node using the condition engine."""
        expr = ctx.variables.interpolate(node.condition_expr)
        try:
            result = self.condition_engine.evaluate(expr, ctx, ctx.variables)
        except Exception as e:
            logger.warning("Condition evaluation failed: %s -> %s", expr, e)
            result = "false"

        return AgentEvent(
            type=AgentEventType.THINK,
            content=result,
            node_id=node.label,
            metadata={"condition": node.condition_expr, "result": result},
        )

    def _execute_loop_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AgentEvent:
        """Execute a loop node — check if loop should continue."""
        # Defense: direct calls (tests/parallel branches) may not have ctx.variables seeded.
        self._seed_ctx_variables(ctx)
        max_iter = node.max_iterations
        loop_var = node.tool_params.get("loop_var", "loop_count")
        current = ctx.variables.get(loop_var, 0)
        try:
            current = int(current)
        except (ValueError, TypeError):
            current = ctx.iteration_count

        if current < max_iter:
            ctx.variables.set(loop_var, current + 1)
            action = "loop_continue"
        else:
            action = "loop_exit"

        return AgentEvent(
            type=AgentEventType.THINK,
            content=action,
            node_id=node.label,
            metadata={
                "max_iterations": max_iter,
                "current": current,
                "action": action,
                # #556: FlowState snapshot for the variable inspector
                "flow_state": self._flow_state_snapshot(ctx, graph),
            },
        )

    async def _execute_iteration_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """#556: run the embedded sub-graph once per item, sequentially.

        tool_params: items_var (ctx variable holding the list), output_var,
        max_items (visible-failure cap), plus __sub_graph__ params
        (graph_json / input_mapping / output_mapping). `item` and
        `item_index` are set in ctx.variables before each pass so
        input_mapping can thread the item into the sub-graph.
        """
        self._seed_ctx_variables(ctx)
        params = node.tool_params or {}
        items_var = params.get("items_var", "items")
        max_items = int(params.get("max_items", 100))
        output_var = params.get("output_var") or node.label or "iteration_results"
        items = ctx.variables.get(items_var)
        if items is None or items == "":
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"iteration: items variable '{items_var}' not found",
                node_id=node.label,
                metadata={"items_var": items_var},
            )
            return
        if not isinstance(items, list):
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"iteration: variable '{items_var}' is {type(items).__name__}, expected list",
                node_id=node.label,
                metadata={"items_var": items_var},
            )
            return
        if len(items) > max_items:
            # #556: cap breach fails visibly — no silent truncation, no hang
            logger.warning(
                "iteration cap breach node=%s items=%d max_items=%d",
                node.label,
                len(items),
                max_items,
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=(
                    f"iteration: {len(items)} items exceeds cap {max_items} (node '{node.label}')"
                ),
                node_id=node.label,
                metadata={"items": len(items), "max_items": max_items, "cap_breach": True},
            )
            return

        results: list = []
        for idx, item in enumerate(items):
            ctx.variables.set("item", item)
            ctx.variables.set("item_index", idx)
            final = ""
            last_think = ""
            errored = False
            async for ev in self._execute_sub_graph(ctx, params, node):
                if ev.type == AgentEventType.RESULT:
                    # sub events carry a "[sub:<name>] " prefix — strip it
                    content = ev.content or ""
                    if content.startswith("[sub:"):
                        content = content.split("] ", 1)[-1]
                    final = content
                elif ev.type == AgentEventType.THINK and ev.content:
                    last_think = ev.content
                yield ev
                if ev.type == AgentEventType.ERROR:
                    errored = True
            if not final and last_think:
                # sub-graph with no explicit RESULT node: use its last LLM
                # THINK output (the sub's de-facto answer)
                content = last_think
                if content.startswith("[sub:"):
                    content = content.split("] ", 1)[-1]
                final = content
            if errored:
                # stop on first failing item — partial results kept, visible
                ctx.variables.set(output_var, results)
                logger.info(
                    "iteration aborted at item %d node=%s (%d completed)",
                    idx,
                    node.label,
                    len(results),
                )
                return
            results.append(final)
            ctx.variables.set(output_var, results)
        logger.info(
            "iteration done node=%s items=%d output=%s", node.label, len(results), output_var
        )
        yield AgentEvent(
            type=AgentEventType.RESULT,
            content=json.dumps(results, ensure_ascii=False),
            node_id=node.label,
            metadata={
                "items": len(results),
                "output_var": output_var,
                "flow_state": self._flow_state_snapshot(ctx, graph),
            },
        )

    async def _execute_variable_aggregator_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """#556: merge variable(s) into one output (list or dict) — the
        gather counterpart of iteration / parallel branches."""
        self._seed_ctx_variables(ctx)
        params = node.tool_params or {}
        input_vars = params.get("input_vars") or []
        output_var = params.get("output_var") or node.label or "aggregated"
        if not input_vars:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content="variable_aggregator: tool_params.input_vars is empty",
                node_id=node.label,
            )
            return
        if len(input_vars) == 1:
            merged = ctx.variables.get(input_vars[0])
            if merged is None:
                merged = []
        else:
            merged = {v: ctx.variables.get(v) for v in input_vars}
        ctx.variables.set(output_var, merged)
        logger.info(
            "variable_aggregator node=%s vars=%s output=%s", node.label, input_vars, output_var
        )
        yield AgentEvent(
            type=AgentEventType.RESULT,
            content=json.dumps(merged, ensure_ascii=False, default=str),
            node_id=node.label,
            metadata={
                "aggregated_vars": input_vars,
                "output_var": output_var,
                "flow_state": self._flow_state_snapshot(ctx, graph),
            },
        )

    def _flow_state_snapshot(self, ctx: AgentContext, graph: AgentGraph) -> dict:
        """#556: FlowState = keys declared at the start node
        (tool_params.flow_state), snapshotted from ctx.variables."""
        start = graph.get_node(graph.start_node_id) if graph.start_node_id else None
        keys = (start.tool_params.get("flow_state") or []) if start else []
        if not keys:
            return {}
        return {k: ctx.variables.get(k) for k in keys if ctx.variables.get(k) is not None}

    async def _execute_human_input_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """#557/#756: pause the run for a human decision.

        Queue mechanics run on fusion_harness.HumanGate (persistent JSON
        queue, single-winner resolve, lazy expiry). Same process: emit a
        SAFETY_APPROVAL request (kind=human_input) and await the gate
        question resolved via resolve_human_input() (approve with value /
        reject with reason). Across a backend restart: the checkpoint saved
        before pausing + resume_from_checkpoint(human_inputs={...})
        re-executes this node, which sees the seeded input variable and
        continues; the gate question itself persists and expires lazily.
        Rejection terminates the branch visibly (ERROR event with the
        reason).
        """
        self._seed_ctx_variables(ctx)
        params = node.tool_params or {}
        input_var = params.get("input_var") or node.label or "human_input"
        prompt = str(params.get("prompt", ""))
        fields = params.get("fields", [])
        try:
            timeout = float(params.get("timeout_seconds", 300))
        except (TypeError, ValueError):
            timeout = 300.0

        existing = ctx.variables.get(input_var)
        if existing not in (None, ""):
            # resume path: the decision value was injected before re-run
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content=json.dumps({input_var: existing}, ensure_ascii=False, default=str),
                node_id=node.label,
                metadata={
                    "input_var": input_var,
                    "resumed": True,
                    "flow_state": self._flow_state_snapshot(ctx, graph),
                },
            )
            return

        # persist FIRST — the checkpoint survives a backend restart
        await self._save_checkpoint(ctx, graph)
        gate = self._get_human_gate()
        question = gate.ask(
            session_id=ctx.session_id,
            question=prompt,
            payload={"input_var": input_var, "fields": fields, "node": node.label},
            reply_schema={
                "required": ["approved"],
                "properties": {"approved": "bool", "value": "str", "reason": "str"},
            },
            policy="block",
            requester="agent-runtime",
            ttl_s=timeout,
        )
        input_id = question.id
        logger.info(
            "human_input pause node=%s input_id=%s input_var=%s", node.label, input_id, input_var
        )
        # the question is queued BEFORE emitting the request — a consumer that
        # sees the SAFETY_APPROVAL event must be able to resolve immediately
        yield AgentEvent(
            type=AgentEventType.SAFETY_APPROVAL,
            content=prompt,
            node_id=node.label,
            metadata={
                "kind": "human_input",
                "input_id": input_id,
                "input_var": input_var,
                "prompt": prompt,
                "fields": fields,
                "action": "request",
            },
        )
        try:
            decision = await asyncio.wait_for(gate.wait(input_id), timeout=timeout)
        except asyncio.TimeoutError:
            # pause-and-persist: the stream ends here; resume via checkpoint
            logger.info("human_input timeout — paused node=%s input_id=%s", node.label, input_id)
            yield AgentEvent(
                type=AgentEventType.CHECKPOINT,
                content=f"Paused awaiting human input at '{node.label}': {prompt}",
                node_id=node.label,
                metadata={
                    "paused": True,
                    "input_id": input_id,
                    "input_var": input_var,
                    "prompt": prompt,
                    "fields": fields,
                    "flow_state": self._flow_state_snapshot(ctx, graph),
                },
            )
            return

        if not isinstance(decision, dict):
            decision = {"approved": bool(decision), "value": ""}
        if decision.get("approved") is False:
            reason = str(decision.get("reason") or "rejected by human")
            logger.info("human_input rejected node=%s reason=%s", node.label, reason)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"human_input rejected at '{node.label}': {reason}",
                node_id=node.label,
                metadata={
                    "kind": "human_input",
                    "input_id": input_id,
                    "rejected": True,
                    "reason": reason,
                },
            )
            return
        value = decision.get("value", decision.get("input", ""))
        ctx.variables.set(input_var, value)
        yield AgentEvent(
            type=AgentEventType.RESULT,
            content=json.dumps({input_var: value}, ensure_ascii=False, default=str),
            node_id=node.label,
            metadata={
                "input_var": input_var,
                "approved": True,
                "flow_state": self._flow_state_snapshot(ctx, graph),
            },
        )

    def resolve_human_input(self, input_id: str, decision: dict) -> bool:
        """#557/#756: resolve a pending human_input question via HumanGate.

        Single-winner: a second resolve (or unknown/expired id) returns
        False instead of raising — the gate reports {"resolved": False,
        "reason": ...} and the rejection is logged."""
        try:
            gate = self._get_human_gate()
        except Exception as exc:
            logger.error("human gate unavailable for resolve: %s", exc)
            return False
        result = gate.resolve(input_id, decision)
        if not result.get("resolved"):
            logger.info(
                "human_input resolve rejected input_id=%s reason=%s",
                input_id,
                result.get("reason"),
            )
            return False
        logger.info(
            "human_input resolved input_id=%s approved=%s", input_id, decision.get("approved")
        )
        return True

    def pending_human_inputs(self) -> list[dict]:
        """#557/#756: pending human_input questions from the HumanGate
        queue (for RPC polling). done is always False for pending rows;
        answered/expired/cancelled questions never appear."""
        try:
            gate = self._get_human_gate()
        except Exception as exc:
            logger.error("human gate unavailable for listing: %s", exc)
            return []
        return [
            {
                "input_id": q.id,
                "done": False,
                "question": q.question,
                "input_var": (q.payload or {}).get("input_var", ""),
                "session_id": q.session_id,
            }
            for q in gate.list_questions(status="pending")
        ]

    async def _execute_sub_flow_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """#557: flow-as-tool — run a saved flow (by flow_id) as a node.

        Loads the graph from the store (agent graphs) and delegates to the
        audited _execute_sub_graph path (depth gates, input/output mapping).
        Values pass in via input_mapping (parent var -> sub 'input'/var) and
        out via output_mapping (sub var -> parent var).
        """
        params = node.tool_params or {}
        flow_id = str(params.get("flow_id", ""))
        if not flow_id:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content="sub_flow: tool_params.flow_id is required",
                node_id=node.label,
            )
            return
        if not self.store:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"sub_flow: no store configured to load flow '{flow_id}'",
                node_id=node.label,
            )
            return
        sub = self.store.load_graph(flow_id)
        if sub is None:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"sub_flow: flow '{flow_id}' not found",
                node_id=node.label,
            )
            return
        sub_params = dict(params)
        sub_params["graph_json"] = sub.to_json()
        logger.info("sub_flow node=%s flow=%s", node.label, flow_id)
        async for ev in self._execute_sub_graph(ctx, sub_params, node):
            yield ev

    async def _execute_error_handler_node(
        self, ctx: AgentContext, node: NodeConfig, graph: AgentGraph
    ) -> AsyncIterator[AgentEvent]:
        """Execute an error handler — re-execute the failed node with retries."""
        max_retries = node.max_retries
        retry_delay = node.retry_delay

        failed_node_id = ""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                break
            if isinstance(msg, dict) and msg.get("role") == "tool":
                failed_node_id = msg.get("_node_id", "")
                if failed_node_id:
                    break
                tool_call_id = msg.get("tool_call_id", "")
                if tool_call_id.startswith("tool_"):
                    failed_node_id = tool_call_id[5:]
                    break

        if not failed_node_id:
            failed_node_id = ctx.current_node_id

        error_msg = ctx.error
        ctx.error = ""

        for attempt in range(1, max_retries + 1):
            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"Retry attempt {attempt}/{max_retries} for error: {error_msg}",
                node_id=node.label,
                metadata={
                    "attempt": attempt,
                    "max_retries": max_retries,
                    "error": error_msg,
                },
            )

            if attempt > 1:
                await asyncio.sleep(retry_delay)

            failed_node = graph.get_node(failed_node_id)
            if failed_node:
                if failed_node.type == "tool":
                    async for event in self._execute_tool_node(ctx, failed_node, graph):
                        yield event
                elif failed_node.type == "llm":
                    model = graph.find_llm_model()
                    tools_schema = self.tools.to_openai_schemas(role=getattr(ctx, "role", "all"))
                    if any(n.allow_dynamic_tools for n in graph.nodes.values()):
                        tools_schema.extend(self._dynamic_tool_schemas())
                    async for event in self._execute_llm_node(
                        ctx, failed_node, graph, model, tools_schema, ""
                    ):
                        yield event
                elif failed_node.type == "rag":
                    # Audit P1-16: the original error_handler only retried tool/llm; after a
                    # rag/planner/verify failure there was no retry path -> gave up directly.
                    # Add those three.
                    model = graph.find_llm_model()
                    tools_schema = self.tools.to_openai_schemas(role=getattr(ctx, "role", "all"))
                    async for event in self._execute_rag_node(
                        ctx, failed_node, graph, model, tools_schema, ""
                    ):
                        yield event
                elif failed_node.type == "planner":
                    async for event in self._execute_planner_node(ctx, failed_node, graph):
                        yield event
                elif failed_node.type == "verify":
                    async for event in self._execute_verify_node(ctx, failed_node, graph):
                        yield event
                elif failed_node.type == "parallel":
                    async for event in self._execute_parallel_node(
                        ctx, failed_node, graph, failed_node_id
                    ):
                        yield event
                elif failed_node.type == "acp":
                    # #496: ACP node retryable like rag/planner/verify.
                    async for event in self._execute_acp_node(ctx, failed_node, graph):
                        yield event
                else:
                    logger.warning(
                        "error_handler: failed node %s type=%s not retryable",
                        failed_node_id,
                        failed_node.type,
                    )

            if not ctx.error:
                break
            logger.warning("Retry %d/%d still has error: %s", attempt, max_retries, ctx.error)

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=f"Error handler completed after {attempt} attempt(s)",
            node_id=node.label,
        )

    async def _execute_parallel_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        current_node_id: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        # C5: true fan-out/gather for parallel graph nodes. All outgoing edges = N parallel
        # branches; each branch runs an independent sub-runtime over the sub-graph from its
        # branch target to the merge point, concurrent via asyncio.gather, results merged
        # into the parent ctx in edge order.
        yield AgentEvent(
            type=AgentEventType.START,
            content=f"Parallel fan-out from {current_node_id}",
            node_id=current_node_id,
        )

        outgoing = graph.get_outgoing_edges(current_node_id)
        if not outgoing:
            logger.warning("parallel node %s has no outgoing edges", current_node_id)
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content="parallel: no branches",
                node_id=current_node_id,
                metadata={"next_id": ""},
            )
            return

        # plan_mode active: read-only exploration, no fan-out writes, fall back to the first edge.
        if ctx.plan_mode:
            first_target = outgoing[0].target_id
            logger.info(
                "parallel node %s plan_mode active, fallback first edge -> %s",
                current_node_id,
                first_target,
            )
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content=f"parallel: plan_mode sequential fallback to {first_target}",
                node_id=current_node_id,
                metadata={"next_id": first_target},
            )
            return

        # Single outgoing edge: no fan-out needed; take the first edge (zero behavior change).
        if len(outgoing) == 1:
            only_target = outgoing[0].target_id
            yield AgentEvent(
                type=AgentEventType.RESULT,
                content=f"parallel: single branch to {only_target}",
                node_id=current_node_id,
                metadata={"next_id": only_target},
            )
            return

        # Find the merge point (fan-in target): among the intersection of the reachable
        # node sets from each branch target, the common successor closest to the parallel
        # node. If all branches share one successor that is the merge point; if none is
        # found, each branch runs to its own end with no explicit merge.
        merge_node_id = self._find_merge_node(graph, outgoing)
        logger.info(
            "parallel node %s branches=%d merge=%s",
            current_node_id,
            len(outgoing),
            merge_node_id or "(none)",
        )

        branch_targets = [e.target_id for e in outgoing]

        async def run_branch(
            branch_idx: int, branch_target: str, edge_label: str
        ) -> tuple[int, str, list[AgentEvent], str]:
            # Build the branch sub-graph: nodes reachable from branch_target, excluding merge_node.
            sub_graph = self._build_branch_subgraph(
                graph, branch_target, merge_node_id, current_node_id
            )
            if sub_graph is None:
                return (
                    branch_idx,
                    edge_label,
                    [],
                    f"Error: branch {branch_idx} subgraph build failed",
                )

            from .runtime import AgentRuntime

            sub_runtime = AgentRuntime(
                tool_registry=self.tools,
                max_iterations=self.max_iterations,
                variables=VariableManager(),
                llm_gateway=self.llm_gateway,
                safety_gateway=self.safety_gateway,
                store=self.store,
                memory_engine=self.memory_engine,
                telemetry_engine=self.telemetry_engine,
                # Audit P1-17: parallel sub-runtimes originally got no artifact_manager, so
                # artifact-aware context/compaction triggers all failed inside branches
                # (parent had it, child did not).
                artifact_manager=self.artifact_manager,
            )
            # Audit D-3: the sub-runtime inherits the parent runtime's plan_mode; otherwise
            # while the parent is in read-only exploration parallel branches can still call
            # write tools and bypass the gate.
            # Audit A-1 Tier2: plan_mode is per-exec, passed via sub_ctx; the sub-runtime
            # dispatch (context provided) inherits it instead of writing the sub_runtime
            # singleton.
            sub_ctx = AgentContext()
            sub_ctx.plan_mode = ctx.plan_mode
            # Audit P1-6: sub_ctx inherits the parent session_id/agent_id; otherwise branch
            # trajectory/checkpoint/memory all attach to a nonexistent session and the parent
            # cannot correlate them.
            sub_ctx.session_id = ctx.session_id
            sub_ctx.agent_id = ctx.agent_id
            sub_ctx.metadata["parallel_branch"] = edge_label or f"branch_{branch_idx}"
            # Audit P0-3: parallel sub-runtimes inherit the parent's default token budget.
            sub_runtime._default_token_budget = self._default_token_budget
            # Audit P1-7: parallel sub-runtimes inherit parent depth +1 (same as sub-graph),
            # keeping the recursive parallel depth count continuous to bound fan-out nesting.
            sub_runtime._sub_graph_depth = self._sub_graph_depth + 1
            branch_events: list[AgentEvent] = []
            async for event in sub_runtime.execute_graph(sub_graph, "", sub_ctx):
                branch_events.append(event)

            # Extract the branch's final output (last assistant content or THINK event).
            branch_output = ""
            for ev in reversed(branch_events):
                if ev.type == AgentEventType.THINK and ev.content:
                    branch_output = ev.content
                    break
            if not branch_output:
                for msg in reversed(sub_ctx.messages):
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        branch_output = msg.get("content", "")
                        break
            logger.info(
                "parallel branch %d (%s) done, output_len=%d events=%d",
                branch_idx,
                edge_label or f"branch_{branch_idx}",
                len(branch_output),
                len(branch_events),
            )
            return branch_idx, edge_label, branch_events, branch_output

        # Audit P3-3: throttle wide fan-out. When FUSION_PARALLEL_BRANCH_CONCURRENCY=N, use
        # a semaphore to cap concurrent branches (default 0 = unlimited; bounded by the
        # P2-15 branch cap and P1-4 LLM semaphore as backstops).
        branch_limit = _parallel_branch_concurrency()
        branch_sem = asyncio.Semaphore(branch_limit) if branch_limit > 0 else None

        async def _throttled_branch(i, tgt, label):
            if branch_sem is None:
                return await run_branch(i, tgt, label)
            async with branch_sem:
                return await run_branch(i, tgt, label)

        tasks = [
            _throttled_branch(i, tgt, e.label)
            for i, (e, tgt) in enumerate(zip(outgoing, branch_targets))
        ]
        # Audit P1-8/A-5: return_exceptions=True prevents one branch's exception from
        # aborting the whole parallel node (the original gather re-raised by default and
        # lost all other branches' results). The merge loop processes failed branches in
        # order, degrades them to Error output, and never aborts healthy branches.
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Yield branch events in edge order (tagged with [parallel:label]).
        merged_outputs: list[str] = []
        for r in sorted(results, key=lambda x: x[0] if isinstance(x, tuple) else 0):
            if isinstance(r, BaseException):
                logger.error("parallel branch failed: %s", r)
                merged_outputs.append(f"[parallel_error]\nError: branch failed: {r}")
                continue
            branch_idx, edge_label, branch_events, branch_output = r
            tag = edge_label or f"branch_{branch_idx}"
            for ev in branch_events:
                if ev.type in (AgentEventType.START, AgentEventType.END):
                    continue
                yield AgentEvent(
                    type=ev.type,
                    content=f"[parallel:{tag}] {ev.content}",
                    name=ev.name,
                    args=ev.args,
                    node_id=ev.node_id,
                    metadata=ev.metadata,
                )
            if branch_output:
                merged_outputs.append(f"[{tag}]\n{branch_output}")

        # Merge: concatenate branch outputs into the parent ctx as an assistant message.
        if merged_outputs:
            merged_text = "\n\n".join(merged_outputs)
            ctx.add_message("assistant", merged_text)
            logger.info(
                "parallel node %s merged %d branches, merged_len=%d",
                current_node_id,
                len(merged_outputs),
                len(merged_text),
            )

        yield AgentEvent(
            type=AgentEventType.RESULT,
            content=f"Parallel fan-out complete: {len(results)} branches merged",
            node_id=current_node_id,
            metadata={"next_id": merge_node_id or ""},
        )

    def _find_merge_node(self, graph: AgentGraph, outgoing: list) -> str:
        # Find the fan-in merge point: among nodes common to every branch target's reachable
        # set (excluding itself), pick one reachable from all branches. With multiple
        # candidates, take the first common node in branch 0's reachability order.
        if len(outgoing) < 2:
            return ""

        def reachable_from(start_id: str) -> tuple[set[str], list[str]]:
            # Returns (reachable set, BFS discovery order) — the list keeps traversal deterministic.
            visited: set[str] = set()
            order: list[str] = []
            queue = [start_id]
            while queue:
                cur = queue.pop(0)
                if cur in visited:
                    continue
                visited.add(cur)
                order.append(cur)
                for e in graph.get_outgoing_edges(cur):
                    queue.append(e.target_id)
            return visited, order

        branch_targets = [e.target_id for e in outgoing]
        reachable_sets = [reachable_from(t)[0] for t in branch_targets]
        # Common reachable = nodes every branch can reach.
        common = reachable_sets[0]
        for s in reachable_sets[1:]:
            common = common & s
        # Exclude the branch target itself (the merge point should come after the branches).
        common = common - set(branch_targets)
        if not common:
            return ""
        # Take branch 0's first common BFS-discovered node (nearest merge point, deterministic).
        _, order = reachable_from(branch_targets[0])
        for nid in order:
            if nid in common:
                return nid
        return ""

    def _build_branch_subgraph(
        self,
        graph: AgentGraph,
        branch_target: str,
        merge_node_id: str,
        parallel_node_id: str,
    ) -> AgentGraph | None:
        # Build the branch sub-graph: the node subset reachable from branch_target that
        # excludes merge_node / parallel_node; start_node_id = branch_target; the end node
        # reuses the graph's end (without a merge_node, branches run to the original end).
        if branch_target not in graph.nodes:
            return None

        excluded = {merge_node_id, parallel_node_id} if merge_node_id else {parallel_node_id}

        # BFS-collect branch-reachable nodes (stop at merge_node, do not cross).
        branch_nodes: set[str] = set()
        queue = [branch_target]
        while queue:
            cur = queue.pop(0)
            if cur in branch_nodes or cur in excluded:
                continue
            branch_nodes.add(cur)
            for e in graph.get_outgoing_edges(cur):
                if e.target_id not in excluded:
                    queue.append(e.target_id)

        if not branch_nodes:
            return None

        sub = AgentGraph(
            name=f"{graph.name}:branch:{branch_target}",
            start_node_id=branch_target,
        )
        # Copy branch nodes; keep a start-type branch entry as start, otherwise leave types alone.
        for nid in branch_nodes:
            sub.add_node(nid, graph.nodes[nid])
        # Copy edges within the branch (source+target both in the branch set, not merge_node).
        for e in graph.edges:
            if e.source_id in branch_nodes and e.target_id in branch_nodes:
                sub.add_edge(e.source_id, e.target_id, e.label)
        return sub

    async def _execute_sub_graph(
        self,
        ctx: AgentContext,
        params: dict,
        parent_node: NodeConfig,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a sub-graph node."""
        graph_json = params.get("graph_json", "")
        input_mapping = params.get("input_mapping", {})
        output_mapping = params.get("output_mapping", {})

        # Audit E-16/P0-4: recursion depth gate — block over the limit (cycles crash the process).
        max_depth = _max_sub_graph_depth()
        if self._sub_graph_depth >= max_depth:
            logger.warning(
                "E-16 sub-graph depth limit hit depth=%d max=%d node=%s",
                self._sub_graph_depth,
                max_depth,
                parent_node.label,
            )
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=(
                    f"Sub-graph recursion depth limit reached ({self._sub_graph_depth} >= "
                    f"{max_depth}). Possible circular sub-graph reference. "
                    f"Set FUSION_SUB_GRAPH_MAX_DEPTH higher if intentional."
                ),
                node_id=parent_node.label,
                metadata={"e16_depth": self._sub_graph_depth, "e16_max": max_depth},
            )
            return

        if not graph_json:
            yield AgentEvent(type=AgentEventType.ERROR, content="Sub-graph: no graph_json provided")
            return

        try:
            sub_graph = AgentGraph.from_json(graph_json)
        except Exception as e:
            yield AgentEvent(type=AgentEventType.ERROR, content=f"Sub-graph parse error: {e}")
            return

        # Audit A-2/A-1 Tier3: parent variables read from ctx.variables (per-exec parent
        # space), never touching singleton self.variables. input_mapping resolves to
        # sub_input/sub_vars.
        # Defense: direct _execute_sub_graph calls (tests/parallel branches) may have
        # unseeded ctx.variables — seed in place, same source as dispatch.
        self._seed_ctx_variables(ctx)
        sub_input = ""
        for parent_var, sub_var in input_mapping.items():
            val = ctx.variables.get(parent_var, "")
            if sub_var == "input":
                sub_input = str(val)
            else:
                ctx.variables.set(sub_var, val)

        sub_vars = VariableManager()
        sub_vars.load_from(ctx.variables.to_dict())

        from .runtime import AgentRuntime

        sub_runtime = AgentRuntime(
            tool_registry=self.tools,
            max_iterations=self.max_iterations,
            variables=sub_vars,
            llm_gateway=self.llm_gateway,
            safety_gateway=self.safety_gateway,
            store=self.store,
            memory_engine=self.memory_engine,
            telemetry_engine=self.telemetry_engine,
            # Audit P1-17: sub-graph runtimes match parallel branches — pass artifact_manager
            # to keep artifact-aware context consistent (parent-only coverage missed
            # compaction triggers).
            artifact_manager=self.artifact_manager,
        )
        # Audit D-3: the sub-runtime inherits the parent runtime's safety_gateway / store /
        # memory_engine / plan_mode; otherwise sub-graph execution runs with no safety net
        # (no L3 content checks + no read-only exploration gate + no persistence/memory)
        # and the parent graph's authorized safety policy silently lapses in the sub-graph.
        # Audit D-3: plan_mode inherited via sub_ctx (per-exec), not written to the
        # sub_runtime singleton.
        # Audit A-1 Tier2: dispatch (context provided) inherits ctx.plan_mode.
        sub_ctx = AgentContext()
        sub_ctx.plan_mode = ctx.plan_mode
        # Audit P1-6: sub_ctx inherits the parent session_id/agent_id so sub-graph execution
        # stays on the same session (trajectory/task/checkpoint chain unbroken), matching
        # the parallel-branch handling.
        sub_ctx.session_id = ctx.session_id
        sub_ctx.agent_id = ctx.agent_id
        # Audit E-16: sub-runtimes inherit parent depth +1; the count spans the sub-graph chain.
        sub_runtime._sub_graph_depth = self._sub_graph_depth + 1
        # Audit P0-3: sub-runtimes inherit the parent default budget — consistent token limits.
        sub_runtime._default_token_budget = self._default_token_budget

        # Issue #175: lifecycle hook — sub-agent start.
        await self._fire_tool_hooks(
            "SUBAGENT_START",
            "",
            {"graph_id": sub_graph.id, "graph_name": sub_graph.name},
        )
        async for event in sub_runtime.execute_graph(sub_graph, sub_input, sub_ctx):
            yield AgentEvent(
                type=event.type,
                content=f"[sub:{sub_graph.name}] {event.content}",
                name=event.name,
                args=event.args,
                node_id=event.node_id,
                metadata=event.metadata,
            )

        # Issue #175: lifecycle hook — sub-agent stop.
        await self._fire_tool_hooks(
            "SUBAGENT_STOP",
            "",
            {"graph_id": sub_graph.id, "graph_name": sub_graph.name},
        )

        # #557: read post-execution sub values from sub_ctx.variables (writes
        # land there after dispatch seeds a fresh VM), falling back to the
        # pre-exec sub_vars snapshot for vars the flow never touched.
        for sub_var, parent_var in output_mapping.items():
            val = ""
            if sub_ctx.variables is not None and sub_ctx.variables.get(sub_var, "") != "":
                val = sub_ctx.variables.get(sub_var, "")
            else:
                val = sub_vars.get(sub_var, "")
            ctx.variables.set(parent_var, val)

    def _extract_template_name(self, text: str) -> str:
        """Check if text references a prompt template like {{ template:code-review }}."""
        match = re.match(r"^\{\{\s*template:(\w[\w-]*)\s*\}\}$", text.strip())
        if match:
            return match.group(1)
        return ""

    async def _execute_rag_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
        model: str,
        tools_schema: list[dict],
        system_prompt: str,
        stream: bool = False,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a RAG node — retrieve context then generate via LLM."""
        try:
            from .rag_pipeline import RAGConfig, RAGPipeline
        except ImportError:
            yield AgentEvent(type=AgentEventType.ERROR, content="RAG pipeline not available")
            return

        query = ""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                query = msg.get("content", "")
                break
        if not query:
            query = " ".join(
                m.get("content", "")
                for m in ctx.messages
                if isinstance(m, dict) and m.get("role") == "user"
            )[:500]

        rag_config_dict = node.tool_params.get("rag_config", {})
        rag_config = RAGConfig(
            **{
                k: v
                for k, v in rag_config_dict.items()
                if k
                in (
                    "top_k",
                    "similarity_threshold",
                    "rerank",
                    "mode",
                    "scope",
                    "max_context_tokens",
                )
            }
        )

        knowledge_engine = None
        if hasattr(self, "_knowledge_engine") and self._knowledge_engine:
            knowledge_engine = self._knowledge_engine

        pipeline = RAGPipeline(knowledge_engine=knowledge_engine, gateway=self.llm_gateway)

        try:
            rag_result = pipeline.retrieve(query, config=rag_config)
        except Exception as e:
            logger.warning("RAG retrieve failed: %s", e)
            rag_result = None

        if rag_result and rag_result.context_text:
            context_block = (
                f"\n\n[Retrieved Context]\n{rag_result.context_text}\n[/Retrieved Context]\n\n"
            )
            node_prompt = node.system_prompt or system_prompt or ""
            if node_prompt:
                node_prompt = ctx.variables.interpolate(node_prompt)
                node_prompt += context_block
            else:
                node_prompt = (
                    f"Use the following context to answer the user's question.{context_block}"
                )

            augmented_node = NodeConfig(
                type="llm",
                label=node.label,
                model=node.model or model,
                system_prompt=node_prompt,
                temperature=node.temperature,
                max_tokens=node.max_tokens,
                tool_params=node.tool_params,
            )

            async for event in self._execute_llm_node(
                ctx,
                augmented_node,
                graph,
                augmented_node.model or model,
                tools_schema,
                "",
            ):
                yield event

            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"RAG retrieved {len(rag_result.documents)} docs, {len(rag_result.context_text)} chars context",
                node_id=node.label,
                metadata={
                    "rag_docs": len(rag_result.documents),
                    "rag_context_chars": len(rag_result.context_text),
                    "rag_mode": rag_config.mode,
                },
            )
        else:
            yield AgentEvent(
                type=AgentEventType.THINK,
                content="RAG: no relevant context found, proceeding without retrieval",
                node_id=node.label,
                metadata={"rag_docs": 0},
            )
            async for event in self._execute_llm_node(
                ctx, node, graph, model, tools_schema, system_prompt
            ):
                yield event

    async def _execute_planner_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
    ) -> AsyncIterator[AgentEvent]:
        """Execute a planner node — generate execution plan for user approval."""
        try:
            from .planner import PlannerEngine
        except ImportError:
            yield AgentEvent(type=AgentEventType.ERROR, content="Planner engine not available")
            return

        task = ""
        for msg in reversed(ctx.messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                task = msg.get("content", "")
                break
        if not task:
            task = node.tool_params.get("task", "unknown task")

        planner = PlannerEngine(gateway=self.llm_gateway)
        context_text = node.tool_params.get("context", "")
        target_files = node.tool_params.get("target_files")

        plan = await planner.create_plan(task, context=context_text, files=target_files)

        yield AgentEvent(
            type=AgentEventType.THINK,
            content=f"Plan generated: {plan.task} ({len(plan.steps)} steps, risk={plan.overall_risk})",
            node_id=node.label,
            metadata={
                "plan_id": plan.id,
                "steps": len(plan.steps),
                "risk": plan.overall_risk,
                "status": plan.status,
            },
        )

        ctx.variables.set("current_plan_id", plan.id)
        ctx.variables.set("plan_step_count", len(plan.steps))
        ctx.variables.set("plan_risk", plan.overall_risk)

        step_summaries = []
        for step in plan.steps:
            step_summaries.append(
                {
                    "id": step.id,
                    "description": step.description,
                    "action": step.action,
                    "target_files": step.target_files,
                    "complexity": step.estimated_complexity,
                }
            )

        yield AgentEvent(
            type=AgentEventType.TOOL_RESULT,
            content=f"Execution plan: {plan.task}\nSteps: {len(plan.steps)}\nRisk: {plan.overall_risk}",
            name="planner",
            node_id=node.label,
            metadata={
                "plan_id": plan.id,
                "steps": step_summaries,
                "requires_approval": plan.overall_risk != "low",
            },
        )

        # C6 plan-as-mode: block in-graph for approval when await_approval set.
        # The future is resolved by approve_plan_in_graph/reject_plan_in_graph,
        # called from the planner.approve_plan/reject_plan RPC handlers.
        await_approval = node.tool_params.get("await_approval", False)
        if await_approval:
            timeout = float(node.tool_params.get("approval_timeout", 300.0))
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            self._plan_futures[plan.id] = future
            # Audit A-1 Tier1: register the owning exec; reap as the exception-path safety net.
            self._register_exec_future(ctx.session_id, plan.id)
            logger.info(
                "planner node %s blocking for approval plan_id=%s timeout=%.0fs",
                node.label,
                plan.id,
                timeout,
            )
            yield AgentEvent(
                type=AgentEventType.PLAN_APPROVAL,
                content=f"Plan {plan.id} awaiting approval",
                name="planner",
                node_id=node.label,
                metadata={
                    "plan_id": plan.id,
                    "action": "pending_approval",
                    "steps": step_summaries,
                },
            )
            try:
                approved = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError:
                self._plan_futures.pop(plan.id, None)
                logger.warning("planner approval timed out plan_id=%s", plan.id)
                yield AgentEvent(
                    type=AgentEventType.PLAN_APPROVAL,
                    content=f"Approval timed out for plan {plan.id}",
                    name="planner",
                    node_id=node.label,
                    metadata={"plan_id": plan.id, "action": "timeout"},
                )
                ctx.error = f"Planner: approval timed out for plan {plan.id}"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

            self._plan_futures.pop(plan.id, None)
            if approved:
                logger.info("planner plan %s approved, proceeding", plan.id)
                yield AgentEvent(
                    type=AgentEventType.PLAN_APPROVAL,
                    content=f"Plan {plan.id} approved",
                    name="planner",
                    node_id=node.label,
                    metadata={"plan_id": plan.id, "action": "approved"},
                )
            else:
                logger.info("planner plan %s rejected, stopping", plan.id)
                yield AgentEvent(
                    type=AgentEventType.PLAN_APPROVAL,
                    content=f"Plan {plan.id} rejected",
                    name="planner",
                    node_id=node.label,
                    metadata={"plan_id": plan.id, "action": "rejected"},
                )
                ctx.error = f"Planner: plan {plan.id} rejected"
                yield AgentEvent(type=AgentEventType.ERROR, content=ctx.error)
                return

    async def _execute_verify_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
    ) -> AsyncIterator[AgentEvent]:
        try:
            from .verifier import VerificationEngine
        except ImportError:
            yield AgentEvent(type=AgentEventType.ERROR, content="Verification engine not available")
            return

        # N14 #353: resident mode — the node becomes a standing reviewer on the
        # event stream (attached in _run_with_trajectory); no one-shot verify.
        if node.tool_params.get("resident"):
            ctx.variables.set("verify_resident", True)
            logger.info("verify node %s in resident mode (standing reviewer)", node.label)
            yield AgentEvent(
                type=AgentEventType.VERIFY,
                content="Resident reviewer active: watching event stream for "
                "untraceable conclusions and graph-code mismatch",
                name="resident_reviewer",
                node_id=node.label,
                metadata={"resident": True},
            )
            return

        task = node.tool_params.get("task", "")
        if not task:
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    task = msg.get("content", "")
                    break

        output = node.tool_params.get("output", "")
        if not output:
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    output = msg.get("content", "")
                    break

        criteria = node.tool_params.get("criteria", "")
        context_text = node.tool_params.get("context", "")
        max_attempts = node.tool_params.get("max_attempts", 3)

        engine = VerificationEngine(gateway=self.llm_gateway, max_attempts=max_attempts)
        result = await engine.verify(
            task=task,
            output=output,
            criteria=criteria,
            context=context_text,
            max_attempts=max_attempts,
        )

        ctx.variables.set("verify_passed", result.passed)
        ctx.variables.set("verify_score", result.score)
        ctx.variables.set("verify_attempt", result.attempt)

        yield AgentEvent(
            type=AgentEventType.VERIFY,
            content=f"Verification {'passed' if result.passed else 'failed'} (score={result.score:.2f}, attempt={result.attempt}/{result.max_attempts})",
            name="verifier",
            node_id=node.label,
            metadata=result.to_dict(),
        )

        if not result.passed and result.issues:
            # Audit P2-19: a verify failure must set ctx.error; otherwise downstream
            # condition nodes (checking ctx.error) never route to error_handler and the
            # failure silently skips the fault-tolerance path. Do not emit ERROR (it makes
            # dispatch return early, bypassing error_handler); drive conditional routing
            # with ctx.error.
            ctx.error = f"Verify failed (score={result.score:.2f}): {'; '.join(result.issues)}"
            yield AgentEvent(
                type=AgentEventType.THINK,
                content=f"Verification issues: {'; '.join(result.issues)}",
                node_id=node.label,
                metadata={"suggestion": result.suggestion, "verify_failed": True},
            )

    async def _execute_acp_node(
        self,
        ctx: AgentContext,
        node: NodeConfig,
        graph: AgentGraph,
    ) -> AsyncIterator[AgentEvent]:
        """Execute an ACP node — run a prompt through an external agent CLI.

        #496: spawns an ACP agent subprocess (e.g. claude-code-acp), streams
        its session updates onto the graph event stream (TOKEN / THINKING_TOKEN
        / TOOL_CALL), and appends the final agent message to ctx.messages.
        Missing CLI / dead session / timeout surface as a single ERROR event.
        """
        try:
            from .acp_client import AcpRunResult, run_acp_prompt
        except ImportError:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content="agent-client-protocol not installed (pip install agent-client-protocol)",
            )
            return

        prompt = node.tool_params.get("prompt", "")
        if not prompt:
            for msg in reversed(ctx.messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    prompt = msg.get("content", "")
                    break
        if not prompt:
            prompt = node.system_prompt
        if not prompt:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content="ACP node has no prompt (no user message, tool_params.prompt, or system_prompt)",
                node_id=node.label,
            )
            return

        command = node.tool_params.get("command", "")
        args = node.tool_params.get("args") or []
        cwd = node.tool_params.get("cwd", "") or ""
        timeout = float(node.tool_params.get("timeout", 120))
        permission = node.tool_params.get("permission", "deny")
        env = node.tool_params.get("env") or None

        queue: asyncio.Queue = asyncio.Queue()

        def on_event(ev: dict) -> None:
            queue.put_nowait(ev)

        task = asyncio.create_task(
            run_acp_prompt(
                prompt,
                command=command,
                args=list(args),
                cwd=cwd,
                env=env,
                timeout=timeout,
                on_event=on_event,
                permission_policy=permission,
            )
        )
        try:
            while not task.done():
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                for out_ev in self._acp_event_to_agent_event(ev, node):
                    yield out_ev
            while not queue.empty():
                ev = queue.get_nowait()
                for out_ev in self._acp_event_to_agent_event(ev, node):
                    yield out_ev
        except asyncio.CancelledError:
            task.cancel()
            raise
        result: AcpRunResult = await task

        if not result.success:
            logger.error("acp node %s failed: %s", node.label, result.error)
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=result.error or "ACP run failed",
                node_id=node.label,
                metadata={"acp_events": len(result.events)},
            )
            return

        if result.message:
            ctx.messages.append({"role": "assistant", "content": result.message})
            ctx.variables.set("acp_message", result.message)
        ctx.variables.set("acp_stop_reason", result.stop_reason)
        yield AgentEvent(
            type=AgentEventType.THINK,
            content=f"ACP turn complete (stop_reason={result.stop_reason})",
            node_id=node.label,
            metadata={
                "acp": True,
                "stop_reason": result.stop_reason,
                "session_id": result.session_id,
                "usage": result.usage,
            },
        )

    def _acp_event_to_agent_event(self, ev: dict, node: NodeConfig) -> list[AgentEvent]:
        kind = ev.get("type", "")
        text = ev.get("text", "")
        if kind == "agent_message_chunk" and text:
            return [
                AgentEvent(
                    type=AgentEventType.TOKEN,
                    content=text,
                    node_id=node.label,
                    metadata={"acp": True},
                )
            ]
        if kind == "agent_thought_chunk" and text:
            return [
                AgentEvent(
                    type=AgentEventType.THINKING_TOKEN,
                    content=text,
                    node_id=node.label,
                    metadata={"acp": True},
                )
            ]
        if kind == "tool_call":
            return [
                AgentEvent(
                    type=AgentEventType.TOOL_CALL_START,
                    content=ev.get("title", ""),
                    name=ev.get("title", ""),
                    node_id=node.label,
                    metadata={
                        "acp": True,
                        "tool_call_id": ev.get("tool_call_id", ""),
                        "kind": ev.get("kind", ""),
                    },
                )
            ]
        if kind == "tool_call_update":
            return [
                AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    content=ev.get("title", ""),
                    name=ev.get("title", ""),
                    node_id=node.label,
                    metadata={
                        "acp": True,
                        "tool_call_id": ev.get("tool_call_id", ""),
                        "status": ev.get("status", ""),
                    },
                )
            ]
        # user_message_chunk / usage_update / plan updates — not surfaced
        # individually; usage lands in the final THINK metadata.
        return []

    async def _auto_store_memory(self, ctx: AgentContext, graph: AgentGraph) -> None:
        if not self.memory_engine:
            return
        user_msgs = [m.get("content", "") for m in ctx.messages if m.get("role") == "user"]
        assistant_msgs = [
            m.get("content", "") for m in ctx.messages if m.get("role") == "assistant"
        ]
        if not user_msgs and not assistant_msgs:
            return
        last_user = user_msgs[-1] if user_msgs else ""
        last_assistant = assistant_msgs[-1] if assistant_msgs else ""
        scope = f"graph:{graph.name}"
        content = f"Q: {last_user[:200]} A: {last_assistant[:500]}"
        # Audit A-2: LLM assistant output is an untrusted source and must not be classified
        # as "user" (that type is reserved for human input with high-priority recall).
        # _auto_store_memory includes assistant text; if classify matched "i am/i prefer"
        # into user, that is delayed injection poisoning future sessions. Forced downgrade:
        # on a user match, store as project with source="llm" (low trust) and record the
        # suspected injection pattern.
        from .memory_engine import classify_memory_type

        mem_type = classify_memory_type(content)
        llm_sourced = bool(last_assistant)
        if llm_sourced and mem_type == "user":
            mem_type = "project"
            logger.info(
                "auto-store: LLM-sourced content reclassified user->project "
                "(user type reserved for human input, A-2 injection hardening)"
            )
        # Lightweight injection suspicion detection: assistant text containing directive
        llm_injection_suspect = llm_sourced and any(
            pat in last_assistant.lower()
            for pat in ("ignore previous", "ignore all", "system:", "disregard", "you are now")
        )
        await asyncio.to_thread(
            self.memory_engine.store,
            content=content,
            scope=scope,
            tags="auto-store",
            importance=7 if not ctx.error else 3,
            metadata={
                "graph_id": graph.id,
                "error": ctx.error,
                "iterations": ctx.iteration_count,
                "source": "llm" if llm_sourced else "user",
                "injection_suspect": llm_injection_suspect,
            },
            memory_type=mem_type,
        )
        logger.info(
            "Auto-stored execution result to memory (scope=%s type=%s source=%s)",
            scope,
            mem_type,
            "llm" if llm_sourced else "user",
        )
