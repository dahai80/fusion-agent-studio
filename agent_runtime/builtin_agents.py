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
# #598 seats are appended after _delivery_seat_agents() is defined below.

DELIVERY_SEAT_IDS = (
    "delivery-ba",
    "delivery-architect",
    "delivery-env-mock",
    "delivery-security-reviewer",
    "delivery-privacy-reviewer",
)

# ── #598: product-delivery pipeline agent seats ─────────────────────
# architecture/product-delivery-pipeline-1105.md §8 + §10. Five published
# presets invocable via studio_run / graph execute from the cowork
# product_lifecycle template. Reviewer findings use one fixed,
# machine-consumable schema so the pipeline counts blocking verdicts
# deterministically.

REVIEWER_FINDINGS_SCHEMA = (
    'Output contract (machine-consumable, output ONLY this JSON):\n'
    '{"verdict": "pass" | "blocking",\n'
    ' "findings": [{"severity": "P0" | "P1" | "P2" | "P3",\n'
    '               "file": "<path>", "line": <int>, "desc": "<string>"}]}\n'
    "verdict=blocking iff any finding is P0 or P1. No prose outside the JSON.\n"
)

_SECURITY_CHECKLIST = (
    "- injection (SQL/command/template/prompt)\n"
    "- authentication and authorization gaps\n"
    "- hardcoded secrets or credentials\n"
    "- dependency vulnerabilities (unpinned/known-CVE)\n"
    "- high-risk APIs (shell eval, unsafe deserialization, network egress)\n"
)

_PRIVACY_CHECKLIST = (
    "- PII collection beyond stated purpose\n"
    "- PII storage without minimization or encryption\n"
    "- PII transit over unencrypted channels\n"
    "- PII leaking into logs, traces or error messages\n"
    "- third-party exfiltration (telemetry/SDKs sending user data)\n"
)


def _reviewer_prompt(role: str, checklist: str) -> str:
    return (
        f"You are the {role} seat in the product-delivery pipeline. You "
        "review a PR diff (read it with the git/file tools, or via the "
        "diff text given in the input) and produce a machine-consumable "
        "verdict the pipeline counts deterministically (blocking = 0 "
        "means the task is ready for gate C).\n\n"
        "Checklist (fixed, do not skip items):\n"
        f"{checklist}\n"
        f"{REVIEWER_FINDINGS_SCHEMA}\n"
        "Severity calibration is shared across all reviewer seats: P0 = "
        "exploitable/blocking now, P1 = must fix before gate C, P2 = "
        "should fix, P3 = note. Only report what the diff shows; never "
        "invent file paths or line numbers."
    )


def _delivery_seat_agents() -> list[dict]:
    ba_prompt = (
        "You are the BA (Business Analyst) seat of the product-delivery "
        "pipeline. Input: raw requirement text. Output: a PRD document "
        "with exactly these sections — background, goals, user stories "
        "(as-a/I-want/so-that), feature list, acceptance criteria "
        "(testable, per feature). Save the document via artifact_create "
        "(title = '<slug>-prd') and return the artifact id. No code, no "
        "architecture decisions — that is the Architect seat."
    )
    arch_prompt = (
        "You are the Architect seat of the product-delivery pipeline. "
        "Input: an approved PRD. Output three artifacts: (1) architecture "
        "design doc (components, data flow, technology choices with "
        "rationale), (2) project plan (milestones, dependencies), (3) "
        "task breakdown list — every task carries its own scope and "
        "testable acceptance criteria. Save each via artifact_create "
        "(titles '<slug>-arch', '<slug>-plan', '<slug>-tasks') and "
        "return the artifact ids."
    )
    mock_prompt = (
        "You are the Env/Mock (environment) seat of the "
        "product-delivery pipeline. Input: PRD and/or architecture "
        "artifacts. Output: a mock pack for staging human-acceptance — "
        "stub services for external dependencies, fixture data, a "
        "compose overlay for local staging, and seed scripts. Save the "
        "pack via artifact_create (title '<slug>-mock-pack') and return "
        "the artifact id. Mocks must be deterministic and documented; "
        "never point staging at real third-party endpoints."
    )
    seats = [
        {
            "id": "delivery-ba",
            "name": "Delivery BA Agent",
            "system_prompt": ba_prompt,
            "description": (
                "Product-delivery seat 1: requirement text to PRD "
                "document (background/goals/user stories/feature list/"
                "acceptance criteria), versioned as an artifact. "
                "需求分析师席：需求原文到 PRD 文档产物。"
            ),
            "tools": ["artifact_create", "file_write", "json_parse"],
            "capabilities": ["delivery", "ba", "prd", "artifacts"],
            "tags": ["delivery", "built-in"],
        },
        {
            "id": "delivery-architect",
            "name": "Delivery Architect Agent",
            "system_prompt": arch_prompt,
            "description": (
                "Product-delivery seat 2: approved PRD to architecture "
                "design doc + project plan + task breakdown (per-task "
                "scope + acceptance criteria), versioned as artifacts. "
                "架构师席：PRD 到架构/计划/任务分解产物。"
            ),
            "tools": ["artifact_create", "file_read", "file_write"],
            "capabilities": ["delivery", "architect", "artifacts"],
            "tags": ["delivery", "built-in"],
        },
        {
            "id": "delivery-env-mock",
            "name": "Delivery Env/Mock Agent",
            "system_prompt": mock_prompt,
            "description": (
                "Product-delivery seat 6: PRD/architecture to mock pack "
                "(stub services, fixture data, compose overlay, seed) "
                "for staging human-acceptance, versioned as an "
                "artifact. 环境师席：mock pack 生成产物。"
            ),
            "tools": ["artifact_create", "file_write", "terminal"],
            "capabilities": ["delivery", "mock", "staging", "artifacts"],
            "tags": ["delivery", "built-in"],
        },
        {
            "id": "delivery-security-reviewer",
            "name": "Delivery Security Reviewer",
            "system_prompt": _reviewer_prompt(
                "Security Reviewer", _SECURITY_CHECKLIST
            ),
            "description": (
                "Product-delivery seat 7a: PR diff review with the fixed "
                "findings schema {severity, file, line, desc} and "
                "verdict pass|blocking; checklist = injection/auth/"
                "secrets/dependency vulns/high-risk APIs. 安全评审席。"
            ),
            "tools": ["git", "file_read", "http_request"],
            "capabilities": ["delivery", "review", "security"],
            "tags": ["delivery", "reviewer", "built-in"],
        },
        {
            "id": "delivery-privacy-reviewer",
            "name": "Delivery Privacy Reviewer",
            "system_prompt": _reviewer_prompt(
                "Privacy Reviewer", _PRIVACY_CHECKLIST
            ),
            "description": (
                "Product-delivery seat 7b: PR diff review with the fixed "
                "findings schema {severity, file, line, desc} and "
                "verdict pass|blocking; checklist = PII collection/"
                "storage/transit/log leakage/third-party exfiltration. "
                "隐私评审席。"
            ),
            "tools": ["git", "file_read", "http_request"],
            "capabilities": ["delivery", "review", "privacy"],
            "tags": ["delivery", "reviewer", "built-in"],
        },
    ]
    return seats


BUILTIN_AGENTS.extend(_delivery_seat_agents())


def seed_builtin_agents(daemon) -> list[str]:
    from .agent_package import AgentPackage

    seeded = []
    for spec in BUILTIN_AGENTS:
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
    for spec in BUILTIN_AGENTS:
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
