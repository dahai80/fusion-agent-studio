# Issue #712: lead orchestration loop — Magentic-One-style two-ledger driver
# over the teams.* surface. All judgment lives here (consumer-side); the
# kernel TeamRuntime stays zero-model by contract.
#
# Outer loop = TaskLedger (goal/facts/plan, rebuilt only on re-plan); inner
# loop = ProgressLedger verdict (exactly five fields, parsed strictly, one
# JSON object per round). Stall arithmetic, task picking, completion and
# budget accounting are deterministic; the ONLY model calls are: task
# ledger, progress verdict, completion synthesis (and re-plans).
#
# Spec deviations (visible, per issue §10):
# - The kernel task board has no "cancelled" status: completion closes
#   result-less tasks as done with metadata {"lead_close": "cancelled",
#   "reason": ...}; budget-stop blocks non-done tasks with a self-reference
#   blocked_by placeholder (kernel requires non-empty blocked_by) plus the
#   metadata reason.
import json
import logging
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_JSON_RETRIES = 3
_RESULT_SUMMARY_CHARS = 200

TASK_LEDGER_PROMPT = (
    "You are the lead of a small agent team. Produce a task ledger.\n"
    "Team goal:\n{goal}\n\n"
    "Current task board:\n{board}\n\n"
    "{previous}"
    "Reply with EXACTLY one JSON object, no prose:\n"
    '{{"goal": "<verbatim goal>",\n'
    ' "facts": {{"given": [..], "to_look_up": [..], "to_derive": [..], "guesses": [..]}},\n'
    ' "plan": [{{"step": "<description>", "assignee_hint": "<member name or empty>", '
    '"depends_on": [<step indices>]}}],\n'
    ' "replan_count": <int>}}\n'
    "Plan steps must be concrete and ordered; depends_on refers to step indices "
    "(0-based) in this plan."
)

PROGRESS_LEDGER_PROMPT = (
    "You are the lead of a small agent team tracking progress.\n"
    "Team member names (next_speaker MUST be one of these EXACT names, "
    "never a role): {roster}\n\n"
    "Task ledger:\n{ledger}\n\n"
    "Current task board (with last results):\n{board}\n\n"
    "Last member result:\n{last_result}\n\n"
    "Stalls so far: {n_stalls}\n\n"
    "Reply with EXACTLY one JSON object, no prose:\n"
    '{{"is_request_satisfied": <bool>,\n'
    ' "is_in_loop": <bool>,\n'
    ' "is_progress_being_made": <bool>,\n'
    ' "next_speaker": "<member name>",\n'
    ' "instruction_or_question": "<the next member\'s brief objective>",\n'
    ' "reasons": {{"is_request_satisfied": "<line>", "is_in_loop": "<line>", '
    '"is_progress_being_made": "<line>", "next_speaker": "<line>", '
    '"instruction_or_question": "<line>"}}}}'
)

SYNTHESIS_PROMPT = (
    "You are the lead of a small agent team. The goal is complete.\n"
    "Goal:\n{goal}\n\n"
    "Final task board (with results):\n{board}\n\n"
    "Write a short synthesis (<= 10 sentences) of what the team produced "
    "and any open caveats. Plain prose, no JSON."
)


class LeadError(RuntimeError):
    pass


class LeadLedgerError(LeadError):
    pass


class LeadAlreadyRunningError(LeadError):
    pass


class LeadBudgetError(LeadError):
    pass


class LeadMaxRoundsError(LeadError):
    pass


class LeadStoppedError(LeadError):
    pass


def _estimate_tokens(text: str) -> int:
    # Repo-wide heuristic (artifact_tools/budget paths): chars // 4.
    return len(text or "") // 4


def _board_snapshot(team_rt) -> str:
    lines = []
    for tid in sorted(team_rt._tasks):
        t = team_rt._tasks[tid]
        results = (t.metadata or {}).get("results", [])
        last = ""
        if results:
            last = str(results[-1].get("summary", ""))[:_RESULT_SUMMARY_CHARS]
        lines.append(
            f"- {tid} | {t.title} | status={t.status} | assignee={t.assignee or '-'}"
            + (f" | last_result: {last}" if last else "")
        )
    return "\n".join(lines) or "(empty board)"


class VerdictLog:
    def __init__(self, state_dir: str):
        self.path = Path(state_dir) / "lead" / "verdicts.jsonl"

    def append(self, kind: str, round_: int, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), "kind": kind, "round": round_, "payload": payload}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def tail(self, limit: int = 20) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("verdict log read failed: %s", e)
            return rows
        return rows[-limit:]


