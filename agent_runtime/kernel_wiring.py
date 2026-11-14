# Issue #675: wire fusion-harness 0.10 kernel surfaces into AgentRuntime.
#
# Five wirings, each env-gated with fail-open behavior (a wiring failure
# logs and degrades — it never breaks runtime init):
#   1. token counter      -> CompactionConfig.token_counter (CJK-aware)
#   2. sandbox policy     -> kernel SandboxPolicy gate on the tool funnel
#                            (optional sandbox-exec runner on darwin)
#   3. skill registry     -> SkillsStore skills -> kernel SkillRegistry +
#                            derive_permissions rules -> PermissionEngine
#   4. kernel checkpoints -> CheckpointStore mirror at tool-call boundaries
#   5. orchestrator       -> kernel SubagentRunner/BackgroundPool/ParallelTeam
#                            delegating to the in-tree SubAgentRunner
import asyncio
import logging
import os
import shutil
import sys

from fusion_harness.checkpoint import CheckpointState, CheckpointStore
from fusion_harness.compactor import CompactionConfig
from fusion_harness.orchestrator import BackgroundPool, ParallelTeam, SubagentRunner
from fusion_harness.sandbox import ExecutionTier, SandboxPolicy
from fusion_harness.skills import (
    Skill,
    SkillError,
    SkillGrant,
    SkillRegistry,
    derive_permissions,
    parse_skill,
)
from fusion_harness.tokens import CJKAwareHeuristicTokenCounter

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


# ---------------- 1. token counter ----------------

def build_compaction_config() -> CompactionConfig | None:
    if _flag("FUSION_TOKEN_COUNTER", "0"):
        counter = CJKAwareHeuristicTokenCounter()
        logger.info("kernel token counter wired: CJKAwareHeuristicTokenCounter")
        return CompactionConfig(token_counter=counter)
    logger.info("kernel token counter disabled (FUSION_TOKEN_COUNTER)")
    return None


# ---------------- 2. sandbox policy ----------------

