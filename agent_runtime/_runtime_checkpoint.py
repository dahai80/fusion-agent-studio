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
        # 审计 P1-20/E-20: 找出尾段未闭合 tool_calls — assistant 发 tool_calls
        # 后无匹配 tool 回复. 返回该 assistant 消息的 tool_calls 列表 (空则无 pending).
        if not messages:
            return []
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role == "tool":
                # 最近一条是 tool 回复 -> 已闭合, 无 pending.
                return []
            if role == "assistant" and msg.get("tool_calls"):
                # assistant tool_calls 后无 tool 回复 -> pending.
                return msg["tool_calls"]
            if role == "assistant":
                # assistant 无 tool_calls -> 无 pending.
                return []
        return []

    async def _save_checkpoint(self, ctx: AgentContext, graph: AgentGraph) -> None:
        """Auto-save checkpoint if store is configured."""
        if not self.store:
            return
        try:
            # 审计 P1-20/E-20: 捕获未闭合 tool_calls (assistant 发了 tool_calls 但
            # resume 前无对应 tool 角色回复). 原仅存 messages, resume 从
            # current_node_id 重跑会重发 tool_call 致重复副作用 (文件写两遍/命令
            # 跑两次). 现显式记录, resume 据此标记跳过.
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
            logger.debug(
                "Checkpoint saved: graph=%s node=%s pending_tools=%d",
                graph.name, ctx.current_node_id, len(pending),
            )
            ctx.checkpoint_fail_count = 0
        except Exception as e:
            ctx.checkpoint_fail_count += 1
            # 审计 M-3: 持续失败 (DB 满/锁/schema 不匹配) 仅 warning 但执行照常,
            # 之后 resume 报 "No checkpoint found" 用户无信号. 连续 3 次升级 error.
            # 审计 P3-1: 计数迁 per-exec ctx, 并发执行各自累计不互踩.
            if ctx.checkpoint_fail_count >= 3:
                logger.error(
                    "Checkpoint save failed %d consecutive times (resume will not work): %s",
                    ctx.checkpoint_fail_count, e,
                )
            else:
                logger.warning("Checkpoint save failed (%d): %s", ctx.checkpoint_fail_count, e)

    async def resume_from_checkpoint(
        self,
        graph: AgentGraph,
        session_id: str,
        stream: bool = False,
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
        # 审计 E-20: Checkpoint 是 dataclass 非 dict, 用属性 + 解析 state_json.
        raw_state = checkpoint.context_json or "{}"
        try:
            state = json.loads(raw_state) if raw_state else {}
        except (json.JSONDecodeError, TypeError):
            state = {}
        ctx.messages = state.get("messages", [])
        ctx.iteration_count = state.get("iteration_count", 0)
        ctx.current_node_id = checkpoint.current_node_id or graph.start_node_id
        ctx.tool_call_chain_count = state.get("tool_call_chain_count", 0)
        # 审计 P1-20/E-20: resume 检测 pending tool_calls, 标记 metadata 供
        # tool 节点跳过重发 (避免重复副作用). 消息里 assistant tool_calls 已在,
        # runtime 续跑时若 current_node_id 是被中断的 tool 节点会重发; 标记让
        # 上层感知. 此处仅记日志 + 标记, 不自动删消息 (保留 LLM 上下文).
        pending_tools = state.get("pending_tool_calls", [])
        if pending_tools:
            ctx.metadata["pending_tool_calls"] = pending_tools
            logger.warning(
                "Resumed with %d pending tool_calls (assistant issued but no tool "
                "reply) — caller should skip re-dispatch to avoid duplicate side effects",
                len(pending_tools),
            )
        # 审计 A-1 Tier3: resume 重建 per-exec variables (fresh VM + 载入快照),
        # 不写回 singleton self.variables. dispatch 见 context 提供, 不 reseed.
        ctx.variables = VariableManager()

        saved_vars = state.get("variables", {})
        for k, v in saved_vars.items():
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
