from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

HOOKS_CONFIG_PATH = Path.home() / ".fusion-agent-studio" / "hooks.json"

_DECORATOR_REGISTRY: list = []

# 审计 P-3: 安全门事件在异常/超预算时必须 fail-closed. 跳过不等于放行。
FAIL_CLOSED_EVENTS = frozenset(
    {
        "PRE_TOOL_USE",
        "POST_TOOL_USE",
        "TOOL_DENY",
    }
)


def hook(event: str, matcher: str = "*"):
    def deco(fn):
        _DECORATOR_REGISTRY.append(
            HookConfig(event=event, matcher=matcher, type="callback", callback=fn)
        )
        return fn

    return deco


class HookEvent(str, enum.Enum):
    PRE_TOOL_USE = "PRE_TOOL_USE"
    POST_TOOL_USE = "POST_TOOL_USE"
    POST_TOOL_USE_FAILURE = "POST_TOOL_USE_FAILURE"
    SESSION_START = "SESSION_START"
    SESSION_END = "SESSION_END"
    STOP = "STOP"
    PRE_COMPACT = "PRE_COMPACT"
    SUBAGENT_START = "SUBAGENT_START"
    SUBAGENT_STOP = "SUBAGENT_STOP"
    USER_PROMPT_SUBMIT = "USER_PROMPT_SUBMIT"
    GRAPH_BEFORE = "GRAPH_BEFORE"
    GRAPH_AFTER = "GRAPH_AFTER"
    NODE_BEFORE = "NODE_BEFORE"
    NODE_AFTER = "NODE_AFTER"
    TOOL_DENY = "TOOL_DENY"
    LLM_BEFORE = "LLM_BEFORE"
    LLM_AFTER = "LLM_AFTER"
    APPROVAL_REQUEST = "APPROVAL_REQUEST"
    APPROVAL_RESOLVE = "APPROVAL_RESOLVE"
    COMPACT_BEFORE = "COMPACT_BEFORE"
    COMPACT_AFTER = "COMPACT_AFTER"
    BUDGET_EXCEED = "BUDGET_EXCEED"
    # N8 #391: terminal/observability events that had no hook trigger.
    ERROR = "ERROR"
    CHECKPOINT = "CHECKPOINT"
    TEAM_MESSAGE = "TEAM_MESSAGE"
    # N16 #390: persona file hot reload.
    PERSONA_RELOADED = "PERSONA_RELOADED"


@dataclass
class HookConfig:
    event: str
    matcher: str = "*"
    type: str = "callback"
    command: str = ""
    timeout: float = 10.0
    callback: object = None

    def matches(self, tool_name: str) -> bool:
        if not self.matcher or self.matcher == "*":
            return True
        return re.search(self.matcher, tool_name or "") is not None

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "matcher": self.matcher,
            "type": self.type,
            "command": self.command,
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HookConfig":
        return cls(
            event=data["event"],
            matcher=data.get("matcher", "*"),
            type=data.get("type", "callback"),
            command=data.get("command", ""),
            timeout=data.get("timeout", 10.0),
        )


@dataclass
class HookResult:
    continue_loop: bool = True
    decision: str = "approve"
    reason: str = ""
    updated_input: str = ""
    additional_context: str = ""
    system_message: str = ""