class TeamLead:
    def __init__(self, team_rt, client, agent_fn, playbook_store=None):
        self.team_rt = team_rt
        self.client = client
        self.agent_fn = agent_fn
        self.playbook_store = playbook_store
        self.lead_id = "lead-" + uuid.uuid4().hex[:8]
        self.round = 0
        self.n_stalls = 0
        self.replan_count = 0
        self.last_verdict: dict | None = None
        self.running = False
        self.stop_requested = False
        self.budget_remaining: int | None = None
        self.vlog = VerdictLog(team_rt.state_dir)

    # ---------- model plumbing ----------

    async def _call(self, prompt: str, kind: str, round_: int) -> str:
        if self.budget_remaining is not None and self.budget_remaining <= 0:
            raise LeadBudgetError("lead budget exhausted")
        resp = await self.client.chat(model="", messages=[{"role": "user", "content": prompt}])
        content = str(getattr(resp, "content", "") or "")
        tokens = self._tokens_of(resp, content)
        if self.budget_remaining is not None:
            self.budget_remaining -= tokens
            if self.budget_remaining < 0:
                self.budget_remaining = 0
        logger.info("lead call team=%s kind=%s round=%d tokens=%s", self.team_rt.definition.name, kind, round_, tokens)
        return content

    @staticmethod
    def _tokens_of(resp, content: str) -> int:
        usage = getattr(resp, "usage", None)
        if isinstance(usage, dict):
            total = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
            if total:
                return int(total)
        return _estimate_tokens(content)

    @staticmethod
    def _extract_json(content: str) -> dict:
        # Tolerate fenced/leading-prose replies: take the outermost object.
        text = str(content or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in model reply")
        return json.loads(text[start : end + 1])

    # ---------- ledgers ----------

    async def task_ledger_call(self, goal: str, previous: dict | None = None) -> dict:
        prev = ""
        if previous is not None:
            prev = "Previous ledger (re-plan — keep what still holds, fix what does not):\n" + json.dumps(
                previous, ensure_ascii=False
            ) + "\n\n"
        prompt = TASK_LEDGER_PROMPT.format(goal=goal, board=_board_snapshot(self.team_rt), previous=prev)
        last_err = None
        for _ in range(_MAX_JSON_RETRIES):
            content = await self._call(prompt, "task_ledger", self.round)
            try:
                ledger = self._extract_json(content)
                self._validate_ledger(ledger)
                ledger["replan_count"] = self.replan_count + (1 if previous is not None else 0)
                return ledger
            except (ValueError, json.JSONDecodeError, KeyError, TypeError) as e:
                last_err = e
                logger.warning("task ledger parse retry: %s", e)
        raise LeadLedgerError(f"task ledger invalid after {_MAX_JSON_RETRIES} attempts: {last_err}")

    @staticmethod
    def _validate_ledger(ledger: dict) -> None:
        if not isinstance(ledger.get("goal"), str) or not ledger["goal"]:
            raise ValueError("ledger.goal required")
        facts = ledger.get("facts")
        if not isinstance(facts, dict) or any(not isinstance(facts.get(k), list) for k in ("given", "to_look_up", "to_derive", "guesses")):
            raise ValueError("ledger.facts must hold the four lists")
        plan = ledger.get("plan")
        if not isinstance(plan, list) or not plan:
            raise ValueError("ledger.plan must be a non-empty list")
        for step in plan:
            if not isinstance(step, dict) or not str(step.get("step", "")).strip():
                raise ValueError("plan steps need a non-empty step string")
            if not isinstance(step.get("depends_on", []), list):
                raise ValueError("depends_on must be a list")

    async def progress_verdict_call(self, ledger: dict, last_result: str) -> dict:
        roster = ", ".join(m.name for m in self.team_rt.definition.members)
        prompt = PROGRESS_LEDGER_PROMPT.format(
            roster=roster,
            ledger=json.dumps(ledger, ensure_ascii=False),
            board=_board_snapshot(self.team_rt),
            last_result=last_result[:1000] or "(none yet)",
            n_stalls=self.n_stalls,
        )
        member_names = {m.name for m in self.team_rt.definition.members}
        last_err = None
        for _ in range(_MAX_JSON_RETRIES):
            content = await self._call(prompt, "progress_verdict", self.round)
            try:
                verdict = self._extract_json(content)
                missing = [
                    f for f in ("is_request_satisfied", "is_in_loop", "is_progress_being_made", "next_speaker", "instruction_or_question")
                    if f not in verdict
                ]
                if missing:
                    raise ValueError("verdict missing fields: " + ", ".join(missing))
                if verdict["next_speaker"] not in member_names:
                    raise ValueError(f"next_speaker not a member: {verdict['next_speaker']!r}")
                verdict.setdefault("reasons", {})
                return verdict
            except (ValueError, json.JSONDecodeError, TypeError) as e:
                last_err = e
                logger.warning("progress verdict parse retry: %s", e)
        raise LeadLedgerError(f"progress verdict invalid after {_MAX_JSON_RETRIES} attempts: {last_err}")

    # ---------- board ops ----------

    def _create_tasks_from_plan(self, ledger: dict) -> list[str]:
        from fusion_harness.team_runtime import TeamTask

        valid_names = {m.name for m in self.team_rt.definition.members}
        ids: list[str] = []
        for step in ledger.get("plan", []):
            hint = str(step.get("assignee_hint", "") or "")
            task = TeamTask(
                title=str(step.get("step", ""))[:200],
                detail="lead plan step",
                assignee=hint if hint in valid_names else None,
                episodes=[self.lead_id],
            )
            ids.append(self.team_rt.add_task(task))
        for i, step in enumerate(ledger.get("plan", [])):
            deps = [ids[d] for d in step.get("depends_on", []) if isinstance(d, int) and 0 <= d < len(ids) and d != i]
            if deps:
                self.team_rt._tasks[ids[i]].blocked_by = deps
        return ids

    def _pick_task(self, member_name: str):
        done = {tid for tid, t in self.team_rt._tasks.items() if t.status == "done"}
        for tid in sorted(self.team_rt._tasks):
            t = self.team_rt._tasks[tid]
            if t.status != "todo":
                continue
            if t.assignee not in (member_name, None):
                continue
            if all(dep in done for dep in t.blocked_by):
                return t
        return None

    def _force_done(self, tid: str) -> None:
        # Kernel whitelist has no todo -> done edge; route through in_progress.
        t = self.team_rt._tasks[tid]
        if t.status == "todo":
            self.team_rt.transition_task(tid, "in_progress")
        if t.status != "done":
            self.team_rt.transition_task(tid, "done")

    def _close_board(self, reason: str) -> None:
        # Deviation (see module header): no "cancelled" status in the kernel
        # board — result-less tasks close as done with lead_close metadata.
        for tid, t in list(self.team_rt._tasks.items()):
            if t.status == "done":
                continue
            results = (t.metadata or {}).get("results", [])
            if results:
                self._force_done(tid)
            else:
                meta = dict(t.metadata or {})
                meta["lead_close"] = "cancelled"
                meta["reason"] = reason
                t.metadata = meta
                self._force_done(tid)

    def _block_board(self, reason: str) -> None:
        # Deviation (see module header): kernel requires non-empty blocked_by
        # for the blocked transition — self-reference placeholder + reason.
        for tid, t in list(self.team_rt._tasks.items()):
            if t.status in ("todo", "in_progress"):
                meta = dict(t.metadata or {})
                meta["lead_block_reason"] = reason
                t.metadata = meta
                self.team_rt.transition_task(tid, "blocked", blocked_by=[tid])

    def _emit_bus(self, event: str, payload: dict) -> None:
        bus = self.team_rt.bus
        if bus is None:
            return
        try:
            bus.emit("team", event, payload, session_id=self.team_rt.definition.name)
        except Exception as e:
            logger.warning("team lead bus emit failed: %s", e)

    def _maybe_playbook(self, task_ids: list[str], summary: str) -> None:
        if self.playbook_store is None:
            return
        try:
            self.playbook_store.append_lesson(
                f"team-{self.team_rt.definition.name}",
                f"lead run {self.lead_id} completed: {summary[:200]}",
                episodes=task_ids or [self.lead_id],
            )
        except Exception as e:
            logger.warning("team lead playbook append failed: %s", e)

    # ---------- main loop ----------

    async def run(self, goal: str, max_rounds: int = 30, max_stalls: int = 3, team_budget: int | None = None) -> dict:
        self.team_rt._require_active()
        self.running = True
        self.budget_remaining = team_budget if (team_budget and team_budget > 0) else None
        ledger = await self.task_ledger_call(goal)
        self.vlog.append("task_ledger", self.round, ledger)
        self._create_tasks_from_plan(ledger)
        last_result = ""
        try:
            while self.round < max_rounds:
                if self.stop_requested:
                    raise LeadStoppedError(f"lead stopped at round {self.round}")
                active = [t for t in self.team_rt._tasks.values() if t.status in ("todo", "in_progress", "blocked")]
                if not active:
                    return await self._complete(goal, "board drained", ledger)
                verdict = await self.progress_verdict_call(ledger, last_result)
                self.last_verdict = verdict
                self.vlog.append("verdict", self.round, verdict)
                if verdict["is_request_satisfied"]:
                    return await self._complete(goal, str(verdict.get("reasons", {}).get("is_request_satisfied", "satisfied")), ledger)
                if (not verdict["is_progress_being_made"]) or verdict["is_in_loop"]:
                    self.n_stalls += 1
                else:
                    self.n_stalls = max(0, self.n_stalls - 1)
                self.vlog.append("stall", self.round, {"n_stalls": self.n_stalls})
                if self.n_stalls >= max_stalls:
                    ledger = await self.task_ledger_call(goal, previous=ledger)
                    self._create_tasks_from_plan(ledger)
                    self.replan_count = ledger["replan_count"]
                    self.vlog.append("replan", self.round, {"replan_count": self.replan_count})
                    self.n_stalls = 0
                    self.round += 1
                    continue
                member = self.team_rt.definition.member(verdict["next_speaker"])
                task = self._pick_task(member.name)
                if task is None:
                    self.n_stalls += 1
                    self.vlog.append("stall", self.round, {"n_stalls": self.n_stalls, "reason": "no dispatchable task"})
                    self.round += 1
                    continue
                from fusion_harness.spawn_protocol import SpawnBrief

                brief = SpawnBrief(
                    objective=str(verdict["instruction_or_question"]),
                    output_format="report",
                    tool_guidance="allowlisted tools: " + ", ".join(member.tool_allowlist) if member.tool_allowlist else "no tools",
                    boundaries="stay within the task and allowlisted tools",
                    token_budget=member.budget_tokens,
                    concurrency=1,
                    depth=1,
                )
                result = await self.team_rt.run_member(member, task.id, brief, self.agent_fn)
                # Kernel run_member appends results but never transitions the
                # task — closing a completed task is consumer-side judgment.
                if task.status == "todo":
                    self.team_rt.transition_task(task.id, "in_progress")
                if result.status == "completed":
                    self.team_rt.transition_task(task.id, "done")
                last_result = str(result.summary or result.error or "")
                self.round += 1
            raise LeadMaxRoundsError(f"lead hit max_rounds={max_rounds} without completion")
        except LeadError as e:
            self.running = False
            self.vlog.append(
                "budget_stop" if isinstance(e, LeadBudgetError) else "stopped",
                self.round,
                {"error": str(e)},
            )
            if isinstance(e, LeadBudgetError):
                self._block_board("lead budget exhausted")
            raise
        except Exception as e:
            self.running = False
            self.vlog.append("stopped", self.round, {"error": str(e)})
            raise

    async def _complete(self, goal: str, reason: str, ledger: dict) -> dict:
        # Budget gate before the synthesis call.
        if self.budget_remaining is not None and self.budget_remaining <= 0:
            self.vlog.append("budget_stop", self.round, {"error": "lead budget exhausted"})
            self._block_board("lead budget exhausted")
            raise LeadBudgetError("lead budget exhausted")
        self._close_board(f"lead completion: {reason}")
        prompt = SYNTHESIS_PROMPT.format(goal=goal, board=_board_snapshot(self.team_rt))
        summary = (await self._call(prompt, "synthesis", self.round)).strip() or reason
        task_ids = sorted(self.team_rt._tasks)
        self._maybe_playbook(task_ids, summary)
        payload = {"summary": summary, "reason": reason, "replan_count": self.replan_count, "rounds": self.round}
        self.vlog.append("completion", self.round, payload)
        self.running = False
        self._emit_bus("team.lead.finished", {"team": self.team_rt.definition.name, "lead_id": self.lead_id, **payload})
        logger.info(
            "lead finished team=%s lead=%s rounds=%d replans=%d",
            self.team_rt.definition.name,
            self.lead_id,
            self.round,
            self.replan_count,
        )
        return payload
