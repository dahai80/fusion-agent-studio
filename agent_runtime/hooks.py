from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

HOOKS_CONFIG_PATH = Path.home() / ".fusion-agent-studio" / "hooks.json"


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
    def __init__(self):
        self._hooks: dict[str, list[HookConfig]] = {}

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
        for h in matched:
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
            fail_closed = hook.event in (
                HookEvent.PRE_TOOL_USE.value,
                HookEvent.POST_TOOL_USE.value,
            )
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
        fail_closed = hook.event in (
            HookEvent.PRE_TOOL_USE.value,
            HookEvent.POST_TOOL_USE.value,
        )
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
