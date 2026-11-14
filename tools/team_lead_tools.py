"""Team lead tools (issue #707) — the consumer-side lead judgment loop.

A lead agent reads the task board (team_board), decides assignment
(team_assign), dispatches members under the SpawnBrief contract
(team_dispatch) and coordinates via the mailbox (team_message). The kernel
NEVER assigns — these tools exist so the lead's model-driven decisions have
a deterministic handle on the fusion-harness TeamRuntime. Providers keep the
agent_runtime <-> tools import cycle out of this module.
"""

from __future__ import annotations

import json
import logging

from .base import BaseTool

logger = logging.getLogger(__name__)


class TeamBoardTool(BaseTool):
    name = "team_board"
    description = (
        "Read a team's task board: lifecycle state, tasks (status/assignee/"
        "results), member roster and inbox counts."
    )
    readonly = True
    parameters = {
        "team": {"type": "string", "description": "Team name"},
    }

    def __init__(self, registry_provider):
        super().__init__()
        self.registry_provider = registry_provider

    async def execute(self, **kwargs) -> str:
        team = str(kwargs.get("team", "") or "")
        team_rt = (self.registry_provider() or {}).get(team)
        if team_rt is None:
            return f"Error: team not registered: {team}"
        d = team_rt.definition
        out = {
            "team": d.name,
            "lifecycle": team_rt.lifecycle,
            "members": [m.name for m in d.members],
            "tasks": [
                {
                    "id": tid,
                    "title": t.title,
                    "status": t.status,
                    "assignee": t.assignee,
                    "blocked_by": list(t.blocked_by),
                    "results": list((t.metadata or {}).get("results", [])),
                }
                for tid, t in team_rt._tasks.items()
            ],
            "inbox_counts": {name: len(v) for name, v in team_rt._inboxes.items()},
        }
        return json.dumps(out, ensure_ascii=False)


class TeamAssignTool(BaseTool):
    name = "team_assign"
    description = (
        "Add a task to a team's board (optionally pre-assigned to a member) "
        "or move an existing task between todo/in_progress/blocked/done."
    )
    readonly = False
    parameters = {
        "team": {"type": "string", "description": "Team name"},
        "title": {"type": "string", "description": "Task title (new tasks)"},
        "detail": {"type": "string", "description": "Task detail (new tasks)"},
        "assignee": {"type": "string", "description": "Member name to assign (new tasks)"},
        "blocked_by": {
            "type": "array",
            "description": "Task ids this task depends on (blocked status)",
        },
        "episodes": {
            "type": "array",
            "description": "Provenance ids (trace/eval) for the task",
        },
        "task_id": {"type": "string", "description": "Existing task id to transition"},
        "to": {"type": "string", "description": "Target status for transitions"},
    }

    def __init__(self, registry_provider):
        super().__init__()
        self.registry_provider = registry_provider

    async def execute(self, **kwargs) -> str:
        team = str(kwargs.get("team", "") or "")
        team_rt = (self.registry_provider() or {}).get(team)
        if team_rt is None:
            return f"Error: team not registered: {team}"
        try:
            if kwargs.get("task_id") and kwargs.get("to"):

                blocked_by = kwargs.get("blocked_by") or None
                team_rt.transition_task(str(kwargs["task_id"]), str(kwargs["to"]), blocked_by=blocked_by)
                logger.info("team_assign transition team=%s task=%s to=%s", team, kwargs["task_id"], kwargs["to"])
                return json.dumps({"status": "ok", "task_id": str(kwargs["task_id"]), "to": str(kwargs["to"])})
            from fusion_harness.team_runtime import TeamTask

            task = TeamTask(
                title=str(kwargs.get("title", "") or ""),
                detail=str(kwargs.get("detail", "") or ""),
                assignee=kwargs.get("assignee") or None,
                blocked_by=[str(t) for t in (kwargs.get("blocked_by") or [])],
                episodes=[str(t) for t in (kwargs.get("episodes") or [])],
            )
            task_id = team_rt.add_task(task)
            logger.info("team_assign added team=%s task=%s assignee=%s", team, task_id, task.assignee)
            return json.dumps({"status": "ok", "task_id": task_id, "assignee": task.assignee})
        except Exception as e:
            logger.warning("team_assign rejected team=%s: %s", team, e)
            return f"Error: {e}"


