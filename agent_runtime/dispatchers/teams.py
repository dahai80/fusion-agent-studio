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

# #781: kernel message types that surface as actionable inbox cards
# (web-shell#177 R3). shutdown_response is a reply type, not a card.
TYPED_INBOX_TYPES = ("plan_approval", "shutdown_request", "destructive_gate")


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
        raise ValueError(f"tools outside member allowlist ({member.name}): {', '.join(outside)}")


def member_persona_gate(member):
    # Persona bridge (#707): TeamMember.skills IS the #482 persona skills
    # allowlist — the same list shape PersonaGate.skill_allowed enforces, so
    # consumers can gate a member's skill usage with the existing mechanism.
    from ..persona_gate import PersonaGate

    return PersonaGate({"skills": list(member.skills)})


def teams_state_root() -> str:
    from ..paths import studio_home

    return str(studio_home() / "teams")


SPEC_FILENAME = "SPEC.md"

_SPEC_SKELETON = """# SPEC — {name}

Single source of truth for team conventions and role boundaries. The lead
consults this file before dividing new tasks; members receive the relevant
sections through their brief injection (harness side).

## Roles

## Conventions

## Interfaces

## Current split
"""


def team_spec_path(team_rt) -> str:
    return str(team_rt.state_dir) + "/" + SPEC_FILENAME


