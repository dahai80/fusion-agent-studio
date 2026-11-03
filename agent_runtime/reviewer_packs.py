"""#549: first-party reviewer packs — role-based review templates.

Four installable reviewer packs (architecture-contract / security / domain /
red-team), each a standard agent package (bundle manifest + agent markdown +
skills) plus a shared findings contract:

    {severity: P0-P3, location, rationale, recommendation, pack}

Free-text-only verdicts are rejected at parse time. Blind-review mode strips
author reasoning/rationale from pack input (configurable). Fan-out runs N
packs concurrently on one artifact and merges findings with per-pack
attribution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

SEVERITIES = ("P0", "P1", "P2", "P3")
SEVERITY_RE = re.compile(r"^P[0-3]$")
REVIEWER_PACK_KIND = "reviewer_pack"

REVIEW_FINDINGS_SCHEMA = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "location", "rationale", "recommendation"],
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "location": {"type": "string"},
                    "rationale": {"type": "string"},
                    "recommendation": {"type": "string"},
                },
            },
        }
    },
}


@dataclass
class ReviewFinding:
    severity: str
    location: str
    rationale: str
    recommendation: str
    pack: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "location": self.location,
            "rationale": self.rationale,
            "recommendation": self.recommendation,
            "pack": self.pack,
        }

    @classmethod
    def from_dict(cls, data: dict, pack: str = "") -> "ReviewFinding":
        return cls(
            severity=str(data.get("severity", "")),
            location=str(data.get("location", "")),
            rationale=str(data.get("rationale", "")),
            recommendation=str(data.get("recommendation", "")),
            pack=str(data.get("pack", "") or pack),
        )


class FindingValidationError(ValueError):
    pass


def validate_finding(data: dict) -> ReviewFinding:
    if not isinstance(data, dict):
        raise FindingValidationError("finding must be an object")
    severity = str(data.get("severity", "")).strip().upper()
    if not SEVERITY_RE.match(severity):
        raise FindingValidationError(
            f"finding severity must be one of {SEVERITIES}, got: {data.get('severity')!r}"
        )
    missing = [
        k
        for k in ("location", "rationale", "recommendation")
        if not str(data.get(k, "")).strip()
    ]
    if missing:
        raise FindingValidationError(
            f"finding missing structured field(s): {', '.join(missing)} — "
            "free-text-only verdicts are rejected (#549)"
        )
    return ReviewFinding(
        severity=severity,
        location=str(data.get("location", "")).strip(),
        rationale=str(data.get("rationale", "")).strip(),
        recommendation=str(data.get("recommendation", "")).strip(),
        pack=str(data.get("pack", "") or ""),
    )


def parse_findings(raw: str, pack: str = "") -> list[ReviewFinding]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except ValueError as e:
        raise FindingValidationError(
            "response is not findings JSON — free-text-only verdicts are rejected (#549)"
        ) from e
    items = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise FindingValidationError("findings payload must contain a 'findings' array")
    return [validate_finding({**item, "pack": pack}) for item in items]


@dataclass
class ReviewerPack:
    key: str
    name: str
    description: str
    system_prompt: str
    checklist: str
    default_params: dict = field(default_factory=dict)
    blind_review: bool = True

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "kind": REVIEWER_PACK_KIND,
            "blind_review": self.blind_review,
            "default_params": self.default_params,
            "bundle_name": f"reviewer-pack-{self.key}",
        }


_ARCHITECTURE_PROMPT = """# Architecture-Contract Reviewer

You review an artifact strictly for architecture-contract violations:

- interface consistency across modules (signatures, units, error contracts)
- data-format drift between producers and consumers
- cross-module boundary violations (reaching past a published seam)
- breaking-change risk for existing callers

You are a blind reviewer: judge the artifact and the original request only.
Do not assume the author's intent is correct because they explained it.

Output STRICT JSON, no prose:
{"findings": [{"severity": "P0|P1|P2|P3", "location": "<section/file/line>",
"rationale": "...", "recommendation": "..."}]}
No findings: {"findings": []}
"""

_SECURITY_PROMPT = """# Security Reviewer

You run a STRIDE pass over every attack surface in the artifact:

- spoofing / tampering / repudiation / information disclosure /
  denial of service / elevation of privilege
- authn and authz gaps (missing checks, wrong scope, confused deputy)
- injection, SSRF, path traversal patterns
- secret leakage (credentials in code, logs, config, error messages)

You are a blind reviewer: judge the artifact and the original request only.