class SandboxExecRunner:
    # macOS sandbox-exec runner (kernel SandboxRunner protocol). Executes
    # shell-command tools under /usr/bin/sandbox-exec with a profile that
    # denies network + system-path writes. Non-command tools are rejected.
    _PROFILE = (
        '(version 1)(allow default)'
        '(deny network*)'
        '(deny file-write* (subpath "/usr") (subpath "/bin")'
        ' (subpath "/sbin") (subpath "/etc") (subpath "/private/etc"))'
    )

    def __init__(self):
        self.bin = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"

    async def run_sandboxed(self, tool_name: str, args: dict | None) -> str:
        command = str((args or {}).get("command", "") or "")
        if not command:
            raise ValueError(
                f"sandbox-exec runner supports shell-command tools only, got {tool_name}"
            )
        logger.info("sandbox-exec running tool=%s command=%r", tool_name, command[:120])
        proc = await asyncio.create_subprocess_exec(
            self.bin, "-p", self._PROFILE, "/bin/sh", "-c", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        text = out.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            err_text = err.decode("utf-8", errors="replace").strip()
            text = (text + ("\n" + err_text if err_text else "")).strip()
            logger.warning(
                "sandbox-exec exit=%d tool=%s err=%s", proc.returncode, tool_name, err_text[:200]
            )
        return text


def build_sandbox_policy() -> SandboxPolicy | None:
    if not _flag("FUSION_SANDBOX_POLICY", "0"):
        logger.info("kernel sandbox policy disabled (FUSION_SANDBOX_POLICY)")
        return None
    runner = None
    if _flag("FUSION_SANDBOX_EXEC", "0"):
        if sys.platform != "darwin":
            logger.warning("FUSION_SANDBOX_EXEC set but platform is not darwin — no runner")
        elif not (shutil.which("sandbox-exec") or os.path.exists("/usr/bin/sandbox-exec")):
            logger.warning("FUSION_SANDBOX_EXEC set but sandbox-exec binary missing")
        else:
            runner = SandboxExecRunner()
            logger.info("sandbox-exec runner wired")
    blocked = [
        t.strip()
        for t in os.environ.get("FUSION_SANDBOX_BLOCKED_TOOLS", "").split(",")
        if t.strip()
    ]
    policy = SandboxPolicy(runner=runner, tool_tiers={t: ExecutionTier.BLOCKED for t in blocked})
    logger.info(
        "kernel sandbox policy wired: runner=%s blocked_tools=%d threshold=%s",
        "sandbox-exec" if runner else "none",
        len(blocked),
        policy.risk_threshold,
    )
    return policy


# ---------------- 3. skill registry + derived permissions ----------------

def build_skill_registry(runtime) -> tuple[SkillRegistry, int] | None:
    engine = getattr(runtime, "permission_engine", None)
    try:
        from .skills_store import DEFAULT_SKILLS_DIR, SkillsStore

        skills_dir = os.environ.get("FUSION_SKILLS_DIR", "").strip() or DEFAULT_SKILLS_DIR
        store = SkillsStore(skills_dir=skills_dir)
        metas = store.list_skills()
    except Exception as e:
        logger.warning("kernel skill registry skipped: skills store unavailable: %s", e)
        return None
    registry = SkillRegistry()
    applied = 0
    for meta in metas:
        name = str(meta.get("name", ""))
        if not name:
            continue
        try:
            skill = _build_skill(store, meta)
            registry.register(skill)
            rules = derive_permissions(skill)
            if engine is not None:
                from .permissions import PermissionRule

                for r in rules:
                    engine.add_rule(PermissionRule(**r), layer="node")
            applied += len(rules)
            logger.info("kernel skill registered: %s rules=%d", name, len(rules))
        except (SkillError, ValueError, OSError) as e:
            logger.warning("kernel skill skipped name=%s: %s", name, e)
    logger.info(
        "kernel skill registry wired: skills=%d derived_rules=%d", len(metas), applied
    )
    return registry, applied


def _build_skill(store, meta: dict) -> Skill:
    # Prefer harness parse_skill on the raw SKILL.md (fail-closed on
    # malformed permission clauses); fall back to studio meta mapping.
    path = str(meta.get("path", ""))
    raw = ""
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            raw = ""
    frontmatter = raw.split("---", 2)[1] if raw.startswith("---") else ""
    if "permissions:" in frontmatter:
        return parse_skill(raw, source=path)
    loaded = store.load_skill(str(meta.get("name", "")))
    body = str(loaded.get("body", "")) if loaded else ""
    return Skill(
        name=str(meta.get("name", "")),
        description=str(meta.get("description", "")),
        body=body,
        grant=SkillGrant(allow_tools=list(meta.get("allowed_tools") or [])),
        source=str(meta.get("source", "studio")),
    )


# ---------------- 4. kernel checkpoints ----------------

class KernelCheckpointer:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.store = CheckpointStore(path=path)
        logger.info("kernel checkpoint store wired: %s", path)

    def save(
        self,
        session_id: str,
        messages: list,
        budget_spent_tokens: int = 0,
        permission_mode: str = "",
        metadata: dict | None = None,
    ) -> int | None:
        try:
            seq = self.store.save(
                session_id,
                CheckpointState(
                    messages=list(messages),
                    budget_spent_tokens=int(budget_spent_tokens),
                    permission_mode=str(permission_mode),
                    outbox_offset=0,
                    metadata=dict(metadata or {}),
                ),
            )
            logger.info(
                "kernel checkpoint saved session=%s seq=%s messages=%d",
                session_id,
                seq,
                len(messages),
            )
            return seq
        except Exception as e:
            logger.warning("kernel checkpoint save failed session=%s: %s", session_id, e)
            return None

    def latest(self, session_id: str) -> CheckpointState | None:
        try:
            return self.store.latest(session_id)
        except Exception as e:
            logger.warning("kernel checkpoint load failed session=%s: %s", session_id, e)
            return None


def attach_kernel_checkpointer(runtime) -> KernelCheckpointer | None:
    if not _flag("FUSION_KERNEL_CHECKPOINTS", "1"):
        logger.info("kernel checkpoints disabled (FUSION_KERNEL_CHECKPOINTS)")
        return None
    try:
        from .paths import studio_home

        cp = KernelCheckpointer(str(studio_home() / "kernel_checkpoints.db"))
    except Exception as e:
        logger.warning("kernel checkpoint store unavailable: %s", e)
        return None
    runtime.kernel_checkpoints = cp
    return cp


# ---------------- 5. orchestrator ----------------

def make_studio_agent_fn(runtime):
    # Bridge a kernel SubagentSpec to a real studio subagent run. Shared by
    # the kernel orchestrator (#701) and the teams dispatcher (#707).
    from .subagent import SubAgentRunner as StudioSubAgentRunner
    from .subagent import SubAgentSpec as StudioSubAgentSpec

    async def agent_fn(spec, session) -> str:
        runner = getattr(runtime, "subagent_runner", None)
        if runner is None:
            runner = StudioSubAgentRunner(runtime)
            runtime.subagent_runner = runner
        sspec = StudioSubAgentSpec(
            name=spec.name,
            task=spec.prompt,
            tools=list(spec.tool_allowlist),
            permission_mode=str(spec.permission_mode or ""),
            max_turns=int(spec.max_turns or 8),
            budget_tokens=int(spec.budget_tokens or 0),
        )
        report = await runner.run(sspec)
        text = str(getattr(report, "report", "") or "")
        if not getattr(report, "ok", True) and getattr(report, "error", None):
            text = text or f"subagent failed: {report.error}"
        logger.info(
            "kernel agent_fn subagent done name=%s ok=%s turns=%s",
            spec.name,
            getattr(report, "ok", None),
            getattr(report, "turns", None),
        )
        return text

    return agent_fn


def build_kernel_orchestrator(runtime) -> dict:
    runner = SubagentRunner(bus=getattr(runtime, "lifecycle_bus", None))
    pool = BackgroundPool(runner)
    team = ParallelTeam(runner)
    logger.info("kernel orchestrator wired: subagent_runner+background_pool+parallel_team")
    return {"runner": runner, "pool": pool, "team": team, "agent_fn": make_studio_agent_fn(runtime)}


# ---------------- entry point ----------------

def wire_kernel_surfaces(runtime) -> dict:
    wired: dict = {}
    try:
        cfg = build_compaction_config()
        if cfg is not None:
            if getattr(runtime, "compactor", None) is not None:
                runtime.compactor.config = cfg
            wired["token_counter"] = True
    except Exception as e:
        logger.warning("kernel token counter wiring failed: %s", e)
    try:
        policy = build_sandbox_policy()
        if policy is not None:
            funnel = getattr(runtime, "tool_funnel", None)
            if funnel is not None:
                funnel.sandbox_policy = policy
            wired["sandbox_policy"] = policy
    except Exception as e:
        logger.warning("kernel sandbox policy wiring failed: %s", e)
    try:
        result = build_skill_registry(runtime)
        if result is not None:
            wired["skill_registry"], wired["derived_rules"] = result
    except Exception as e:
        logger.warning("kernel skill registry wiring failed: %s", e)
    try:
        cp = attach_kernel_checkpointer(runtime)
        if cp is not None:
            wired["checkpointer"] = cp
    except Exception as e:
        logger.warning("kernel checkpoint wiring failed: %s", e)
    try:
        wired["orchestrator"] = build_kernel_orchestrator(runtime)
    except Exception as e:
        logger.warning("kernel orchestrator wiring failed: %s", e)
    logger.info("kernel surfaces wired: %s", sorted(k for k in wired))
    return wired
