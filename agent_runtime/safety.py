"""Safety gateway - Human-in-the-Loop safety system delegating judgment to fusion-guard.

fusion-guard (per-host zero-trust authorization daemon) is the single security-rule SSOT.
This gateway is a thin client: evaluate_action/check delegate to GuardSafetyBackend, which
talks to the guard daemon over UDS. When guard is unreachable (CI has no fusion_guard; local
dev has no socket) the backend degrades to a fail-closed floor that BLOCKs destructive ops
(rm -rf /, DROP TABLE) and REDACTs secrets — never ALLOWs a destructive command.

L1/L2/L3 levels + approval store remain for the approval flow. detect_prompt_injection,
generate_diff_preview and classify_action stay local (guard out-of-scope).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class SafetyLevel(str, Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class SafetyAction(str, Enum):
    ALLOW = "allow"
    PREVIEW = "preview"
    BLOCK = "block"
    REDACT = "redact"


CAT_CODE_ANALYSIS = "code_analysis"
CAT_DOC_RETRIEVAL = "doc_retrieval"
CAT_KNOWLEDGE_SEARCH = "knowledge_search"
CAT_FILE_READ = "file_read"
CAT_FILE_WRITE = "file_write"
CAT_CODE_EDIT = "code_edit"
CAT_SHELL_EXEC = "shell_exec"
CAT_GIT_PUSH = "git_push"
CAT_DATABASE_WRITE = "database_write"
CAT_NETWORK_ACCESS = "network_access"
CAT_TOOL_CALL = "tool_call"
CAT_LLM_CALL = "llm_call"


@dataclass
class DiffPreviewRequest:
    action_id: str
    category: str
    original: str
    proposed: str
    diff: str
    requires_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "category": self.category,
            "original": self.original,
            "proposed": self.proposed,
            "diff": self.diff,
            "requires_approval": self.requires_approval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffPreviewRequest:
        return cls(
            action_id=data["action_id"],
            category=data["category"],
            original=data["original"],
            proposed=data["proposed"],
            diff=data["diff"],
            requires_approval=data.get("requires_approval", True),
        )


@dataclass
class SafetyPolicy:
    category: str
    default_level: SafetyLevel
    requires_diff: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "default_level": self.default_level.value,
            "requires_diff": self.requires_diff,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SafetyPolicy:
        return cls(
            category=data["category"],
            default_level=SafetyLevel(data["default_level"]),
            requires_diff=data.get("requires_diff", False),
            description=data.get("description", ""),
        )


@dataclass
class SafetyVerdict:
    action: SafetyAction
    reason: str = ""
    redacted_content: str = ""
    requires_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    diff_preview: DiffPreviewRequest | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "action": self.action.value,
            "reason": self.reason,
            "redacted_content": self.redacted_content,
            "requires_approval": self.requires_approval,
            "metadata": self.metadata,
        }
        if self.diff_preview is not None:
            d["diff_preview"] = self.diff_preview.to_dict()
        else:
            d["diff_preview"] = None
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SafetyVerdict:
        dp = data.get("diff_preview")
        if dp is not None and isinstance(dp, dict):
            dp = DiffPreviewRequest.from_dict(dp)
        return cls(
            action=SafetyAction(data["action"]),
            reason=data.get("reason", ""),
            redacted_content=data.get("redacted_content", ""),
            requires_approval=data.get("requires_approval", False),
            metadata=data.get("metadata", {}),
            diff_preview=dp,
        )


@dataclass
class SafetyRule:
    name: str
    pattern: str
    action: SafetyAction
    reason: str = ""
    scope: str = "all"
    min_level: SafetyLevel = SafetyLevel.L1


# #258: local rule engine (_DANGEROUS_PATTERNS) + default policies (_DEFAULT_POLICIES)
# deleted — judgment is delegated to GuardSafetyBackend (fusion-guard SSOT). SafetyRule is
# retained: set_network_policy + custom_rules still build it as advisory state, but the
# regex loop that consumed it is gone (guard-down floor catches destructive ops).

ApproverCallback = Callable[[str, str], Coroutine[Any, Any, bool]]


class SafetyGateway:
    """3-level Human-in-the-Loop safety gateway.

    Usage:
        gateway = SafetyGateway(level=SafetyLevel.L2)
        verdict = gateway.check("DELETE FROM users WHERE id=1")
        if verdict.requires_approval:
            approved = await gateway.request_approval("sql-delete", "DELETE FROM users...")

        # Category-based evaluation (new):
        verdict = gateway.evaluate_action("file_write", content, context)
        if verdict.diff_preview:
            approved = gateway.approve_action(verdict.diff_preview.action_id)
    """

    def __init__(
        self,
        level: SafetyLevel = SafetyLevel.L1,
        custom_rules: list[SafetyRule] | None = None,
        approver: ApproverCallback | None = None,
        policies: list[SafetyPolicy] | None = None,
        enable_injection: bool = False,
        backend: str | None = None,
        guard_client=None,
    ):
        self.level = level
        # custom_rules is advisory-only (#258): set_network_policy appends
        # SafetyRule here, but judgment no longer iterates it (guard-down floor
        # catches destructive ops). Retained for get_network_policy state.
        self.custom_rules = custom_rules or []
        self.approver = approver
        self.enable_injection = enable_injection
        # _policies is advisory-only (#258): add_policy/get_policy/policies keep
        # a thin local-override store (safety.add_policy RPC stays public), but
        # judgment delegates to guard and ignores it. Starts empty.
        self._policies = policies if policies is not None else []
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._pending_action_approvals: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

        # #258: unified guard backend. local/guard/auto all route through
        # GuardSafetyBackend — guard-up -> guard verdict; guard-down (CI: no
        # fusion_guard import; local: no socket) -> fail-closed floor (BLOCKs
        # rm-rf / never ALLOW). The local regex engine is gone.
        self.backend = backend or os.environ.get("FUSION_SAFETY_BACKEND", "local")
        self._guard_backend = self._init_guard_backend(guard_client)
        if self._guard_backend is not None:
            logger.info(
                "SafetyGateway guard backend active (available=%s)",
                self._guard_backend.is_available(),
            )
        else:
            logger.error(
                "SafetyGateway guard backend init failed; fail-closed floor will apply"
            )

    def _init_guard_backend(self, guard_client):
        # lazy import avoids circular (guard_client imports safety symbols).
        try:
            from .guard_client import GuardSafetyBackend
            return GuardSafetyBackend(client=guard_client) if guard_client else GuardSafetyBackend()
        except Exception as e:
            logger.warning("SafetyBackend guard init failed (%s)", e)
            return None

    @property
    def policies(self) -> list[SafetyPolicy]:
        return self._policies

    def get_policy(self, category: str) -> SafetyPolicy | None:
        for p in self._policies:
            if p.category == category:
                return p
        return None

    def add_policy(self, policy: SafetyPolicy) -> None:
        with self._lock:
            existing = self.get_policy(policy.category)
            if existing is not None:
                self._policies.remove(existing)
            self._policies.append(policy)
            logger.info(
                "Added safety policy: %s -> %s",
                policy.category,
                policy.default_level.value,
            )

    def evaluate_action(
        self,
        category: str,
        content: str = "",
        context: str = "",
    ) -> SafetyVerdict:
        # #258: unified guard delegation. Guard is the judgment SSOT; local
        # policy lookup is gone. If the backend failed to init, fail closed.
        if self._guard_backend is None:
            logger.error(
                "evaluate_action: no guard backend, fail-closed BLOCK for '%s'", category
            )
            return SafetyVerdict(
                action=SafetyAction.BLOCK,
                reason="Safety backend unavailable",
                requires_approval=True,
                metadata={"category": category, "backend_missing": True},
            )
        return self._evaluate_action_guard(category, content, context)

    def _evaluate_action_guard(
        self, category: str, content: str, context: str
    ) -> SafetyVerdict:
        # #252 guard thin-client: delegate judgment to fusion-guard. Guard is the
        # security-rule SSOT; local policies/_DANGEROUS_PATTERNS are advisory only.
        # injection detection stays local (guard out-of-scope). diff preview for
        # file_write/code_edit generated locally (guard doesn't diff).
        if self.enable_injection and content:
            inj = detect_prompt_injection(content)
            if inj["detected"]:
                logger.warning(
                    "Guard mode: injection detected (%d patterns) before delegate",
                    inj["match_count"],
                )
                if self._level_ord(self.level) >= self._level_ord(SafetyLevel.L2):
                    return SafetyVerdict(
                        action=SafetyAction.BLOCK,
                        reason=f"Prompt injection detected ({inj['match_count']} patterns)",
                        requires_approval=True,
                        metadata={"injection": True, "matches": inj["matches"]},
                    )
                return SafetyVerdict(
                    action=SafetyAction.PREVIEW,
                    reason=f"Prompt injection detected ({inj['match_count']} patterns)",
                    requires_approval=True,
                    metadata={"injection": True, "matches": inj["matches"]},
                )

        verdict = self._guard_backend.evaluate(category, content, context)
        verdict.metadata.setdefault("category", category)

        # L2 preview with a file target: generate local diff preview (guard doesn't diff).
        if (
            verdict.action == SafetyAction.PREVIEW
            and category in (CAT_FILE_WRITE, CAT_CODE_EDIT)
            and content
        ):
            action_id = verdict.metadata.get("action_id") or str(uuid.uuid4())
            verdict.metadata["action_id"] = action_id
            verdict.diff_preview = self.generate_diff_preview(
                content, context or "", category, action_id
            )

        # L3 block requires approval: register pending store so the runtime
        # approval future resolves via approve_action/reject_action below.
        if verdict.action == SafetyAction.BLOCK and verdict.requires_approval:
            action_id = verdict.metadata.get("action_id")
            if action_id:
                with self._lock:
                    self._pending_action_approvals[action_id] = {
                        "category": category,
                        "content": content,
                        "level": verdict.metadata.get("level", "L3"),
                        "status": "pending",
                    }

        logger.info(
            "Guard verdict for category '%s': action=%s risk=%s action_id=%s",
            category,
            verdict.action.value,
            verdict.metadata.get("risk_level"),
            verdict.metadata.get("action_id"),
        )
        return verdict

    def generate_diff_preview(
        self,
        original: str,
        proposed: str,
        category: str = "",
        action_id: str | None = None,
    ) -> DiffPreviewRequest:
        if action_id is None:
            action_id = str(uuid.uuid4())

        original_lines = original.splitlines(keepends=True)
        proposed_lines = proposed.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                original_lines,
                proposed_lines,
                fromfile="original",
                tofile="proposed",
            )
        )
        diff_text = "".join(diff_lines)

        request = DiffPreviewRequest(
            action_id=action_id,
            category=category,
            original=original,
            proposed=proposed,
            diff=diff_text,
            requires_approval=True,
        )

        with self._lock:
            self._pending_action_approvals[action_id] = {
                "category": category,
                "original": original,
                "proposed": proposed,
                "diff": diff_text,
                "level": "L2",
                "status": "pending",
                "request": request,
            }

        logger.debug(
            "Generated diff preview for action_id=%s, category=%s", action_id, category
        )
        return request

    def approve_action(self, action_id: str) -> bool:
        # #252 guard mode: delegate confirm to guard before resolving local future.
        if self._guard_backend is not None:
            if not self._guard_backend.confirm(action_id, True):
                logger.warning(
                    "approve_action: guard.confirm failed for action_id=%s", action_id
                )
                return False
        with self._lock:
            pending = self._pending_action_approvals.get(action_id)
            if pending is None:
                logger.warning(
                    "approve_action: no pending action with id=%s", action_id
                )
                return False
            if pending["status"] != "pending":
                logger.warning(
                    "approve_action: action %s already resolved as %s",
                    action_id,
                    pending["status"],
                )
                return False
            pending["status"] = "approved"

        if action_id in self._pending_approvals:
            future = self._pending_approvals[action_id]
            if not future.done():
                future.set_result(True)

        logger.info("Action approved: action_id=%s", action_id)
        return True

    def reject_action(self, action_id: str) -> bool:
        # #252 guard mode: delegate confirm(rejected) to guard.
        if self._guard_backend is not None:
            if not self._guard_backend.confirm(action_id, False):
                logger.warning(
                    "reject_action: guard.confirm failed for action_id=%s", action_id
                )
                return False
        with self._lock:
            pending = self._pending_action_approvals.get(action_id)
            if pending is None:
                logger.warning("reject_action: no pending action with id=%s", action_id)
                return False
            if pending["status"] != "pending":
                logger.warning(
                    "reject_action: action %s already resolved as %s",
                    action_id,
                    pending["status"],
                )
                return False
            pending["status"] = "rejected"

        if action_id in self._pending_approvals:
            future = self._pending_approvals[action_id]
            if not future.done():
                future.set_result(False)

        logger.info("Action rejected: action_id=%s", action_id)
        return True

    def get_pending_actions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"action_id": aid, **{k: v for k, v in info.items() if k != "request"}}
                for aid, info in self._pending_action_approvals.items()
                if info["status"] == "pending"
            ]

    def check(self, content: str, context: str = "") -> SafetyVerdict:
        if self.enable_injection:
            inj = detect_prompt_injection(content)
            if inj["detected"]:
                if self._level_ord(self.level) >= self._level_ord(SafetyLevel.L2):
                    logger.warning(
                        "Injection blocked at L%d: %d patterns",
                        self._level_ord(self.level),
                        inj["match_count"],
                    )
                    return SafetyVerdict(
                        action=SafetyAction.BLOCK,
                        reason=f"Prompt injection detected ({inj['match_count']} patterns)",
                        requires_approval=True,
                        metadata={"injection": True, "matches": inj["matches"]},
                    )
                logger.info(
                    "Injection flagged for approval at L1: %d patterns",
                    inj["match_count"],
                )
                return SafetyVerdict(
                    action=SafetyAction.PREVIEW,
                    reason=f"Prompt injection detected ({inj['match_count']} patterns)",
                    requires_approval=True,
                    metadata={"injection": True, "matches": inj["matches"]},
                )

        # #258: delegate content judgment to the guard backend. Guard-up ->
        # guard verdict; guard-down -> fail-closed floor (BLOCKs rm-rf /,
        # DROP TABLE, REDACTs secrets, never ALLOWs destructive ops). If the
        # backend failed to init, fail closed.
        if self._guard_backend is None:
            logger.error("check: no guard backend, fail-closed BLOCK")
            return SafetyVerdict(
                action=SafetyAction.BLOCK,
                reason="Safety backend unavailable",
                requires_approval=True,
                metadata={"backend_missing": True},
            )
        return self._guard_backend.evaluate("", content, context)

    async def request_approval(
        self,
        action_id: str,
        description: str,
        timeout: float = 60.0,
    ) -> bool:
        if self.level == SafetyLevel.L1:
            logger.debug("L1 auto-approving: %s", action_id)
            return True

        if self.approver is None:
            logger.warning(
                "No approver set for L%d, denying: %s",
                self._level_ord(self.level),
                action_id,
            )
            return False

        try:
            approved = await asyncio.wait_for(
                self.approver(action_id, description),
                timeout=timeout,
            )
            logger.info(
                "Approval %s for %s", "granted" if approved else "denied", action_id
            )
            return approved
        except asyncio.TimeoutError:
            logger.warning("Approval timed out for %s", action_id)
            return False

    def set_level(self, level: SafetyLevel) -> None:
        old = self.level
        self.level = level
        logger.info("Safety level changed: %s -> %s", old, level)

    def classify_action(self, action: str, context: str = "") -> dict[str, Any]:
        action_lower = action.lower()
        high_risk_keywords = [
            "delete",
            "drop",
            "rm ",
            "format",
            "shutdown",
            "reboot",
            "fork bomb",
            "eval(",
            "exec(",
        ]
        medium_risk_keywords = [
            "write",
            "edit",
            "modify",
            "update",
            "push",
            "deploy",
            "install",
            "pip install",
            "npm install",
        ]

        risk_score = 0.0
        risk_factors = []

        for kw in high_risk_keywords:
            if kw in action_lower:
                risk_score += 0.3
                risk_factors.append(f"high_risk_keyword:{kw}")

        for kw in medium_risk_keywords:
            if kw in action_lower:
                risk_score += 0.15
                risk_factors.append(f"medium_risk_keyword:{kw}")

        if context:
            context_lower = context.lower()
            for kw in high_risk_keywords:
                if kw in context_lower:
                    risk_score += 0.1
                    risk_factors.append(f"context_risk:{kw}")

        risk_score = min(risk_score, 1.0)

        if risk_score >= 0.5:
            classification = "human_approve"
        elif risk_score >= 0.2:
            classification = "preview"
        else:
            classification = "auto_approve"

        logger.info(
            "Action classified: score=%.2f class=%s factors=%s",
            risk_score,
            classification,
            risk_factors,
        )
        return {
            "classification": classification,
            "risk_score": risk_score,
            "risk_factors": risk_factors,
            "action": action[:200],
        }

    def set_auto_mode(self, enabled: bool, threshold: float = 0.2) -> None:
        if enabled:
            self.level = SafetyLevel.L1
            logger.info("Auto-mode enabled, threshold=%.2f, level=L1", threshold)
        else:
            self.level = SafetyLevel.L2
            logger.info("Auto-mode disabled, level=L2")

    def set_network_policy(self, policy: dict[str, Any]) -> None:
        allowlist = policy.get("allowlist", [])
        denylist = policy.get("denylist", [])
        net_policy = SafetyPolicy(
            category=CAT_NETWORK_ACCESS,
            default_level=SafetyLevel.L2,
            requires_diff=False,
            description=f"Network: allow={allowlist}, deny={denylist}",
        )
        if denylist:
            for domain in denylist:
                self.custom_rules.append(
                    SafetyRule(
                        name=f"network_deny_{domain}",
                        pattern=re.escape(domain),
                        action=SafetyAction.BLOCK,
                        reason=f"Network access to {domain} blocked by policy",
                        scope="network",
                    )
                )
        self.add_policy(net_policy)
        self._network_policy = policy
        logger.info(
            "Network policy set: allowlist=%d denylist=%d",
            len(allowlist),
            len(denylist),
        )

    def get_network_policy(self) -> dict[str, Any]:
        return getattr(self, "_network_policy", {"allowlist": [], "denylist": []})

    @staticmethod
    def _level_ord(level: SafetyLevel) -> int:
        return {SafetyLevel.L1: 1, SafetyLevel.L2: 2, SafetyLevel.L3: 3}[level]


_INJECTION_PATTERNS = [
    re.compile(r"(?i)ignore\s+(previous|above|all)\s+instructions?", re.IGNORECASE),
    re.compile(
        r"(?i)forget\s+(your|all|previous)\s+(instructions|rules|prompt)", re.IGNORECASE
    ),
    re.compile(r"(?i)you\s+are\s+now\s+a", re.IGNORECASE),
    re.compile(r"(?i)system\s*:\s*", re.IGNORECASE),
    re.compile(r"(?i)new\s+instructions?\s*:", re.IGNORECASE),
    re.compile(
        r"(?i)disregard\s+(your|all|previous)\s+(instructions|rules)", re.IGNORECASE
    ),
    re.compile(r"(?i)act\s+as\s+if\s+you\s+(are|were)", re.IGNORECASE),
    re.compile(r"(?i)pretend\s+(you\s+are|to\s+be)", re.IGNORECASE),
    re.compile(r"(?i)jailbreak", re.IGNORECASE),
    re.compile(r"(?i)DAN\s+mode", re.IGNORECASE),
    re.compile(
        r"(?i)override\s+(safety|security|content)\s+(policy|filter|guard)",
        re.IGNORECASE,
    ),
    re.compile(r"(?i)reveal\s+.*\s?(prompt|instructions)", re.IGNORECASE),
    re.compile(r"(?i)\<\/?system\>", re.IGNORECASE),
    re.compile(r"(?i)inject\s+(prompt|instruction)", re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> dict[str, Any]:
    matched = []
    for pat in _INJECTION_PATTERNS:
        m = pat.search(text)
        if m:
            matched.append({"pattern": pat.pattern, "match": m.group()})
    if matched:
        logger.warning("Prompt injection detected: %d patterns matched", len(matched))
    return {
        "detected": bool(matched),
        "match_count": len(matched),
        "matches": matched,
    }