class HookEngine:
    def __init__(
        self,
        auto_register_decorators: bool = True,
        max_fire_ms: float | None = None,
    ):
        self._hooks: dict[str, list[HookConfig]] = {}
        self._decorator_consumed = 0
        if max_fire_ms is None:
            max_fire_ms = float(os.environ.get("FUSION_HOOK_MAX_MS", "50"))
        self.max_fire_ms = max_fire_ms
        if auto_register_decorators:
            n = self.flush_decorator_registry()
            if n:
                logger.info("auto-register %d decorator hooks", n)

    def flush_decorator_registry(self) -> int:
        pending = _DECORATOR_REGISTRY[self._decorator_consumed:]
        for cfg in pending:
            self.register(cfg)
        self._decorator_consumed += len(pending)
        return len(pending)

    def register(self, hook: HookConfig) -> None:
        self._hooks.setdefault(hook.event, []).append(hook)
        logger.info(
            "hook registered event=%s matcher=%s type=%s",
            hook.event,
            hook.matcher,
            hook.type,
        )

    def load_from_config(self, path=None) -> None:
        path = Path(path or HOOKS_CONFIG_PATH)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except Exception as e:
            logger.warning("hooks config parse error %s: %s", path, e)
            return
        for item in data.get("hooks", []):
            try:
                self.register(HookConfig.from_dict(item))
            except Exception as e:
                logger.warning("skip bad hook %s: %s", item, e)

    def list_hooks(self) -> list[dict]:
        return [h.to_dict() for hs in self._hooks.values() for h in hs]

    async def fire(self, event: str, payload: dict, tool_name: str = "") -> HookResult:
        hooks = self._hooks.get(event, [])
        matched = [h for h in hooks if h.matches(tool_name)]
        if not matched:
            return HookResult()
        aggregated = HookResult()
        deadline = time.monotonic() + self.max_fire_ms / 1000
        for idx, h in enumerate(matched):
            if time.monotonic() > deadline:
                skipped = len(matched) - idx
                if event in FAIL_CLOSED_EVENTS:
                    # 审计 P-3: 安全门事件预算耗尽必须 block —— 跳过剩余
                    # PRE_TOOL_USE 等于把工具调用直接放行.
                    logger.warning(
                        "hook chain budget exceeded event=%s budget_ms=%.1f skipped=%d fail_closed=true",
                        event,
                        self.max_fire_ms,
                        skipped,
                    )
                    return HookResult(
                        continue_loop=False,
                        decision="block",
                        reason=(
                            f"hook chain budget exceeded ({self.max_fire_ms}ms) with "
                            f"{skipped} security hook(s) unrun (fail-closed)"
                        ),
                    )
                logger.warning(
                    "hook chain budget exceeded event=%s budget_ms=%.1f skipping=%d",
                    event,
                    self.max_fire_ms,
                    skipped,
                )
                break
            try:
                res = await self._run(h, payload)
            except Exception as e:
                logger.warning("hook run error event=%s err=%s", event, e)
                continue
            if res is None:
                continue
            if not res.continue_loop:
                aggregated.continue_loop = False
            if res.decision == "block":
                aggregated.decision = "block"
                aggregated.reason = res.reason or aggregated.reason
            if res.additional_context:
                aggregated.additional_context = res.additional_context
            if res.system_message:
                aggregated.system_message = res.system_message
            if res.updated_input:
                aggregated.updated_input = res.updated_input
        logger.info(
            "hook fired event=%s matched=%d decision=%s continue=%s",
            event,
            len(matched),
            aggregated.decision,
            aggregated.continue_loop,
        )
        return aggregated

    async def _run(self, hook: HookConfig, payload: dict) -> HookResult | None:
        if hook.type == "command" and hook.command:
            return await self._run_command(hook, payload)
        if hook.type == "callback" and hook.callback is not None:
            return self._coerce(hook.callback(payload))
        logger.warning("hook has no command/callback event=%s", hook.event)
        return None

    async def _run_command(self, hook: HookConfig, payload: dict) -> HookResult:
        # 审计 E-7: command hook 执行任意 shell 命令, 默认关 (RCE 面). 须显式
        # FUSION_HOOKS_ALLOW_COMMAND=1 开启. 关时该 hook 降级为 no-op (回调类
        # hook 不受影响), fail-closed 仅安全门事件.
        if os.environ.get("FUSION_HOOKS_ALLOW_COMMAND", "") != "1":
            logger.warning(
                "hook command blocked (FUSION_HOOKS_ALLOW_COMMAND!=1) cmd=%s event=%s",
                hook.command,
                hook.event,
            )
            fail_closed = hook.event in FAIL_CLOSED_EVENTS
            if fail_closed:
                return HookResult(
                    continue_loop=False,
                    decision="block",
                    reason="command hooks disabled (set FUSION_HOOKS_ALLOW_COMMAND=1)",
                )
            return HookResult()
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(hook.command),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdin_data = json.dumps(payload).encode()
        # 审计 D-7: 安全门 fail-closed. PRE_TOOL_USE / POST_TOOL_USE 是工具执行
        # 安全门, hook 超时或输出畸形时不应静默放行 (原默认 approve), 否则安全
        # hook 崩溃即变 no-op. 改为 fail-closed: block + 留日志. 其余事件
        # (SESSION_*/STOP/...) 维持 fail-open approve, 因其非安全门.
        fail_closed = hook.event in FAIL_CLOSED_EVENTS
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=hook.timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                "hook command timed out cmd=%s event=%s fail_closed=%s",
                hook.command,
                hook.event,
                fail_closed,
            )
            if fail_closed:
                return HookResult(
                    continue_loop=False,
                    decision="block",
                    reason=f"hook timed out after {hook.timeout}s (fail-closed)",
                )
            return HookResult()
        try:
            data = json.loads(stdout.decode())
        except Exception as e:
            logger.warning(
                "hook %s returned non-JSON output, fail_closed=%s err=%s",
                hook.command,
                fail_closed,
                e,
            )
            if fail_closed:
                return HookResult(
                    continue_loop=False,
                    decision="block",
                    reason="hook returned non-JSON output (fail-closed)",
                )
            return HookResult()
        return self._coerce(data)

    def _coerce(self, val) -> HookResult:
        if isinstance(val, HookResult):
            return val
        if isinstance(val, dict):
            return HookResult(
                continue_loop=val.get("continue_loop", True),
                decision=val.get("decision", "approve"),
                reason=val.get("reason", ""),
                updated_input=val.get("updated_input", ""),
                additional_context=val.get("additional_context", ""),
                system_message=val.get("system_message", ""),
            )
        if isinstance(val, bool):
            return HookResult(continue_loop=val, decision="approve" if val else "block")
        if isinstance(val, str):
            low = val.strip().lower()
            return HookResult(
                decision="block" if low in ("block", "deny", "false") else "approve",
                reason=val,
            )
        return HookResult()
