import logging
import time

logger = logging.getLogger(__name__)

GOVERNANCE_STEWARD_ID = "governance-steward"

GOVERNANCE_STEWARD_PROMPT = (
    "You are the Multi-Repo Governance Steward for the five pilot repos "
    "(fusion-gateway, fusion-web-shell, fusion-agent-studio, "
    "fusion-artifacts-engine, fusion-cowork). You operate the G1-G5 gate "
    "model of the multi-repo automation system:\n"
    "- G1 proposal approval: issue enters the label state machine at "
    "state:proposal and needs Owner sign-off to reach state:approved "
    "(skipping G1 is a hard rule violation).\n"
    "- G2/G3 code review gates: PRs labeled risk:high never auto-merge; "
    "they require a manual review gate.\n"
    "- G4 in-acceptance verification: acceptance tasks move "
    "state:in-acceptance to accepted only after Owner verification "
    "(risk:low may be spot-checked).\n"
    "- G5 rollout sign-off: accepted work reaches rollout only via an "
    "explicit Owner go command (supervisor rollout / merge).\n\n"
    "You can create, list and decide approval tasks through the approval "
    "gateway (POST/GET /v1/agent-platform/approvals, approve/reject with "
    "decided_by=owner), register and list decision webhooks "
    "(/v1/agent-platform/webhooks), and query the issue label state "
    "machine across the pilot repos. Every decision flows back to the "
    "DAG through the signed decision webhook (sha256= signature).\n\n"
    "Invariants you must respect: decided_by defaults to owner; a "
    "requester can never decide their own request (server-side "
    "self-approval guard); the approval gateway base URL is the loopback "
    "daemon http://127.0.0.1:11442 and no secret ever lands on disk from "
    "a prompt."
)

GOVERNANCE_STEWARD_DESCRIPTION = (
    "Multi-repo governance steward: runs the G1-G5 gate model over the "
    "five pilot repos (G1 proposal approval, G2/G3 review gates, G4 "
    "in-acceptance verification, G5 rollout sign-off) via the approval "
    "gateway, decision webhooks and the issue label state machine. "
    "decided_by=owner, requester != reviewer (no self-approval), "
    "loopback :11442. 多仓库治理管家：按 G1-G5 门模型驱动五个试点仓库的"
    "审批网关、决策 webhook 与 issue 标签状态机。"
)

BUILTIN_AGENTS = [
    {
        "id": GOVERNANCE_STEWARD_ID,
        "name": "Multi-Repo Governance Steward",
        "model": "",
        "system_prompt": GOVERNANCE_STEWARD_PROMPT,
        "description": GOVERNANCE_STEWARD_DESCRIPTION,
        "tools": ["http_request"],
        "capabilities": ["governance", "approvals", "webhooks", "labels"],
        "tags": ["governance", "built-in"],
        "author": "system",
        "status": "published",
        "visibility": "public",
        "safety_level": "L3",
    },
]


def all_builtin_specs() -> list:
    # Governance steward stays a code constant per the #588 ruling; the
    # delivery seats come from the declarative config pack
    # (agent_runtime/seat_pack.py) — never from Python constants.
    from .seat_pack import load_seat_specs

    return BUILTIN_AGENTS + list(load_seat_specs().values())


def seed_builtin_agents(daemon) -> list[str]:
    from .agent_package import AgentPackage

    seeded = []
    for spec in all_builtin_specs():
        agent_id = spec["id"]
        if agent_id in daemon._agents:
            continue
        agent_dir = daemon._agent_dir(agent_id)
        pkg = AgentPackage(agent_dir)
        if not (agent_dir / ".fusion-agent" / "manifest.json").exists():
            _create_builtin_agent(spec, pkg)
        manifest = pkg.load_manifest()
        daemon._agents[agent_id] = manifest.to_dict()
        daemon._agents[agent_id]["id"] = agent_id
        daemon._agents[agent_id]["created_at"] = time.time()
        seeded.append(agent_id)
        logger.info("Seeded built-in agent: %s (%s)", agent_id, spec["name"])
    if seeded:
        daemon._persist_agents_index()
    return seeded


def seed_builtin_index(index: dict, agents_root=None) -> list[str]:
    """#588: daemon-agnostic seeding for the HTTP api_server's index loader.

    Same presence check + package bootstrap as seed_builtin_agents, but
    operates on a plain index dict and persists agents/index.json directly,
    so the steward is visible over REST even before any authenticated RPC
    forces the daemon-side index load.
    """
    import json as _json

    from .agent_package import AgentPackage
    from .paths import studio_home

    root = agents_root or studio_home() / "agents"
    idx_path = root / "index.json"
    seeded = []
    for spec in all_builtin_specs():
        agent_id = spec["id"]
        if agent_id in index:
            continue
        agent_dir = root / agent_id
        pkg = AgentPackage(agent_dir)
        if not (agent_dir / ".fusion-agent" / "manifest.json").exists():
            _create_builtin_agent(spec, pkg)
        manifest = pkg.load_manifest()
        index[agent_id] = manifest.to_dict()
        index[agent_id]["id"] = agent_id
        index[agent_id]["created_at"] = time.time()
        seeded.append(agent_id)
        logger.info("Seeded built-in agent (REST index): %s", agent_id)
    if seeded:
        try:
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            with open(idx_path, "w", encoding="utf-8") as f:
                _json.dump(index, f, indent=4, ensure_ascii=False)
        except OSError as e:
            logger.warning("Failed to persist seeded agents index: %s", e)
    return seeded


def _create_builtin_agent(spec: dict, pkg) -> None:
    from .agent_package import AgentManifest

    manifest = AgentManifest(
        name=spec["name"],
        model=spec.get("model", ""),
        system_prompt=spec["system_prompt"],
        temperature=0.3,
        max_tokens=4096,
        tools=spec.get("tools", []),
        capabilities=spec.get("capabilities", []),
        safety_level=spec.get("safety_level", "L1"),
        tags=spec.get("tags", []),
        author=spec.get("author", "system"),
        description=spec.get("description", ""),
        status=spec.get("status", "published"),
        visibility=spec.get("visibility", "public"),
    )
    # soul.md carries the prompt so resolve_soul_prompt() (the shared
    # execution path) picks it up; manifest keeps it too.
    pkg.init(manifest=manifest, soul=spec["system_prompt"])
