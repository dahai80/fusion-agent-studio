"""ACP (Agent Client Protocol) client — #496.

Drives external agent CLIs (e.g. Claude Code via the claude-code-acp adapter)
from graph nodes. Uses the agent-client-protocol package (module `acp`) over
stdio JSON-RPC. Gracefully degrades when the CLI is missing or the session
dies — callers get a structured AcpRunResult, never a bare crash.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


def default_acp_command() -> str:
    return os.environ.get("FUSION_ACP_COMMAND", "claude-code-acp")


@dataclass
class AcpRunResult:
    success: bool
    stop_reason: str = ""
    message: str = ""
    error: str = ""
    session_id: str = ""
    usage: dict | None = None
    events: list = field(default_factory=list)


def map_session_update(update: Any) -> dict:
    # Map an ACP session-update model to a plain dict. `session_update` is the
    # discriminator ("agent_message_chunk", "tool_call", ...). Content blocks
    # carry the text for message/thought chunks.
    kind = getattr(update, "session_update", "") or ""
    ev: dict = {"type": kind}
    content = getattr(update, "content", None)
    if content is not None:
        text = getattr(content, "text", None)
        if text is not None:
            ev["text"] = text
    for key in ("tool_call_id", "title", "kind", "status"):
        val = getattr(update, key, None)
        if val:
            ev[key] = val
    if kind == "usage_update":
        # In acp 0.12.x, UsageUpdate.used/size are plain token counts; the
        # full Usage breakdown arrives on the PromptResponse instead.
        used = getattr(update, "used", None)
        if used is not None:
            ev["used"] = used
    return ev


class AcpClientCallbacks:
    """Client-side callbacks the ACP agent invokes on us.

    session_update streams agent tokens/tool calls into `events` (and an
    optional async on_event hook for live SSE forwarding). Permission
    requests follow `policy`: "deny" (default, safe for unattended runs) or
    "allow" (auto-select the first allow-style option).
    """

    def __init__(
        self,
        events: list,
        on_event: Callable[[dict], Any] | None = None,
        permission_policy: str = "deny",
    ):
        self.events = events
        self.on_event = on_event
        self.permission_policy = permission_policy

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        ev = map_session_update(update)
        ev["session_id"] = session_id
        self.events.append(ev)
        logger.debug("acp session_update: %s", ev.get("type"))
        if self.on_event is not None:
            try:
                result = self.on_event(ev)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning("acp on_event hook failed: %s", e)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list, **kwargs: Any
    ) -> Any:
        from acp.schema import DeniedOutcome, RequestPermissionResponse

        if self.permission_policy == "allow" and options:
            from acp.schema import RequestPermissionResponse, SelectedPermissionOutcome

            option_id = options[0].option_id if hasattr(options[0], "option_id") else ""
            logger.info(
                "acp permission auto-allowed: tool=%s option=%s",
                getattr(tool_call, "title", "?"), option_id,
            )
            return RequestPermissionResponse(outcome=SelectedPermissionOutcome(option_id=option_id))
        logger.warning(
            "acp permission denied (policy=%s): tool=%s",
            self.permission_policy, getattr(tool_call, "title", "?"),
        )
        return RequestPermissionResponse(outcome=DeniedOutcome())

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any):
        raise _unsupported("fs/write_text_file")

    async def read_text_file(self, session_id: str, path: str, **kwargs: Any):
        raise _unsupported("fs/read_text_file")

    async def create_terminal(self, session_id: str, command: str, **kwargs: Any):
        raise _unsupported("terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any):
        raise _unsupported("terminal/output")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any):
        return None

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any):
        raise _unsupported("terminal/wait_for_exit")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any):
        return None

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any):
        from acp.schema import DeclineElicitationResponse

        logger.warning("acp elicitation declined: %s", str(message)[:120])
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict) -> dict:
        raise _unsupported(f"ext/{method}")

    async def ext_notification(self, method: str, params: dict) -> None:
        return None


def _unsupported(method: str) -> Exception:
    from acp.exceptions import RequestError

    return RequestError(-32601, f"fusion-agent-studio ACP client does not support {method}")


async def run_acp_prompt(
    prompt: str,
    command: str = "",
    args: list | None = None,
    cwd: str = "",
    env: dict | None = None,
    timeout: float = 120.0,
    on_event: Callable[[dict], Any] | None = None,
    permission_policy: str = "deny",
) -> AcpRunResult:
    """Spawn an ACP agent CLI, open a session, send one prompt, return result.

    Errors (missing CLI, init/timeout, dead session) surface as
    AcpRunResult(success=False, error=...) — never raise to the caller.
    """
    import acp

    cmd = command or default_acp_command()
    argv = [cmd, *(args or [])]
    workdir = str(cwd) if cwd else os.getcwd()
    events: list = []
    logger.info("acp run start: argv=%s cwd=%s timeout=%s", argv, workdir, timeout)

    client = AcpClientCallbacks(events, on_event=on_event, permission_policy=permission_policy)
    try:
        async with acp.spawn_agent_process(client, cmd, *(args or []), env=env, cwd=workdir) as (
            conn,
            proc,
        ):
            init = await asyncio.wait_for(conn.initialize(acp.PROTOCOL_VERSION), timeout)
            logger.info(
                "acp initialized: protocol=%s agent=%s",
                init.protocol_version,
                (init.agent_info.name if init.agent_info else "?"),
            )
            new = await asyncio.wait_for(conn.new_session(cwd=workdir), timeout)
            session_id = new.session_id
            logger.info("acp session opened: %s", session_id)
            resp = await asyncio.wait_for(
                conn.prompt(session_id, [acp.text_block(prompt)]), timeout
            )
            try:
                await asyncio.wait_for(conn.close_session(session_id), timeout=10.0)
            except Exception as e:
                logger.debug("acp close_session skipped: %s", e)
    except FileNotFoundError:
        logger.error("acp CLI not found: %s", cmd)
        return AcpRunResult(success=False, error=f"ACP CLI not found: {cmd}")
    except asyncio.TimeoutError:
        logger.error("acp run timed out after %ss: argv=%s", timeout, argv)
        return AcpRunResult(
            success=False, error=f"ACP run timed out after {timeout}s", events=events
        )
    except Exception as e:
        # Dead session / JSON-RPC error / process exit — graceful surface.
        logger.error("acp run failed: %s: %s", type(e).__name__, e)
        return AcpRunResult(
            success=False, error=f"ACP run failed: {type(e).__name__}: {e}", events=events
        )

    message = "".join(
        ev.get("text", "") for ev in events if ev.get("type") == "agent_message_chunk"
    )
    usage = None
    if resp.usage is not None:
        usage = {
            "total_tokens": resp.usage.total_tokens,
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
    logger.info(
        "acp run done: stop_reason=%s message_len=%d events=%d",
        resp.stop_reason, len(message), len(events),
    )
    return AcpRunResult(
        success=True,
        stop_reason=resp.stop_reason,
        message=message,
        session_id=session_id,
        usage=usage,
        events=events,
    )