Output STRICT JSON, no prose:
{"findings": [{"severity": "P0|P1|P2|P3", "location": "<section/file/line>",
"rationale": "...", "recommendation": "..."}]}
No findings: {"findings": []}
"""

_DOMAIN_PROMPT = """# Domain Reviewer

You review the artifact for domain correctness: facts, terminology, units,
regulatory constraints, and workflows that a practitioner of the domain
would flag. Use the domain knowledge notes attached to this pack when they
cover the artifact's domain; state explicitly in a finding's rationale when
a claim is outside the notes and you are flagging from general principles.

You are a blind reviewer: judge the artifact and the original request only.

Output STRICT JSON, no prose:
{"findings": [{"severity": "P0|P1|P2|P3", "location": "<section/file/line>",
"rationale": "...", "recommendation": "..."}]}
No findings: {"findings": []}
"""

_REDTEAM_PROMPT = """# Red-Team Reviewer — Mandatory Dissent

Your job is to argue why this artifact WILL FAIL. You must always produce
at least one finding: your strongest objective. Do not soften findings to
be polite; do not output an empty list unless the artifact is literally an
empty document.

Attack: hidden assumptions, unhandled failure modes, scaling cliffs,
operational gaps, and every claim that lacks supporting evidence.

You are a blind reviewer: judge the artifact and the original request only.

