# Issue #707 (T3): teams.* dispatcher — kernel TeamRuntime facade.
#
# agent-studio is the roster holder (design doc
# fusion-insight/fusion-team-architecture-2026-11-14.md §5.1): this module
# translates RPC params into kernel TeamDefinition/TeamTask/TeamMessage and
# delegates ALL mechanisms to fusion_harness.team_runtime (lifecycle state
# machine, task board, mailbox, persistence). Judgment (assignment, lead
# decisions) stays consumer-side — the kernel never assigns.
#
# Negative cases are rejected here BEFORE any worker starts: incomplete
# briefs, brief tools outside the member allowlist, brief budget above the
# member cap, and operations in the wrong lifecycle state (kernel raises
# TeamStateError; the member-cap/tool checks are pre-flight here so no
# SubagentRunner session is ever created for a doomed run).
#
# Auth: teams.* handlers are NOT in rpc_auth.PRE_AUTH_ALLOWLIST, so they sit
# at the same protection level as subagent.run (#326 deny-by-default flow).
import logging
import os

from .base import SubDispatcher

logger = logging.getLogger(__name__)


def validate_member_constraints(definition, member_name: str, brief_raw: dict):
    # Pre-flight (before any worker): brief completeness is enforced by
    # brief_from_event; budget can only TIGHTEN (brief above the member cap
    # is rejected, never silently clamped). Shared by the dispatcher and the
    # lead tools (#707).
    from fusion_harness.spawn_protocol import SpawnBriefError, brief_from_event

    brief = brief_from_event(dict(brief_raw or {}))
    member = definition.member(str(member_name or ""))
    if brief.token_budget > member.budget_tokens:
        raise SpawnBriefError(
            f"brief token_budget {brief.token_budget} exceeds member cap "
            f"{member.budget_tokens} ({member.name})"
        )
    return brief, member


def reject_tools_outside_allowlist(member, requested: list) -> None:
    if not member.tool_allowlist:
        return
    outside = [t for t in requested if t not in member.tool_allowlist]
    if outside:
        raise ValueError(
            f"tools outside member allowlist ({member.name}): {', '.join(outside)}"
        )


def member_persona_gate(member):
    # Persona bridge (#707): TeamMember.skills IS the #482 persona skills
    # allowlist — the same list shape PersonaGate.skill_allowed enforces, so
    # consumers can gate a member's skill usage with the existing mechanism.
    from ..persona_gate import PersonaGate

    return PersonaGate({"skills": list(member.skills)})


def teams_state_root() -> str:
    from ..paths import studio_home

    return str(studio_home() / "teams")