def _init_spec_file(state_dir: str, team_name: str, initial_text: str):
    # #745: teams.register seeds SPEC.md exactly once — a pre-existing file
    # (e.g. from a re-register or manual edit) is never clobbered.
    path = state_dir + "/" + SPEC_FILENAME
    if os.path.exists(path):
        logger.info("teams.register spec exists, leaving untouched path=%s", path)
        return None
    try:
        text = initial_text.strip() or _SPEC_SKELETON.format(name=team_name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError as e:
        logger.warning("teams.register spec init failed path=%s err=%s", path, e)
        return e
    logger.info("teams.register spec seeded path=%s", path)
    return None


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
            "teams.inbox_typed": self._handle_inbox_typed,
            "teams.inbox_respond": self._handle_inbox_respond,
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
            "teams.playbook_render": self._handle_teams_playbook_render,
            "teams.playbook_trace": self._handle_teams_playbook_trace,
            "teams.spec_read": self._handle_spec_read,
            "teams.spec_write": self._handle_spec_write,
            "teams.runs_list": self._handle_runs_list,
            "teams.run_messages": self._handle_run_messages,
            "teams.run_usage": self._handle_run_usage,
            "teams.supplement": self._handle_supplement,
            "teams.wakeup_create": self._handle_wakeup_create,
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
        # #747: structured dispatch errors — stable reason_code + operator
        # hint. Studio-home paths are operator-owned (local-first daemon) and
        # get shortened to ~; any other machine path collapses to
        # invocation_not_allowed with the full detail kept server-side only.
        from ..paths import studio_home
        from ..reason_codes import classify_error, error_payload, leaks_machine_detail

        detail = f"{type(e).__name__}: {e}"
        message = str(e or "").replace(str(studio_home()), "~")
        if leaks_machine_detail(message):
            logger.warning("teams.* rejected (detail withheld): %s", detail)
            return error_payload(
                "invocation_not_allowed", "operation rejected: internal detail withheld"
            )
        code = classify_error(e)
        logger.warning("teams.* rejected (%s): %s", code, detail)
        return error_payload(code, message)

    # ---------- registration ----------

    async def _handle_register(self, params: dict) -> dict:
        from fusion_harness.spawn_protocol import (
            FANOUT_COMPLEX_MAX,
            FANOUT_HARD_MAX,
            FANOUT_SIMPLE_MAX,
            TOKEN_MULTIPLIER_WARNING,
        )
        from fusion_harness.team import VALID_MODEL_TIERS, TeamDefinition, TeamMember
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
                    # #778: optional per-member tier hint, validated against
                    # the kernel vocabulary before any kernel call. Empty
                    # string = no hint; TeamRuntime falls back to the
                    # deterministic recommend_tier policy — AS never guesses.
                    model_tier=str(m.get("model_tier", "") or ""),
                    # #760: resident per-member brief — pass through verbatim
                    # so TeamRuntime.run_member can inject [member context].
                    context=str(m.get("context", "") or ""),
                )
                for m in (raw.get("members") or [])
            ]
            for m in members:
                if m.model_tier not in VALID_MODEL_TIERS:
                    raise ValueError(
                        "unknown model tier %r for member %s (valid: cheap, strong, or empty)"
                        % (m.model_tier, m.name)
                    )
            # #778: fan-out ladder pre-validation (#124 constants). The
            # kernel hard-errors above the hard max inside TeamDefinition
            # validation; anticipating it here gives the studio UI a typed
            # reason code with the ladder band instead of a raw kernel error.
            count = len(members)
            if count > FANOUT_HARD_MAX:
                raise ValueError(
                    "fanout hard max exceeded: %d members exceeds FANOUT_HARD_MAX=%d"
                    % (count, FANOUT_HARD_MAX)
                )
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
            state_dir=str(params.get("state_dir", "") or "")
            or teams_state_root() + "/" + definition.name,
        )
        try:
            os.makedirs(team_rt.state_dir, exist_ok=True)
        except OSError as e:
            return self._error(e)
        spec_error = _init_spec_file(
            team_rt.state_dir,
            definition.name,
            str(params.get("spec", "") or params.get("spec_text", "") or ""),
        )
        if spec_error is not None:
            return self._error(spec_error)
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
        # #778: ladder band always surfaced; TOKEN_MULTIPLIER_WARNING rides
        # the reason payload for teams above the complex band (16-20) so the
        # studio UI can show the ~15x token cost before any member runs.
        fanout_band = (
            "simple"
            if count <= FANOUT_SIMPLE_MAX
            else "complex"
            if count <= FANOUT_COMPLEX_MAX
            else "hard"
        )
        return {
            "status": "ok",
            "name": definition.name,
            "namespace_key": definition.namespace_key,
            "memory_anchor": definition.memory_anchor,
            "members": [m.name for m in definition.members],
            "state_dir": team_rt.state_dir,
            "fanout_band": fanout_band,
            "warning": TOKEN_MULTIPLIER_WARNING if fanout_band == "hard" else "",
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
                team_rt.definition,
                str(params.get("member", "") or ""),
                dict(params.get("brief") or {}),
            )
            reject_tools_outside_allowlist(member, [str(t) for t in (params.get("tools") or [])])
            errors = validate_brief(brief)
            if errors:
                raise ValueError("invalid spawn brief: " + "; ".join(errors))
            if not isinstance(brief, SpawnBrief):
                raise ValueError("brief must be a SpawnBrief contract dict")
            # #791: optional handoff verification (FUSION_HANDOFF_VERIFY=1).
            # Replay the brief's permission ledger against its handoff claims
            # before dispatch (harness handoff_verify, arXiv 2611.06545) — a
            # hallucinated or unauthorized claim blocks the spawn with the
            # typed handoff_verification_failed reason. Default off: brief
            # flow unchanged when the flag is unset.
            if os.environ.get("FUSION_HANDOFF_VERIFY", "0").strip() == "1":
                from fusion_harness.handoff_verify import verify_spawn_brief

                hv = verify_spawn_brief(brief)
                if not hv.ok:
                    detail = "; ".join(
                        str(f.get("reason", "unverified")) + ": " + str(f.get("tool", ""))
                        if f.get("tool")
                        else str(f.get("reason", "unverified"))
                        for f in hv.failures
                    )
                    raise ValueError("handoff verification failed: " + detail)
            result = await team_rt.run_member(
                member, str(params.get("task_id", "") or ""), brief, self._agent_fn()
            )
        except Exception as e:
            return self._error(e)
        # #714 part 2: full worker output lands in the kernel artifact channel
        # (ref-only boundary) — the RPC payload and the task-board metadata
        # carry the ref + the kernel-truncated summary, never the full text.
        artifact_ref = ""
        try:
            from ..artifact_channel import studio_artifact_channel

            channel = studio_artifact_channel()
            full_text = str(result.summary or result.error or "")
            if full_text:
                artifact_ref = channel.tools.capture(
                    member.name,
                    full_text,
                    kind="text",
                    run_id=str(params.get("task_id", "") or team_rt.definition.name),
                )
        except Exception as capture_err:
            logger.warning("artifact channel capture failed: %s", capture_err)
        logger.info(
            "teams.run_member team=%s member=%s task=%s status=%s tokens=%s",
            team_rt.definition.name,
            member.name,
            params.get("task_id"),
            result.status,
            result.tokens_spent,
        )
        # #778: tier provenance surfaced in the dispatch payload so the
        # studio UI can show why a member ran cheap/strong. Mirrors the
        # kernel resolution order for AS-shaped briefs (brief_from_event
        # carries no model_tier, so the "brief" source is unreachable here):
        # explicit member hint wins, else the deterministic policy default.
        if member.model_tier:
            tier, tier_source = member.model_tier, "explicit"
        else:
            from fusion_harness.tier_policy import recommend_tier

            tier, tier_source = recommend_tier("member_execution"), "policy"
        logger.info("teams.run_member tier=%s source=%s member=%s", tier, tier_source, member.name)
        return {
            "status": "ok",
            "result": {
                "subagent_id": result.subagent_id,
                "member": result.name,
                "status": result.status,
                "summary": result.summary,
                "artifact_ref": artifact_ref,
                "turns": result.turns,
                "tokens_spent": result.tokens_spent,
                "error": result.error,
                "via": "kernel-team-runtime",
                "partial": result.status == "capped",
                "tier": tier,
                "tier_source": tier_source,
            },
        }

    # ---------- mailbox ----------

    async def _handle_send(self, params: dict) -> dict:
        from fusion_harness.team_runtime import TeamMessage

        try:
            team_rt = self._require_team(params)
            # #716 (C2): full TeamMessage passthrough — msg_id/reply_to/
            # mentions/attachments/audio_ref/transcript ride params straight
            # into the kernel contract (reply_to must be msg_..., mentions/
            # attachments validated against the member list / artifact refs).
            msg = TeamMessage(
                type=str(params.get("type", "") or "text"),
                sender=str(params.get("from", "") or ""),
                recipient=str(params.get("to", "") or ""),
                body=str(params.get("body", "") or ""),
                msg_id=str(params.get("msg_id", "") or ""),
                reply_to=str(params.get("reply_to", "") or ""),
                mentions=tuple(str(m) for m in (params.get("mentions") or [])),
                attachments=tuple(str(a) for a in (params.get("attachments") or [])),
                audio_ref=str(params.get("audio_ref", "") or ""),
                transcript=str(params.get("transcript", "") or ""),
            )
            outcome = team_rt.send(msg)
            delivered = (
                bool(outcome.get("delivered")) if isinstance(outcome, dict) else bool(outcome)
            )
            msg_id = outcome.get("msg_id", "") if isinstance(outcome, dict) else ""
        except Exception as e:
            return self._error(e)
        if not delivered:
            return {"status": "error", "message": "mailbox delivery failed (disk write)"}
        logger.info(
            "teams.send team=%s from=%s to=%s type=%s msg_id=%s",
            team_rt.definition.name,
            msg.sender,
            msg.recipient,
            msg.type,
            msg_id,
        )
        return {"status": "ok", "delivered": True, "msg_id": msg_id}

    async def _handle_broadcast(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            # #716 (C2): mentions passthrough — kernel validates each against
            # the member list before anything is queued.
            delivered = team_rt.broadcast(
                str(params.get("from", "") or ""),
                str(params.get("body", "") or ""),
                type_=str(params.get("type", "") or "text"),
                mentions=tuple(str(m) for m in (params.get("mentions") or [])),
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

    # ---------- typed kernel inbox (#781) ----------

    @staticmethod
    def _typed_inbox_entries(team_rt, member: str, include_answered: bool) -> list:
        # #781: kernel-typed messages (plan_approval / shutdown_request)
        # surfaced as actionable inbox cards. A card is "answered" once a
        # reply with reply_to == msg_id sits in the ORIGINAL SENDER's inbox
        # (inbox_respond delivers the decision there — the kernel mailbox is
        # per-recipient, so the member's own inbox never shows the reply).
        out = []
        for m in team_rt.inbox(member):
            mtype = str(m.get("type", ""))
            if mtype not in TYPED_INBOX_TYPES:
                continue
            msg_id = str(m.get("msg_id", ""))
            sender = str(m.get("sender", ""))
            answered = False
            if msg_id and sender:
                try:
                    answered = any(
                        str(r.get("reply_to", "")) == msg_id for r in team_rt.inbox(sender)
                    )
                except Exception:
                    answered = False
            if answered and not include_answered:
                continue
            entry = dict(m)
            entry["answered"] = answered
            entry["kind"] = mtype
            out.append(entry)
        return out

    async def _handle_inbox_typed(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            member = str(params.get("member", "") or "")
            team_rt.definition.member(member)  # typed member_unknown on miss
            include_answered = bool(params.get("answered", False))
            entries = self._typed_inbox_entries(team_rt, member, include_answered)
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.inbox_typed team=%s member=%s pending=%d",
            team_rt.definition.name,
            member,
            len(entries),
        )
        return {"status": "ok", "messages": entries}

    def _resolve_destructive_gate(self, card: dict, decision: str) -> str:
        # #785: a destructive_gate card body carries the host approval call
        # id as a "gate_call_id=<id>" line. Resolve the pending
        # ApprovalGate question over the shared HumanGate queue — the same
        # queue the funnel's gate stage parks on (#770) and the
        # human_input.resolve surface answers (#557). Missing or unknown
        # call ids fail loudly: an approval that resolves nothing is worse
        # than an error.
        body = str(card.get("body", ""))
        call_id = ""
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("gate_call_id="):
                call_id = s.split("=", 1)[1].strip()
                break
        if not call_id:
            raise ValueError(
                "destructive_gate card %s missing gate_call_id line" % card.get("msg_id", "")
            )
        runtime = None
        get_runtime = getattr(self._daemon, "_get_runtime", None)
        if callable(get_runtime):
            runtime = get_runtime()
        if runtime is None:
            runtime = getattr(self._daemon, "_runtime", None)
        if runtime is None or not hasattr(runtime, "_get_human_gate"):
            raise RuntimeError("no runtime with human gate support for destructive_gate response")
        gate = runtime._get_human_gate()
        question = None
        try:
            for q in gate.list_questions(status="pending"):
                if str((q.payload or {}).get("call_id", "")) == call_id:
                    question = q
                    break
        except Exception as exc:
            raise RuntimeError("destructive gate queue lookup failed: %s" % exc)
        if question is None:
            raise ValueError("no pending destructive gate for call %s" % call_id)
        outcome = gate.resolve(question.id, {"approved": decision == "approved"})
        if not outcome.get("resolved"):
            raise RuntimeError(
                "destructive gate resolve failed for %s: %s"
                % (question.id, outcome.get("reason", ""))
            )
        logger.info(
            "destructive gate resolved via inbox card call=%s question=%s decision=%s",
            call_id,
            question.id,
            decision,
        )
        return question.id

    async def _handle_inbox_respond(self, params: dict) -> dict:
        from fusion_harness.team_runtime import TeamMessage

        try:
            team_rt = self._require_team(params)
            member = str(params.get("member", "") or "")
            team_rt.definition.member(member)
            message_id = str(params.get("message_id", "") or "")
            decision_raw = str(params.get("decision", "") or "").strip().lower()
            decision = {
                "approve": "approved",
                "accept": "approved",
                "reject": "rejected",
                "decline": "rejected",
            }.get(decision_raw, "")
            if not decision:
                raise ValueError(
                    "invalid decision %r (valid: approve/reject or accept/decline)" % decision_raw
                )
            if not message_id.startswith("msg_"):
                raise ValueError("message_id must be a msg_id (msg_...): %r" % message_id)
            inbox = team_rt.inbox(member)
            target = next((m for m in inbox if str(m.get("msg_id", "")) == message_id), None)
            if target is None:
                raise ValueError("message not in member inbox: %s" % message_id)
            mtype = str(target.get("type", ""))
            if mtype not in TYPED_INBOX_TYPES:
                raise ValueError(
                    "message %s is type %r, not an actionable typed message (%s)"
                    % (message_id, mtype, "/".join(sorted(TYPED_INBOX_TYPES)))
                )
            sender = str(target.get("sender", ""))
            try:
                already = any(
                    str(m.get("reply_to", "")) == message_id for m in team_rt.inbox(sender)
                )
            except Exception:
                already = False  # sender inbox unreadable; respond still idempotent-guarded by the card scan
            if already:
                raise ValueError("message already answered: %s" % message_id)
            gate_question_id = ""
            if mtype == "destructive_gate":
                gate_question_id = self._resolve_destructive_gate(target, decision)
            # Kernel reply-type mapping: shutdown_request answers with the
            # typed shutdown_response; plan_approval has no typed response
            # in kernel MSG_TYPES, so the decision rides a text reply with
            # reply_to anchoring it to the original message. Body is the
            # bare decision word — deterministic, machine-parseable.
            reply_type = "shutdown_response" if mtype == "shutdown_request" else "text"
            reply = TeamMessage(
                type=reply_type,
                sender=member,
                recipient=str(target.get("sender", "")),
                body=decision,
                reply_to=message_id,
            )
            outcome = team_rt.send(reply)
            delivered = (
                bool(outcome.get("delivered")) if isinstance(outcome, dict) else bool(outcome)
            )
            reply_id = outcome.get("msg_id", "") if isinstance(outcome, dict) else ""
            if not delivered:
                raise RuntimeError("mailbox delivery failed for reply to %s" % message_id)
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.inbox_respond team=%s member=%s message=%s type=%s decision=%s reply=%s",
            team_rt.definition.name,
            member,
            message_id,
            mtype,
            decision,
            reply_id,
        )
        return {
            "status": "ok",
            "message_id": message_id,
            "decision": decision,
            "reply_type": reply_type,
            "reply_msg_id": reply_id,
            "gate_question_id": gate_question_id,
        }

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
                # #761: read-only console metadata. Armed (not-yet-fired)
                # wakeup rules mirror the kernel's _armed_wakeups filter;
                # expired rules are pruned by the kernel itself, so nothing
                # extra to filter here. Steer counter is derived from the
                # durable supplement receipts for the current run attempt —
                # same number _steer_counts tracks in memory, but it survives
                # daemon restarts and stays consistent across operators.
                "wakeups": self._armed_wakeup_views(t),
                "steers_used": sum(
                    1
                    for r in (t.metadata or {}).get("supplements", [])
                    if r.get("attempt") == self._current_attempt(team_rt, tid)
                ),
                "steer_cap": self.STEER_CAP_PER_RUN,
            }
            for tid, t in team_rt._tasks.items()
        }
        # #726 (C3-enable): the kernel TeamRuntime has no per-member presence
        # state (members are configuration; execution is per run_member), so
        # presence is DERIVED deterministically from the team lifecycle —
        # ACTIVE members render as available, PAUSED as paused, everything
        # else (DEFINED/DISBANDED) as offline. The @fusion/chat adapter
        # should gray out non-available members.
        lifecycle_presence = {
            "ACTIVE": "available",
            "PAUSED": "paused",
        }
        presence = lifecycle_presence.get(team_rt.lifecycle, "offline")
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
                    "presence": presence,
                }
                for m in d.members
            ],
            "tasks": tasks,
            "inbox_counts": {name: len(v) for name, v in team_rt._inboxes.items()},
        }

    @staticmethod
    def _armed_wakeup_views(task) -> list:
        # #761: console-facing projection of a task's armed wakeup rules
        # (kind at|every + their scheduling fields). Fired rules are excluded
        # — they mirror the kernel's _armed_wakeups semantics.
        views = []
        for rule in (task.metadata or {}).get("wakeups", []):
            if rule.get("fired"):
                continue
            view = {"wake_id": rule.get("wake_id"), "kind": rule.get("kind")}
            for key in ("at", "every", "on_timeout", "expires_at"):
                if rule.get(key) is not None:
                    view[key] = rule[key]
            views.append(view)
        return views

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
                {
                    "name": k,
                    "lifecycle": v.lifecycle,
                    "members": [m.name for m in v.definition.members],
                }
                for k, v in sorted(self._registry().items())
            ],
        }

    # ---------- team SPEC.md (#745) ----------

    def _require_writable_team(self, team_rt):
        # #745: SPEC writes are console/human-driven in v1 and are refused on
        # teams that are not actively running — paused/disbanded teams must
        # not have their source of truth mutated underneath a frozen state.
        lifecycle = str(getattr(team_rt, "lifecycle", "") or "")
        if lifecycle != "ACTIVE":
            raise ValueError(
                f"team {team_rt.definition.name} is {lifecycle or 'in an unknown lifecycle state'}; "
                "spec_write is only allowed on ACTIVE teams"
            )

    async def _handle_spec_read(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
        except Exception as e:
            return self._error(e)
        path = team_spec_path(team_rt)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            # #747: route through _error so the payload carries
            # reason_code=spec_unavailable (message text unchanged for #745
            # clients).
            logger.warning(
                "teams.spec_read team=%s missing spec path=%s", team_rt.definition.name, path
            )
            return self._error(
                FileNotFoundError(f"no SPEC.md exists yet for team {team_rt.definition.name}")
            )
        except OSError as e:
            return self._error(e)
        logger.info("teams.spec_read team=%s bytes=%d", team_rt.definition.name, len(text))
        return {"status": "ok", "team": team_rt.definition.name, "path": path, "spec": text}

    async def _handle_spec_write(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            text = str(params.get("text", "") or "")
            if not text.strip():
                raise ValueError("text required (non-empty SPEC.md body)")
            self._require_writable_team(team_rt)
            path = team_spec_path(team_rt)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text if text.endswith("\n") else text + "\n")
        except Exception as e:
            return self._error(e)
        logger.info("teams.spec_write team=%s bytes=%d", team_rt.definition.name, len(text))
        return {"status": "ok", "team": team_rt.definition.name, "path": path, "bytes": len(text)}

    # ---------- playbook read surface (#733) ----------

    def _require_playbook_store(self, team_rt):
        # #733: read-only over the kernel — the store layout is the shared
        # ops.jsonl the lead loop writes (#712); team gating is the
        # TeamDefinition.playbook flag. fusion_harness/ diff stays empty.
        from ..playbook_store import PlaybookStore

        if not getattr(team_rt.definition, "playbook", False):
            raise LookupError(
                f"playbook disabled on team {team_rt.definition.name} (definition.playbook=false)"
            )
        store = PlaybookStore()
        if not store.ops_path.is_file():
            raise FileNotFoundError(
                f"no playbook store exists yet (ops log missing under {store.dir})"
            )
        return store

    async def _handle_teams_playbook_render(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            store = self._require_playbook_store(team_rt)
        except (KeyError, LookupError, FileNotFoundError) as e:
            return self._error(e)
        entries = store.entries()
        logger.info(
            "teams.playbook_render team=%s entries=%d", team_rt.definition.name, len(entries)
        )
        return {
            "status": "ok",
            "team": team_rt.definition.name,
            "rendered": store.book.render(),
            "entries": entries,
        }

    async def _handle_teams_playbook_trace(self, params: dict) -> dict:
        entry_id = str(params.get("entry_id", "") or "")
        try:
            team_rt = self._require_team(params)
            if not entry_id:
                raise ValueError("entry_id required")
            store = self._require_playbook_store(team_rt)
        except (KeyError, LookupError, FileNotFoundError, ValueError) as e:
            return self._error(e)
        episodes = store.book.trace(entry_id)
        if not episodes:
            return self._error(
                LookupError(f"playbook entry not found or has no provenance: {entry_id}")
            )
        logger.info(
            "teams.playbook_trace team=%s entry=%s episodes=%d",
            team_rt.definition.name,
            entry_id,
            len(episodes),
        )
        return {
            "status": "ok",
            "team": team_rt.definition.name,
            "entry_id": entry_id,
            "episodes": episodes,
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
                    logger.info(
                        "teams.lead finished team=%s lead=%s", team_rt.definition.name, lead.lead_id
                    )
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
                {
                    "id": e.id,
                    "content": e.content,
                    "importance": e.importance,
                    "created_at": e.created_at,
                }
                for e in entries
            ],
        }

    # ---------- run replay surface (#750, harness #63 streams) ----------

    @staticmethod
    def _runs_dir(team_rt) -> str:
        # One JSONL per run at <state_dir>/runs/<task_id>/<attempt>.jsonl
        # (fusion-harness #63 convention); read-only here.
        import os

        return os.path.join(team_rt.state_dir, "runs")

    def _load_run_stream(self, team_rt, task_id: str, attempt: int):
        import os

        from fusion_harness.session_log import SessionEventLog

        path = os.path.join(self._runs_dir(team_rt), str(task_id), f"{attempt}.jsonl")
        if not os.path.isfile(path):
            raise ValueError(f"no run attempt {attempt} for task {task_id}")
        return SessionEventLog.load(path)

    async def _handle_runs_list(self, params: dict) -> dict:
        import os

        try:
            team_rt = self._require_team(params)
            task_id = str(params.get("task_id", "") or "")
            team_rt.task(task_id)
            runs_dir = os.path.join(self._runs_dir(team_rt), task_id)
            attempts = []
            if os.path.isdir(runs_dir):
                for name in sorted(os.listdir(runs_dir)):
                    if not (name.endswith(".jsonl") and name[:-6].isdigit()):
                        continue
                    attempt = int(name[:-6])
                    log = self._load_run_stream(team_rt, task_id, attempt)
                    events = log.events()
                    member = ""
                    last_status = ""
                    finished = {}
                    for ev in events:
                        payload = ev.to_dict().get("payload") or {}
                        member = member or str(payload.get("member", "") or "")
                        if ev.type == "run_finished":
                            finished = payload.get("usage") or {}
                            last_status = str(payload.get("status", "") or "")
                    attempts.append(
                        {
                            "attempt": attempt,
                            "member": member,
                            "events": len(events),
                            "status": last_status,
                            "usage": finished,
                        }
                    )
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.runs_list team=%s task=%s runs=%d",
            team_rt.definition.name,
            task_id,
            len(attempts),
        )
        return {
            "status": "ok",
            "team": team_rt.definition.name,
            "task_id": task_id,
            "runs": attempts,
        }

    async def _handle_run_messages(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            task_id = str(params.get("task_id", "") or "")
            team_rt.task(task_id)
            attempt = int(params.get("attempt", 0) or 0)
            if attempt <= 0:
                raise ValueError("attempt required (positive integer)")
            log = self._load_run_stream(team_rt, task_id, attempt)
            messages = [
                {
                    "seq": ev.seq,
                    "ts": ev.ts,
                    "type": ev.type,
                    "payload": ev.to_dict().get("payload") or {},
                }
                for ev in log.events()
            ]
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.run_messages team=%s task=%s attempt=%d events=%d",
            team_rt.definition.name,
            task_id,
            attempt,
            len(messages),
        )
        return {
            "status": "ok",
            "team": team_rt.definition.name,
            "task_id": task_id,
            "attempt": attempt,
            "messages": messages,
        }

    async def _handle_run_usage(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            task_id = str(params.get("task_id", "") or "")
            team_rt.task(task_id)
            import os

            runs_dir = os.path.join(self._runs_dir(team_rt), task_id)
            rows = []
            if os.path.isdir(runs_dir):
                for name in sorted(os.listdir(runs_dir)):
                    if not (name.endswith(".jsonl") and name[:-6].isdigit()):
                        continue
                    attempt = int(name[:-6])
                    log = self._load_run_stream(team_rt, task_id, attempt)
                    usage = {}
                    status = ""
                    for ev in log.events():
                        if ev.type != "run_finished":
                            continue
                        payload = ev.to_dict().get("payload") or {}
                        usage = payload.get("usage") or {}
                        status = str(payload.get("status", "") or "")
                    rows.append({"attempt": attempt, "status": status, "usage": usage})
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.run_usage team=%s task=%s rows=%d", team_rt.definition.name, task_id, len(rows)
        )
        return {"status": "ok", "team": team_rt.definition.name, "task_id": task_id, "usage": rows}

    # ---------- mid-run steering (#748, harness #64 queue) ----------

    STEER_CAP_PER_RUN = 3

    def _current_attempt(self, team_rt, task_id: str) -> int:
        import os

        runs_dir = os.path.join(self._runs_dir(team_rt), task_id)
        try:
            names = os.listdir(runs_dir)
        except OSError:
            return 0
        attempts = [int(n[:-6]) for n in names if n.endswith(".jsonl") and n[:-6].isdigit()]
        return max(attempts) if attempts else 0

    async def _handle_supplement(self, params: dict) -> dict:
        import time

        from fusion_harness.team_runtime import TeamMessage

        try:
            team_rt = self._require_team(params)
            task_id = str(params.get("task_id", "") or "")
            task = team_rt.task(task_id)
            content = str(params.get("content", "") or "")
            if not content.strip():
                raise ValueError("content required (non-empty supplement body)")
            origin = str(params.get("origin", "") or "operator")
            if origin not in ("operator", "lead"):
                raise ValueError("origin must be 'operator' or 'lead'")
            attempt = self._current_attempt(team_rt, task_id)
            if not hasattr(self, "_steer_counts"):
                self._steer_counts = {}
            key = (team_rt.definition.namespace_key, task_id, attempt)
            used = self._steer_counts.get(key, 0)
            if used >= self.STEER_CAP_PER_RUN:
                raise ValueError(
                    f"supplement cap reached: {self.STEER_CAP_PER_RUN} steers per run "
                    f"(task={task_id} attempt={attempt})"
                )
            seq = int(task.metadata.get("supplement_seq", 0) or 0) + 1
            task.metadata["supplement_seq"] = seq
            supplement_id = f"sp_{seq}"
            queued = team_rt.steer_member(task_id, content, origin=origin)
            if queued:
                status = "delivered"
            else:
                # No active run: degrade to the assignee's inbox (visible
                # next round) instead of dropping the note on the floor.
                recipient = str(task.assignee or "")
                if not recipient:
                    raise ValueError(
                        f"no active run for task {task_id} and task has no assignee for inbox downgrade"
                    )
                outcome = team_rt.send(
                    TeamMessage(
                        type="text",
                        # Mailbox contract (#31) requires a member sender;
                        # the assignee self-notes with the origin preserved
                        # in the body.
                        sender=recipient,
                        recipient=recipient,
                        body=f"[steer-late] {origin} note: {content.strip()}",
                    )
                )
                delivered = (
                    bool(outcome.get("delivered")) if isinstance(outcome, dict) else bool(outcome)
                )
                if not delivered:
                    raise ValueError("mailbox delivery failed for supplement downgrade")
                status = "delivered_as_inbox"
            self._steer_counts[key] = used + 1
            task.metadata.setdefault("supplements", []).append(
                {
                    "supplement_id": supplement_id,
                    "origin": origin,
                    "content": content.strip(),
                    "status": status,
                    "attempt": attempt,
                    "created": time.time(),
                }
            )
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.supplement team=%s task=%s id=%s status=%s attempt=%d used=%d",
            team_rt.definition.name,
            task_id,
            supplement_id,
            status,
            attempt,
            self._steer_counts[key],
        )
        return {
            "status": "ok",
            "team": team_rt.definition.name,
            "task_id": task_id,
            "supplement_id": supplement_id,
            "receipt": status,
            "attempt": attempt,
            "steers_used": self._steer_counts[key],
            "steer_cap": self.STEER_CAP_PER_RUN,
        }

    # ---------- task wakeup rules (#749, harness #65 storage) ----------

    async def _handle_wakeup_create(self, params: dict) -> dict:
        try:
            team_rt = self._require_team(params)
            task_id = str(params.get("task_id", "") or "")
            team_rt.task(task_id)
            rule = dict(params.get("rule") or {})
            for key in ("kind", "at", "every", "on_timeout", "expires_at", "on_event"):
                if key in params and key not in rule:
                    rule[key] = params[key]
            stored = team_rt.add_wakeup(task_id, rule)
        except Exception as e:
            return self._error(e)
        logger.info(
            "teams.wakeup_create team=%s task=%s wake=%s kind=%s",
            team_rt.definition.name,
            task_id,
            stored.get("wake_id"),
            stored.get("kind"),
        )
        return {
            "status": "ok",
            "team": team_rt.definition.name,
            "task_id": task_id,
            "wakeup": stored,
        }
