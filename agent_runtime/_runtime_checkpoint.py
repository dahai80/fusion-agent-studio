"""Checkpoint mixin — extracted from AgentRuntime (audit 0826 P2-4).

Holds checkpoint save + resume methods. Method bodies are verbatim moves;
AgentRuntime inherits this mixin.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from ._runtime_helpers import logger
from .context import AgentContext, AgentEvent, AgentEventType
from .graph import AgentGraph
from .variable_manager import VariableManager


class _CheckpointMixin:
    @staticmethod
    def _extract_pending_tool_calls(messages: list) -> list[dict]:
        # Audit P1-20/E-20: find trailing unclosed tool_calls — an assistant
        # message sent tool_calls with no matching tool reply after it.
        # Returns that assistant message's tool_calls list (empty = no pending).
        if not messages:
            return []
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "tool":
                # Last message is a tool reply -> closed, no pending.
                return []
            if role == "assistant" and msg.get("tool_calls"):
                # assistant tool_calls with no tool reply after -> pending.
                return msg["tool_calls"]
            if role == "assistant":
                # assistant without tool_calls -> no pending.
                return []
        return []

    async def _save_checkpoint(self, ctx: AgentContext, graph: AgentGraph) -> None:
        """Auto-save checkpoint if store is configured."""
        if not self.store:
            return
        try:
            # Audit P1-20/E-20: capture unclosed tool_calls (assistant sent
            # tool_calls but no matching tool-role reply before resume).
            # Previously only messages were stored; resuming from current_node_id
            # re-ran the tool_call causing duplicate side effects (file written
            # twice / command run twice). Now recorded explicitly and resume
            # uses it to mark and skip.
            pending = self._extract_pending_tool_calls(ctx.messages)
            self.store.save_checkpoint(
                graph_id=graph.name,
                session_id=ctx.session_id,
                node_id=ctx.current_node_id or "",
                state={
                    "messages": ctx.messages,
                    "iteration_count": ctx.iteration_count,
                    "variables": ctx.variables.to_dict(),
                    "tool_call_chain_count": ctx.tool_call_chain_count,
                    "pending_tool_calls": pending,
                },
            )
            # #675: mirror the checkpoint into the kernel CheckpointStore
            # (session-keyed, CheckpointState shape) for kernel-side
            # resume/audit. Best-effort — save failures are logged inside.
            kcp = getattr(self, "kernel_checkpoints", None)
            if kcp is not None:
                kcp.save(
                    ctx.session_id,
                    ctx.messages,
                    budget_spent_tokens=int(getattr(ctx, "tokens_used", 0) or 0),
                    permission_mode=str(getattr(ctx, "permission_mode", "") or ""),
                    metadata={
                        "graph_id": graph.name,
                        "node_id": ctx.current_node_id or "",
                        "iteration_count": ctx.iteration_count,
                    },
                )
                # #714 part 1: per-node state through the kernel-canonical
                # binding API (fusion_harness.checkpoint_binding, harness K2
                # #35) — same CheckpointStore, distinct "session:node" keys,
                # so the old session-keyed rows stay byte-compatible. The
                # node_effect_outbox (harness OutboxStore) is wired here for
                # the binder; mark_emitted receipts for tool-call dedupe land
                # with the tool-executor integration (follow-up, keeps this
                # diff confined to the #714-listed files).
                try:
                    from .paths import studio_home

                    outbox = getattr(self, "node_effect_outbox", None)
                    if outbox is None:
                        from fusion_harness.outbox import OutboxStore

                        outbox = OutboxStore(str(studio_home() / "node_effects.db"))
                        self.node_effect_outbox = outbox
                    from fusion_harness.checkpoint_binding import bind_node_state

                    binder = bind_node_state(kcp.store, outbox, ctx.session_id)
                    binder.save(
                        ctx.current_node_id or "run",
                        {
                            "graph_id": graph.name,
                            "iteration_count": ctx.iteration_count,
                            "variables": ctx.variables.to_dict(),
                            "pending_tool_calls": pending,
                        },
                    )
                except Exception as bind_err:
                    logger.warning(
                        "node state binding failed session=%s: %s", ctx.session_id, bind_err
                    )
            logger.debug(
                "Checkpoint saved: graph=%s node=%s pending_tools=%d",
                graph.name,
                ctx.current_node_id,
                len(pending),
            )
            ctx.checkpoint_fail_count = 0
            # N8 #391: checkpoint hook trigger (auto-save path).
            hooks = getattr(self, "hooks", None)
            if hooks is not None:
                try:
                    await hooks.fire(
                        "CHECKPOINT",
                        {
                            "graph": graph.name,
                            "session_id": ctx.session_id,
                            "node": ctx.current_node_id,
                            "pending_tool_calls": len(pending),
                        },
                    )
                except Exception as hook_err:
                    logger.warning("checkpoint hook error: %s", hook_err)
        except Exception as e:
            ctx.checkpoint_fail_count += 1
            # Audit M-3: persistent failures (DB full/locked/schema mismatch)
            # only warned while execution continued; resume then failed with
            # "No checkpoint found" giving the user no signal. Escalate to
            # error after 3 consecutive failures.
            # Audit P3-1: counter moved to per-exec ctx so concurrent
            # executions each accumulate without clobbering each other.
            if ctx.checkpoint_fail_count >= 3:
                logger.error(
                    "Checkpoint save failed %d consecutive times (resume will not work): %s",
                    ctx.checkpoint_fail_count,
                    e,
                )
            else:
                logger.warning("Checkpoint save failed (%d): %s", ctx.checkpoint_fail_count, e)

    async def resume_from_checkpoint(
        self,
        graph: AgentGraph,
        session_id: str,
        stream: bool = False,
        human_inputs: dict | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Resume graph execution from the latest checkpoint."""
        if not self.store:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content="No store configured for checkpoint resume",
            )
            return

        checkpoint = self.store.load_latest_checkpoint(graph_id=graph.name, session_id=session_id)
        if not checkpoint:
            yield AgentEvent(
                type=AgentEventType.ERROR,
                content=f"No checkpoint found for graph={graph.name} session={session_id}",
            )
            return

        ctx = AgentContext(session_id=session_id)
        # Audit E-20: Checkpoint is a dataclass, not a dict — use attributes
        # and parse state_json.
        raw_state = checkpoint.context_json or "{}"
        try:
            state = json.loads(raw_state) if raw_state else {}
        except (json.JSONDecodeError, TypeError):
            state = {}
        ctx.messages = state.get("messages", [])
        ctx.iteration_count = state.get("iteration_count", 0)
        ctx.current_node_id = checkpoint.current_node_id or graph.start_node_id
        ctx.tool_call_chain_count = state.get("tool_call_chain_count", 0)
        # Audit P1-20/E-20: resume detects pending tool_calls and marks
        # metadata so tool nodes skip re-sending (avoiding duplicate side
        # effects). The assistant tool_calls are already in the messages;
        # when the runtime continues and current_node_id is the interrupted
        # tool node it would re-send; the marker lets upper layers detect it.
        # Here only log + mark; do not auto-delete messages (preserve LLM context).
        pending_tools = state.get("pending_tool_calls", [])
        # #714 part 1: prefer the node-state binding (checkpoint_binding
        # "session:node" key) when it holds fresher state for the checkpointed
        # node than the legacy session row; old checkpoints without binding
        # rows behave exactly as before.
        kcp = getattr(self, "kernel_checkpoints", None)
        bound_node_id = checkpoint.current_node_id or graph.start_node_id
        if kcp is not None:
            try:
                from fusion_harness.checkpoint_binding import bind_node_state

                from .paths import studio_home

                outbox = getattr(self, "node_effect_outbox", None)
                if outbox is None:
                    from fusion_harness.outbox import OutboxStore

                    outbox = OutboxStore(str(studio_home() / "node_effects.db"))
                    self.node_effect_outbox = outbox
                binder = bind_node_state(kcp.store, outbox, session_id)
                bound = binder.load(bound_node_id)
                if isinstance(bound, dict):
                    if not ctx.messages and bound.get("pending_tool_calls"):
                        pending_tools = list(bound["pending_tool_calls"])
                    logger.info(
                        "node binding state loaded node=%s keys=%s",
                        bound_node_id,
                        sorted(bound),
                    )
            except Exception as bind_err:
                logger.warning("node binding load failed session=%s: %s", session_id, bind_err)
        if pending_tools:
            ctx.metadata["pending_tool_calls"] = pending_tools
            logger.warning(
                "Resumed with %d pending tool_calls (assistant issued but no tool "
                "reply) — caller should skip re-dispatch to avoid duplicate side effects",
                len(pending_tools),
            )
        # Audit A-1 Tier3: resume rebuilds per-exec variables (fresh VM +
        # loaded snapshot), never writing back to the singleton self.variables.
        # dispatch is provided via context; no reseed.
        ctx.variables = VariableManager()

        saved_vars = state.get("variables", {})
        for k, v in saved_vars.items():
            ctx.variables.set(k, v)

        # #557: decisions captured while the backend was down — inject as
        # variables so the re-executed human_input node takes the resumed
        # branch ({input_var: value} keys) instead of pausing again.
        for k, v in (human_inputs or {}).items():
            ctx.variables.set(k, v)

        logger.info(
            "Resumed from checkpoint: graph=%s node=%s iteration=%d pending=%d",
            graph.name,
            ctx.current_node_id,
            ctx.iteration_count,
            len(pending_tools),
        )

        yield AgentEvent(
            type=AgentEventType.CHECKPOINT,
            content=f"Resumed from checkpoint at node '{ctx.current_node_id}'",
            metadata={"checkpoint": checkpoint.to_dict()},
        )

        exec_fn = self.execute_graph_stream if stream else self.execute_graph
        async for event in exec_fn(graph, "", context=ctx):
            yield event