Output STRICT JSON, no prose:
{"findings": [{"severity": "P0|P1|P2|P3", "location": "<section/file/line>",
"rationale": "...", "recommendation": "..."}]}
"""

_ARCH_CHECKLIST = """# Architecture-Contract Checklist
- For each module boundary crossed: is the seam published and stable?
- Do two modules define different shapes for the same data?
- Any change that silently breaks an existing caller?
- Are error contracts (codes, retryability) consistent across surfaces?
"""

_SEC_CHECKLIST = """# Security Checklist (STRIDE)
- Enumerate entry points; for each, which STRIDE strides apply?
- Every privileged action: authn + authz + audit trail?
- Any unbounded input reaching a parser, filesystem path, or URL fetch?
- Any secret in plaintext code/config/logs?
"""

_DOMAIN_CHECKLIST = """# Domain Review Checklist
- Claims verified against domain knowledge notes (mark out-of-notes claims).
- Units, currencies, time zones, and regulatory limits correct?
- Terminology matches practitioner usage?
"""

_RED_CHECKLIST = """# Red-Team Mandatory Dissent
- Strongest-objective finding REQUIRED (>= 1 finding, every run).
- Rank findings by objective evidence of failure, not by rhetoric.
"""


REVIEWER_PACKS: dict[str, ReviewerPack] = {
    p.key: p
    for p in [
        ReviewerPack(
            key="architecture-contract",
            name="Architecture-Contract Reviewer",
            description=(
                "Interface consistency, data-format drift, cross-module "
                "boundary violations, breaking-change risk."
            ),
            system_prompt=_ARCHITECTURE_PROMPT,
            checklist=_ARCH_CHECKLIST,
        ),
        ReviewerPack(
            key="security",
            name="Security Reviewer",
            description=(
                "STRIDE pass over attack surfaces, authn/authz gaps, "
                "injection/SSRF/path-traversal, secret leakage."
            ),
            system_prompt=_SECURITY_PROMPT,
            checklist=_SEC_CHECKLIST,
        ),
        ReviewerPack(
            key="domain",
            name="Domain Reviewer",
            description=(
                "Domain-correctness pass with per-domain knowledge hooks "
                "(pluggable knowledge notes per customer domain)."
            ),
            system_prompt=_DOMAIN_PROMPT,
            checklist=_DOMAIN_CHECKLIST,
            default_params={"domain_notes": ""},
        ),
        ReviewerPack(
            key="red-team",
            name="Red-Team Reviewer",
            description=(
                "Mandatory dissent — argues why the artifact will fail; "
                "strongest-objective finding required."
            ),
            system_prompt=_REDTEAM_PROMPT,
            checklist=_RED_CHECKLIST,
        ),
    ]
}


def get_pack(key: str) -> ReviewerPack | None:
    return REVIEWER_PACKS.get(key)


def list_packs() -> list[dict]:
    return [p.to_dict() for p in REVIEWER_PACKS.values()]


def _user_message(
    pack: ReviewerPack,
    artifact: str,
    original_request: str,
    author_notes: str,
    blind: bool,
    params: dict,
) -> str:
    parts = [f"# Original request\n{original_request}", f"# Artifact under review\n{artifact}"]
    if not blind and author_notes.strip():
        parts.append(f"# Author notes (untrusted, non-blind mode)\n{author_notes}")
    domain_notes = str(params.get("domain_notes", "") or "")
    if domain_notes.strip():
        parts.append(f"# Domain knowledge notes\n{domain_notes}")
    parts.append(f"# Review checklist\n{pack.checklist}")
    return "\n\n".join(parts)


def merge_results(results: list[dict]) -> dict:
    findings: list[dict] = []
    per_pack: dict[str, dict] = {}
    for r in results:
        pack = r["pack"]
        entry = {
            "status": r["status"],
            "finding_count": len(r.get("findings", [])),
        }
        if r["status"] == "ok":
            findings.extend(r.get("findings", []))
        else:
            entry["error"] = r.get("error", "")
        per_pack[pack] = entry
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order.get(f.get("severity", ""), 99), f.get("pack", ""), f.get("location", "")))
    return {
        "status": "ok",
        "findings": findings,
        "per_pack": per_pack,
        "total": len(findings),
    }


class ReviewerPackRunner:
    """Runs reviewer packs concurrently. llm_fn is injected (no direct MLX
    import here — callers wire their LLM gateway / FusionMLXClient)."""

    def __init__(self, llm_fn):
        self._llm_fn = llm_fn

    async def _run_one(
        self,
        pack: ReviewerPack,
        artifact: str,
        original_request: str,
        author_notes: str,
        blind: bool,
        params: dict,
    ) -> dict:
        merged = {**pack.default_params, **params}
        use_blind = blind if blind is not None else pack.blind_review
        messages = [
            {"role": "system", "content": pack.system_prompt},
            {
                "role": "user",
                "content": _user_message(
                    pack, artifact, original_request, author_notes, use_blind, merged
                ),
            },
        ]
        try:
            raw = await self._llm_fn(messages, model=merged.get("model", ""))
            findings = parse_findings(raw, pack=pack.key)
            logger.info(
                "reviewer pack %s: %d findings (blind=%s)", pack.key, len(findings), use_blind
            )
            return {
                "pack": pack.key,
                "status": "ok",
                "findings": [f.to_dict() for f in findings],
                "blind": use_blind,
            }
        except FindingValidationError as e:
            logger.error("reviewer pack %s produced invalid findings: %s", pack.key, e)
            return {"pack": pack.key, "status": "error", "error": f"invalid findings: {e}"}
        except Exception as e:
            logger.exception("reviewer pack %s failed", pack.key)
            return {"pack": pack.key, "status": "error", "error": str(e)}

    async def run(
        self,
        pack_keys: list[str],
        artifact: str,
        original_request: str,
        author_notes: str = "",
        blind: bool = True,
        params_by_pack: dict[str, dict] | None = None,
    ) -> dict:
        params_by_pack = params_by_pack or {}
        packs = []
        for key in pack_keys:
            p = REVIEWER_PACKS.get(key)
            if p is None:
                raise ValueError(f"unknown reviewer pack: {key}")
            packs.append(p)
        if not packs:
            raise ValueError("no reviewer packs selected")
        results = await asyncio.gather(
            *[
                self._run_one(
                    p, artifact, original_request, author_notes, blind, params_by_pack.get(p.key, {})
                )
                for p in packs
            ]
        )
        merged = merge_results(list(results))
        logger.info(
            "reviewer fan-out: packs=%s total_findings=%d",
            ",".join(pack_keys),
            merged["total"],
        )
        return merged


# ── first-party bundle seeding (installable via bundle.install) ──

def write_bundle_dirs(base_dir: Path | str) -> list[Path]:
    """Materialize the 4 first-party packs as standard bundles
    (manifest.json + agent.md + skills/<key>-checklist/SKILL.md)."""
    base = Path(base_dir)
    written = []
    for pack in REVIEWER_PACKS.values():
        d = base / f"reviewer-pack-{pack.key}"
        (d / "skills" / f"{pack.key}-checklist").mkdir(parents=True, exist_ok=True)
        (d / "agent.md").write_text(pack.system_prompt, encoding="utf-8")
        (d / "skills" / f"{pack.key}-checklist" / "SKILL.md").write_text(
            f"---\nname: {pack.key}-checklist\ndescription: {pack.name} checklist\n---\n\n"
            + pack.checklist,
            encoding="utf-8",
        )
        manifest = {
            "name": f"reviewer-pack-{pack.key}",
            "version": "1.0.0",
            "description": pack.description,
            "kind": REVIEWER_PACK_KIND,
            "agent": {
                "name": f"reviewer-{pack.key}",
                "description": pack.description,
                "system_prompt": pack.system_prompt,
            },
            "skills": [{"name": f"{pack.key}-checklist"}],
        }
        (d / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(d)
        logger.info("reviewer bundle written: %s", d)
    return written
