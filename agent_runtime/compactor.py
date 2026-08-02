from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CompactionConfig:
    context_window: int = 32768
    warning_buffer: int = 3000
    error_buffer: int = 2000
    manual_buffer: int = 500
    keep_recent_rounds: int = 3
    tool_result_head: int = 200
    tool_result_tail: int = 100
    tool_result_max_lines: int = 50

    def warning_threshold(self) -> int:
        return self.context_window - self.warning_buffer

    def error_threshold(self) -> int:
        return self.context_window - self.error_buffer

    def manual_threshold(self) -> int:
        return self.context_window - self.manual_buffer


class Compactor:
    def __init__(
        self,
        config: CompactionConfig | None = None,
        llm_gateway=None,
        memory_engine=None,
    ):
        self.config = config or CompactionConfig()
        self.llm_gateway = llm_gateway
        self.memory_engine = memory_engine

    def _persist_summary(self, summary: str, original_count: int) -> None:
        if self.memory_engine is None or not summary:
            return
        try:
            self.memory_engine.store_summary(
                summary, scope="compaction", original_count=original_count
            )
            logger.info("persisted compaction summary orig_count=%d", original_count)
        except Exception as exc:
            logger.warning("failed to persist compaction summary: %s", exc)

    def estimate_tokens(self, messages: list[dict]) -> int:
        total = 0
        for m in messages:
            content = m.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            total += len(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                total += len(fn.get("name", "")) + len(fn.get("arguments", ""))
        return max(1, total // 4)

    def should_compact(self, messages: list[dict]) -> str:
        tokens = self.estimate_tokens(messages)
        if tokens >= self.config.error_threshold():
            return "error"
        if tokens >= self.config.warning_threshold():
            return "warning"
        if tokens >= self.config.manual_threshold():
            return "manual"
        return "none"

    def compact(self, messages: list[dict], level: str = "warning") -> list[dict]:
        before_tokens = self.estimate_tokens(messages)
        msgs = self._microcompact(list(messages))
        if self.estimate_tokens(msgs) >= self.config.warning_threshold():
            msgs = self._smart_truncate(msgs)
        if (
            level == "error"
            and self.estimate_tokens(msgs) >= self.config.error_threshold()
        ):
            msgs = self._hard_compact(msgs)
        after_tokens = self.estimate_tokens(msgs)
        logger.info(
            "compact level=%s before_msgs=%d after_msgs=%d before_tok=%d after_tok=%d",
            level,
            len(messages),
            len(msgs),
            before_tokens,
            after_tokens,
        )
        return msgs

    def reactive_strip(self, messages: list[dict]) -> list[dict]:
        system_msgs, rest = self._partition(messages)
        if len(rest) <= 2:
            stripped = [self._truncate_tool(m) for m in rest]
        else:
            keep_tail = rest[-2:]
            dropped = rest[:-2]
            summary = self._summarize(dropped)
            self._persist_summary(summary, len(dropped))
            stripped = [{"role": "system", "content": summary}] + [
                self._truncate_tool(m) for m in keep_tail
            ]
        logger.info(
            "reactive_strip before_msgs=%d after_msgs=%d before_tok=%d after_tok=%d",
            len(messages),
            len(system_msgs) + len(stripped),
            self.estimate_tokens(messages),
            self.estimate_tokens(system_msgs + stripped),
        )
        return system_msgs + stripped

    def _microcompact(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m.get("role") == "tool":
                m = self._truncate_tool(m)
            out.append(m)
        return out

    def _truncate_tool(self, m: dict) -> dict:
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = str(content)
        limit = self.config.tool_result_head + self.config.tool_result_tail
        if len(content) <= limit:
            return m
        head = content[: self.config.tool_result_head]
        tail = content[-self.config.tool_result_tail :]
        dropped = len(content) - len(head) - len(tail)
        logger.debug("truncate_tool dropped=%d", dropped)
        return {**m, "content": f"{head}\n...[truncated {dropped} chars]...\n{tail}"}

    def _smart_truncate(self, messages: list[dict]) -> list[dict]:
        system_msgs, rest = self._partition(messages)
        rounds = self._split_rounds(rest)
        keep = self.config.keep_recent_rounds
        if len(rounds) <= keep:
            return messages
        dropped = rounds[: len(rounds) - keep]
        kept = rounds[len(rounds) - keep :]
        dropped_msgs = [m for r in dropped for m in r]
        summary = self._summarize(dropped_msgs)
        self._persist_summary(summary, len(dropped_msgs))
        logger.info(
            "smart_truncate dropped_rounds=%d kept_rounds=%d", len(dropped), len(kept)
        )
        return (
            system_msgs
            + [{"role": "system", "content": summary}]
            + [m for r in kept for m in r]
        )

    def _hard_compact(self, messages: list[dict]) -> list[dict]:
        system_msgs, rest = self._partition(messages)
        rounds = self._split_rounds(rest)
        if len(rounds) <= 1:
            return messages
        dropped = rounds[: len(rounds) - 1]
        kept = rounds[-1:]
        dropped_msgs = [m for r in dropped for m in r]
        summary = self._summarize(dropped_msgs, hard=True)
        self._persist_summary(summary, len(dropped_msgs))
        logger.info(
            "hard_compact dropped_rounds=%d kept_rounds=%d", len(dropped), len(kept)
        )
        return (
            system_msgs
            + [{"role": "system", "content": summary}]
            + [m for r in kept for m in r]
        )

    def _summarize(self, messages: list[dict], hard: bool = False) -> str:
        user_intents = []
        tool_names = []
        conclusions = []
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            if role == "user" and content:
                user_intents.append(content[:120])
            elif role == "assistant":
                if content:
                    conclusions.append(content[:120])
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    if name and name not in tool_names:
                        tool_names.append(name)
        parts = ["[Compacted prior context]"]
        if user_intents:
            parts.append("User asked: " + " | ".join(user_intents[:5]))
        if tool_names:
            parts.append("Tools used: " + ", ".join(tool_names[:10]))
        if conclusions:
            parts.append("Conclusions: " + " | ".join(conclusions[:5]))
        if hard:
            parts.append("(hard-compact: only most recent round retained)")
        return "\n".join(parts)

    def _partition(self, messages: list[dict]) -> tuple[list[dict], list[dict]]:
        system_msgs = []
        rest = []
        for m in messages:
            if m.get("role") == "system" and not rest:
                system_msgs.append(m)
            else:
                rest.append(m)
        return system_msgs, rest

    def _split_rounds(self, messages: list[dict]) -> list[list[dict]]:
        rounds: list[list[dict]] = []
        current: list[dict] = []
        for m in messages:
            if m.get("role") == "user":
                if current:
                    rounds.append(current)
                current = [m]
            else:
                current.append(m)
        if current:
            rounds.append(current)
        return rounds