class TeamDispatchTool(BaseTool):
    name = "team_dispatch"
    description = (
        "Run a team member on a task with a full SpawnBrief (objective/"
        "output_format/tool_guidance/boundaries/token_budget/concurrency/depth). "
        "Incomplete briefs, budgets above the member cap, tools outside the "
        "member allowlist and non-ACTIVE teams are rejected before any worker starts."
    )
    readonly = False
    parameters = {
        "team": {"type": "string", "description": "Team name"},
        "member": {"type": "string", "description": "Member name to run"},
        "task_id": {"type": "string", "description": "Task id on the board"},
        "brief": {
            "type": "object",
            "description": "SpawnBrief dict with all 7 required fields",
        },
        "tools": {
            "type": "array",
            "description": "Optional requested tools; must be within the member allowlist",
        },
    }

    def __init__(self, registry_provider, agent_fn_provider):
        super().__init__()
        self.registry_provider = registry_provider
        self.agent_fn_provider = agent_fn_provider

    async def execute(self, **kwargs) -> str:
        from fusion_harness.spawn_protocol import validate_brief

        from agent_runtime.dispatchers.teams import (
            reject_tools_outside_allowlist,
            validate_member_constraints,
        )

        team = str(kwargs.get("team", "") or "")
        team_rt = (self.registry_provider() or {}).get(team)
        if team_rt is None:
            return f"Error: team not registered: {team}"
        try:
            brief, member = validate_member_constraints(
                team_rt.definition, str(kwargs.get("member", "") or ""), dict(kwargs.get("brief") or {})
            )
            reject_tools_outside_allowlist(member, [str(t) for t in (kwargs.get("tools") or [])])
            errors = validate_brief(brief)
            if errors:
                return "Error: invalid spawn brief: " + "; ".join(errors)
            result = await team_rt.run_member(
                member, str(kwargs.get("task_id", "") or ""), brief, self.agent_fn_provider()
            )
        except Exception as e:
            logger.warning("team_dispatch rejected team=%s: %s", team, e)
            return f"Error: {e}"
        logger.info(
            "team_dispatch team=%s member=%s status=%s tokens=%s",
            team,
            member.name,
            result.status,
            result.tokens_spent,
        )
        return json.dumps(
            {
                "status": "ok",
                "member": result.name,
                "run_status": result.status,
                "summary": result.summary,
                "turns": result.turns,
                "tokens_spent": result.tokens_spent,
                "error": result.error,
                "partial": result.status == "capped",
            },
            ensure_ascii=False,
        )


class TeamMessageTool(BaseTool):
    name = "team_message"
    description = (
        "Send a mailbox message to one member (to=name) or broadcast to all "
        "others (to empty). Delivered only when the inbox disk write succeeds."
    )
    readonly = False
    parameters = {
        "team": {"type": "string", "description": "Team name"},
        "from": {"type": "string", "description": "Sender member name"},
        "to": {"type": "string", "description": "Recipient member name; empty = broadcast"},
        "body": {"type": "string", "description": "Message body"},
        "type": {
            "type": "string",
            "description": "Message type: text|plan_approval|shutdown_request|shutdown_response",
        },
    }

    def __init__(self, registry_provider):
        super().__init__()
        self.registry_provider = registry_provider

    async def execute(self, **kwargs) -> str:
        team = str(kwargs.get("team", "") or "")
        team_rt = (self.registry_provider() or {}).get(team)
        if team_rt is None:
            return f"Error: team not registered: {team}"
        sender = str(kwargs.get("from", "") or "")
        body = str(kwargs.get("body", "") or "")
        type_ = str(kwargs.get("type", "") or "text")
        try:
            if kwargs.get("to"):
                from fusion_harness.team_runtime import TeamMessage

                delivered = team_rt.send(
                    TeamMessage(type=type_, sender=sender, recipient=str(kwargs["to"]), body=body)
                )
                count = 1 if delivered else 0
            else:
                count = team_rt.broadcast(sender, body, type_=type_)
        except Exception as e:
            logger.warning("team_message rejected team=%s: %s", team, e)
            return f"Error: {e}"
        if not count:
            return "Error: mailbox delivery failed (disk write)"
        logger.info("team_message team=%s from=%s delivered=%d", team, sender, count)
        return json.dumps({"status": "ok", "delivered": count})