class TeamsDispatcher(SubDispatcher):
    def get_handlers(self) -> dict:
        return {
            "teams.register": self._handle_register,
            "teams.add_task": self._handle_add_task,
            "teams.transition": self._handle_transition,
            "teams.claim_task": self._handle_claim_task,
            "teams.run_member": self._handle_run_member,
            "teams.send": self._handle_send,
            "teams.broadcast": self._handle_broadcast,
            "teams.inbox": self._handle_inbox,
            "teams.status": self._handle_status,
            "teams.pause": self._handle_pause,
            "teams.resume": self._handle_resume,
            "teams.disband": self._handle_disband,
            "teams.memory_write": self._handle_memory_write,
            "teams.memory_read": self._handle_memory_read,
            "teams.list": self._handle_list,
            "teams.lead_start": self._handle_lead_start,
            "teams.lead_stop": self._handle_lead_stop,
            "teams.lead_status": self._handle_lead_status,
        }

    def _registry(self) -> dict:
        rt = self._daemon._get_runtime() if hasattr(self._daemon, "_get_runtime") else None
        reg = getattr(rt, "teams_registry", None)
        if reg is None:
            reg = {}
            try:
                rt.teams_registry = reg
            except Exception:
                pass
        return reg

    def _get(self, name: str):
        return self._registry().get(str(name or ""))

    def _require_team(self, params: dict):
        name = str(params.get("team", "") or "")
        team_rt = self._get(name)
        if team_rt is None:
            raise KeyError(f"team not registered: {name}")
        return team_rt

    @staticmethod
    def _error(e: Exception) -> dict:
        logger.warning("teams.* rejected: %s: %s", type(e).__name__, e)
        return {"status": "error", "message": str(e)}

    # ---------- registration ----------

    async def _handle_register(self, params: dict) -> dict:
        from fusion_harness.team import TeamDefinition, TeamMember
        from fusion_harness.team_runtime import TeamRuntime

        raw = dict(params.get("definition") or params)
        try:
            members = [
                TeamMember(
                    name=str(m.get("name", "") or ""),
                    role=str(m.get("role", "") or ""),
                    skills=[str(s) for s in (m.get("skills") or [])],
                    tool_allowlist=[str(t) for t in (m.get("tool_allowlist") or [])],
                    permission_mode=str(m.get("permission_mode", "") or "default"),
                    budget_tokens=int(m.get("budget_tokens", 0) or 50000),
                    max_concurrency=int(m.get("max_concurrency", 0) or 1),
                )
                for m in (raw.get("members") or [])
            ]
            definition = TeamDefinition(
                name=str(raw.get("name", "") or ""),
                namespace_key=str(raw.get("namespace_key", "") or raw.get("name", "") or ""),
                members=members,
                lead_budget_tokens=int(raw.get("lead_budget_tokens", 0) or 200000),
                playbook=bool(raw.get("playbook", False)),
                max_depth=int(raw.get("max_depth", 0) or 1),
                metadata=dict(raw.get("metadata") or {}),
            )
        except (TypeError, ValueError) as e:
            return self._error(e)
        if definition.name in self._registry():
            return {"status": "error", "message": f"team already registered: {definition.name}"}
        team_rt = TeamRuntime(
            definition,
            runner=self._kernel_runner(),
            bus=getattr(self._daemon._get_runtime(), "lifecycle_bus", None)
            if hasattr(self._daemon, "_get_runtime")
            else None,
            state_dir=str(params.get("state_dir", "") or "") or teams_state_root() + "/" + definition.name,
        )
        try:
            os.makedirs(team_rt.state_dir, exist_ok=True)
        except OSError as e:
            return self._error(e)
        try:
            team_rt.activate()
        except Exception as e:
            return self._error(e)
        self._registry()[definition.name] = team_rt
        logger.info(
            "teams.register name=%s members=%d namespace_key=%s",
            definition.name,
            len(definition.members),
            definition.namespace_key,
        )
        return {
            "status": "ok",
            "name": definition.name,
            "namespace_key": definition.namespace_key,
            "memory_anchor": definition.memory_anchor,
            "members": [m.name for m in definition.members],
            "state_dir": team_rt.state_dir,
        }

    def _kernel_runner(self):
        # Reuse the kernel orchestrator's SubagentRunner when wired (#675) so
        # team runs share the same runner/bus; TeamRuntime builds its own
        # otherwise.
        rt = self._daemon._get_runtime() if hasattr(self._daemon, "_get_runtime") else None
        orch = (getattr(rt, "kernel", None) or {}).get("orchestrator")
        if isinstance(orch, dict) and orch.get("runner") is not None:
            return orch["runner"]
        return None

    def _agent_fn(self):
        rt = self._daemon._get_runtime() if hasattr(self._daemon, "_get_runtime") else None
        orch = (getattr(rt, "kernel", None) or {}).get("orchestrator")
        if isinstance(orch, dict) and orch.get("agent_fn") is not None:
            return orch["agent_fn"]
        from ..kernel_wiring import make_studio_agent_fn

        return make_studio_agent_fn(rt)

    # ---------- task board ----------

    async def _handle_add_task(self, params: dict) -> dict:
        from fusion_harness.team_runtime import TeamTask

        try:
            team_rt = self._require_team(params)
            task = TeamTask(
                title=str(params.get("title", "") or ""),
                detail=str(params.get("detail", "") or ""),
                id=str(params.get("task_id", "") or ""),
                assignee=params.get("assignee") or None,
                status=str(params.get("status", "") or "todo"),
                blocked_by=[str(t) for t in (params.get("blocked_by") or [])],
                episodes=[str(t) for t in (params.get("episodes") or [])],
                metadata=dict(params.get("metadata") or {}),
            )
            task_id = team_rt.add_task(task)
        except Exception as e:
            return self._error(e)
        logger.info("teams.add_task team=%s task=%s", team_rt.definition.name, task_id)
        return {"status": "ok", "task_id": task_id}

    async def _handle_transition(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            team_rt.transition_task(
                str(params.get("task_id", "") or ""),
                str(params.get("to", "") or params.get("status", "") or ""),
                blocked_by=params.get("blocked_by"),
            )
        except Exception as e:
            return self._error(e)
        return {"status": "ok"}

    async def _handle_claim_task(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            won = team_rt.claim_task(
                str(params.get("task_id", "") or ""),
                str(params.get("member", "") or ""),
            )
        except Exception as e:
            return self._error(e)
        return {"status": "ok", "won": won}

    # ---------- member execution ----------

    async def _handle_run_member(self, params: dict) -> dict:
        from fusion_harness.spawn_protocol import SpawnBrief, validate_brief

        try:
            team_rt = self._require_team(params)
            brief, member = validate_member_constraints(
                team_rt.definition, str(params.get("member", "") or ""), dict(params.get("brief") or {})
            )
            reject_tools_outside_allowlist(member, [str(t) for t in (params.get("tools") or [])])
            errors = validate_brief(brief)
            if errors:
                raise ValueError("invalid spawn brief: " + "; ".join(errors))
            if not isinstance(brief, SpawnBrief):
                raise ValueError("brief must be a SpawnBrief contract dict")
            result = await team_rt.run_member(member, str(params.get("task_id", "") or ""), brief, self._agent_fn())
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.run_member team=%s member=%s task=%s status=%s tokens=%s",
            team_rt.definition.name,
            member.name,
            params.get("task_id"),
            result.status,
            result.tokens_spent,
        )
        return {
            "status": "ok",
            "result": {
                "subagent_id": result.subagent_id,
                "member": result.name,
                "status": result.status,
                "summary": result.summary,
                "turns": result.turns,
                "tokens_spent": result.tokens_spent,
                "error": result.error,
                "via": "kernel-team-runtime",
                "partial": result.status == "capped",
            },
        }

    # ---------- mailbox ----------

    async def _handle_send(self, params: dict) -> dict:
        from fusion_harness.team_runtime import TeamMessage

        try:
            team_rt = self._require_team(params)
            msg = TeamMessage(
                type=str(params.get("type", "") or "text"),
                sender=str(params.get("from", "") or ""),
                recipient=str(params.get("to", "") or ""),
                body=str(params.get("body", "") or ""),
            )
            delivered = team_rt.send(msg)
        except Exception as e:
            return self._error(e)
        if not delivered:
            return {"status": "error", "message": "mailbox delivery failed (disk write)"}
        logger.info(
            "teams.send team=%s from=%s to=%s type=%s",
            team_rt.definition.name,
            msg.sender,
            msg.recipient,
            msg.type,
        )
        return {"status": "ok", "delivered": True}

    async def _handle_broadcast(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            delivered = team_rt.broadcast(
                str(params.get("from", "") or ""),
                str(params.get("body", "") or ""),
                type_=str(params.get("type", "") or "text"),
            )
        except Exception as e:
            return self._error(e)
        logger.info("teams.broadcast team=%s delivered=%d", team_rt.definition.name, delivered)
        return {"status": "ok", "delivered": delivered}

    async def _handle_inbox(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            msgs = team_rt.inbox(str(params.get("member", "") or ""))
        except Exception as e:
            return self._error(e)
        return {"status": "ok", "messages": msgs}

    # ---------- lifecycle + status ----------

    async def _handle_status(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
        except Exception as e:
            return self._error(e)
        d = team_rt.definition
        tasks = {
            tid: {
                "title": t.title,
                "detail": t.detail,
                "assignee": t.assignee,
                "status": t.status,
                "blocked_by": list(t.blocked_by),
                "artifact_refs": list(t.artifact_refs),
                "episodes": list(t.episodes),
                "results": list((t.metadata or {}).get("results", [])),
            }
            for tid, t in team_rt._tasks.items()
        }
        return {
            "status": "ok",
            "team": d.name,
            "lifecycle": team_rt.lifecycle,
            "namespace_key": d.namespace_key,
            "memory_anchor": d.memory_anchor,
            "members": [
                {
                    "name": m.name,
                    "role": m.role,
                    "skills": list(m.skills),
                    "tool_allowlist": list(m.tool_allowlist),
                    "permission_mode": m.permission_mode,
                    "budget_tokens": m.budget_tokens,
                }
                for m in d.members
            ],
            "tasks": tasks,
            "inbox_counts": {name: len(v) for name, v in team_rt._inboxes.items()},
        }

    async def _handle_pause(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            team_rt.pause()
        except Exception as e:
            return self._error(e)
        return {"status": "ok", "lifecycle": team_rt.lifecycle}

    async def _handle_resume(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            team_rt.resume()
        except Exception as e:
            return self._error(e)
        return {"status": "ok", "lifecycle": team_rt.lifecycle}

    async def _handle_disband(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            team_rt.disband(cleanup_inboxes=bool(params.get("cleanup_inboxes", True)))
            self._registry().pop(team_rt.definition.name, None)
        except Exception as e:
            return self._error(e)
        logger.info("teams.disband team=%s", team_rt.definition.name)
        return {"status": "ok", "lifecycle": team_rt.lifecycle}

    async def _handle_list(self, params: dict) -> dict:
        return {
            "status": "ok",
            "teams": [
                {"name": k, "lifecycle": v.lifecycle, "members": [m.name for m in v.definition.members]}
                for k, v in sorted(self._registry().items())
            ],
        }

    # ---------- lead orchestration loop (#712) ----------

    def _lead_entry(self, name: str):
        return self._registry().get(("lead", str(name or "")))

    def _lead_client(self):
        daemon = self._daemon
        client = getattr(daemon, "mlx_client", None)
        if client is None:
            raise RuntimeError("no LLM client wired on the daemon (mlx_client missing)")
        return client

    async def _handle_lead_start(self, params: dict) -> dict:
        import asyncio

        from ..team_lead import LeadAlreadyRunningError, TeamLead

        try:
            team_rt = self._require_team(params)
            entry = self._lead_entry(team_rt.definition.name)
            if entry is not None and entry.get("task") is not None and not entry["task"].done():
                raise LeadAlreadyRunningError(
                    f"lead already running on team {team_rt.definition.name}: {entry.get('lead_id')}"
                )
            from ..playbook_store import PlaybookStore, playbook_enabled

            lead = TeamLead(
                team_rt,
                self._lead_client(),
                self._agent_fn(),
                playbook_store=PlaybookStore() if playbook_enabled() else None,
            )
            goal = str(params.get("goal", "") or "")
            if not goal:
                raise ValueError("goal required")

            async def _run():
                try:
                    payload = await lead.run(
                        goal,
                        max_rounds=int(params.get("max_rounds", 0) or 30),
                        max_stalls=int(params.get("max_stalls", 0) or 3),
                        team_budget=params.get("team_budget"),
                    )
                    logger.info("teams.lead finished team=%s lead=%s", team_rt.definition.name, lead.lead_id)
                    return payload
                finally:
                    entry["running"] = False

            entry = {"lead": lead, "lead_id": lead.lead_id, "running": True, "task": None}
            self._registry()[("lead", team_rt.definition.name)] = entry
            entry["task"] = asyncio.create_task(_run())
        except Exception as e:
            return self._error(e)
        logger.info("teams.lead_start team=%s lead=%s", team_rt.definition.name, lead.lead_id)
        return {"status": "ok", "lead_id": lead.lead_id}

    async def _handle_lead_stop(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            entry = self._lead_entry(team_rt.definition.name)
            if entry is None or entry.get("lead") is None:
                raise KeyError(f"no lead for team: {team_rt.definition.name}")
            entry["lead"].stop_requested = True
            round_ = entry["lead"].round
        except Exception as e:
            return self._error(e)
        logger.info("teams.lead_stop team=%s round=%d", team_rt.definition.name, round_)
        return {"status": "ok", "stopped": True, "round": round_}

    async def _handle_lead_status(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            entry = self._lead_entry(team_rt.definition.name)
        except Exception as e:
            return self._error(e)
        if entry is None or entry.get("lead") is None:
            return {"status": "ok", "running": False}
        lead = entry["lead"]
        running = entry.get("running", False) and not (
            entry.get("task") is not None and entry["task"].done()
        )
        return {
            "status": "ok",
            "running": bool(running),
            "lead_id": lead.lead_id,
            "round": lead.round,
            "n_stalls": lead.n_stalls,
            "replan_count": lead.replan_count,
            "last_verdict": lead.last_verdict,
            "verdict_log": lead.vlog.tail(20),
            "tasks": {
                tid: {"status": t.status, "assignee": t.assignee}
                for tid, t in team_rt._tasks.items()
            },
        }

    # ---------- team memory (namespace-scoped) ----------

    async def _handle_memory_write(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            from ..memory_engine import MemoryEngine

            engine = getattr(self._daemon._get_runtime(), "memory_engine", None) or MemoryEngine()
            entry_id = engine.store(
                content=str(params.get("content", "") or ""),
                scope="team:" + team_rt.definition.namespace_key,
                tags=str(params.get("tags", "") or ""),
                importance=int(params.get("importance", 0) or 5),
                metadata=dict(params.get("metadata") or {}),
                team_id=team_rt.definition.namespace_key,
            )
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.memory_write team=%s entry=%s", team_rt.definition.namespace_key, entry_id
        )
        return {"status": "ok", "entry_id": entry_id}

    async def _handle_memory_read(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            from ..memory_engine import MemoryEngine

            engine = getattr(self._daemon._get_runtime(), "memory_engine", None) or MemoryEngine()
            entries = engine.recall(
                query=str(params.get("query", "") or ""),
                scope="team:" + team_rt.definition.namespace_key,
                limit=int(params.get("limit", 0) or 10),
                team_id=team_rt.definition.namespace_key,
            )
        except Exception as e:
            return self._error(e)
        return {
            "status": "ok",
            "entries": [
                {"id": e.id, "content": e.content, "importance": e.importance, "created_at": e.created_at}
                for e in entries
            ],
        }
