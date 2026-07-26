"""Safety gateway - 3-level Human-in-the-Loop safety system.

L1 (Autonomous): Agent acts silently. No approval needed.
L2 (Preview): Agent shows diff/plan, waits for user confirm before executing.
L3 (Gateway): Agent must get explicit approval before every action.

Also provides content filtering for dangerous patterns.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
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


_DANGEROUS_PATTERNS = [
    SafetyRule(
        name="rm-rf",
        pattern="rm -rf /",
        action=SafetyAction.BLOCK,
        reason="Destructive filesystem command",
        min_level=SafetyLevel.L3,
    ),
    SafetyRule(
        name="drop-table",
        pattern="DROP TABLE",
        action=SafetyAction.BLOCK,
        reason="Destructive database operation",
        min_level=SafetyLevel.L3,
    ),
    SafetyRule(
        name="delete-from",
        pattern="DELETE FROM",
        action=SafetyAction.PREVIEW,
        reason="Database deletion requires review",
        min_level=SafetyLevel.L2,
    ),
    SafetyRule(
        name="env-secrets",
        pattern=r"(?i)(password|secret|token|api_key)\s*=\s*\S+",
        action=SafetyAction.REDACT,
        reason="Potential secret exposure",
        min_level=SafetyLevel.L1,
    ),
    SafetyRule(
        name="network-bind",
        pattern=r"0\.0\.0\.0",
        action=SafetyAction.PREVIEW,
        reason="Binding to all interfaces may expose service",
        min_level=SafetyLevel.L2,
    ),
]

_DEFAULT_POLICIES = [
    SafetyPolicy(
        category=CAT_CODE_ANALYSIS,
        default_level=SafetyLevel.L1,
        requires_diff=False,
        description="Code AST analysis, runs silently",
    ),
    SafetyPolicy(
        category=CAT_DOC_RETRIEVAL,
        default_level=SafetyLevel.L1,
        requires_diff=False,
        description="Document retrieval, runs silently",
    ),
    SafetyPolicy(
        category=CAT_KNOWLEDGE_SEARCH,
        default_level=SafetyLevel.L1,
        requires_diff=False,
        description="Plaza knowledge search, runs silently",
    ),
    SafetyPolicy(
        category=CAT_FILE_READ,
        default_level=SafetyLevel.L1,
        requires_diff=False,
        description="File read operations, runs silently",
    ),
    SafetyPolicy(
        category=CAT_FILE_WRITE,
        default_level=SafetyLevel.L2,
        requires_diff=True,
        description="File write requires diff preview",
    ),
    SafetyPolicy(
        category=CAT_CODE_EDIT,
        default_level=SafetyLevel.L2,
        requires_diff=True,
        description="Code edit requires diff preview",
    ),
    SafetyPolicy(
        category=CAT_SHELL_EXEC,
        default_level=SafetyLevel.L3,
        requires_diff=False,
        description="Shell command execution requires gateway approval",
    ),
    SafetyPolicy(
        category=CAT_GIT_PUSH,
        default_level=SafetyLevel.L3,
        requires_diff=False,
        description="Git push to main requires gateway approval",
    ),
    SafetyPolicy(
        category=CAT_DATABASE_WRITE,
        default_level=SafetyLevel.L3,
        requires_diff=False,
        description="Database write operations require gateway approval",
    ),
    SafetyPolicy(
        category=CAT_NETWORK_ACCESS,
        default_level=SafetyLevel.L2,
        requires_diff=False,
        description="Network access requires preview",
    ),
]

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
    ):
        self.level = level
        self.custom_rules = custom_rules or []
        self.approver = approver
        self._policies = policies if policies is not None else list(_DEFAULT_POLICIES)
        self._pending_approvals: dict[str, asyncio.Future[bool]] = {}
        self._pending_action_approvals: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def rules(self) -> list[SafetyRule]:
        return _DANGEROUS_PATTERNS + self.custom_rules

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
            logger.info("Added safety policy: %s -> %s", policy.category, policy.default_level.value)

    def evaluate_action(
        self,
        category: str,
        content: str = "",
        context: str = "",
    ) -> SafetyVerdict:
        policy = self.get_policy(category)

        if policy is None:
            logger.warning("No policy for category '%s', defaulting to L3 BLOCK", category)
            return SafetyVerdict(
                action=SafetyAction.BLOCK,
                reason=f"No safety policy defined for category '{category}'",
                requires_approval=True,
                metadata={"category": category, "policy_missing": True},
            )

        required_level = policy.default_level

        content_verdict = self.check(content, context) if content else None
        if content_verdict is not None and content_verdict.action == SafetyAction.BLOCK:
            logger.info("Content check blocked action in category '%s'", category)
            content_verdict.metadata["category"] = category
            return content_verdict

        if required_level == SafetyLevel.L1:
            logger.debug("L1 auto-approve category '%s': %s", category, policy.description)
            return SafetyVerdict(
                action=SafetyAction.ALLOW,
                reason=policy.description,
                requires_approval=False,
                metadata={"category": category, "level": "L1"},
            )

        if required_level == SafetyLevel.L2:
            if policy.requires_diff and content:
                action_id = str(uuid.uuid4())
                diff_preview = self.generate_diff_preview(content, context or "", category, action_id)
                logger.info("L2 preview required for category '%s', action_id=%s", category, action_id)
                return SafetyVerdict(
                    action=SafetyAction.PREVIEW,
                    reason=policy.description,
                    requires_approval=True,
                    metadata={"category": category, "level": "L2"},
                    diff_preview=diff_preview,
                )
            else:
                action_id = str(uuid.uuid4())
                with self._lock:
                    self._pending_action_approvals[action_id] = {
                        "category": category,
                        "content": content,
                        "level": "L2",
                        "status": "pending",
                    }
                logger.info("L2 preview required for category '%s' (no diff), action_id=%s", category, action_id)
                return SafetyVerdict(
                    action=SafetyAction.PREVIEW,
                    reason=policy.description,
                    requires_approval=True,
                    metadata={"category": category, "level": "L2", "action_id": action_id},
                )

        if required_level == SafetyLevel.L3:
            action_id = str(uuid.uuid4())
            with self._lock:
                self._pending_action_approvals[action_id] = {
                    "category": category,
                    "content": content,
                    "level": "L3",
                    "status": "pending",
                }
            logger.warning("L3 gateway required for category '%s', action_id=%s", category, action_id)
            return SafetyVerdict(
                action=SafetyAction.BLOCK,
                reason=policy.description,
                requires_approval=True,
                metadata={"category": category, "level": "L3", "action_id": action_id},
            )

        return SafetyVerdict(action=SafetyAction.ALLOW)

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
        diff_lines = list(difflib.unified_diff(
            original_lines,
            proposed_lines,
            fromfile="original",
            tofile="proposed",
        ))
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

        logger.debug("Generated diff preview for action_id=%s, category=%s", action_id, category)
        return request

    def approve_action(self, action_id: str) -> bool:
        with self._lock:
            pending = self._pending_action_approvals.get(action_id)
            if pending is None:
                logger.warning("approve_action: no pending action with id=%s", action_id)
                return False
            if pending["status"] != "pending":
                logger.warning("approve_action: action %s already resolved as %s", action_id, pending["status"])
                return False
            pending["status"] = "approved"

        if action_id in self._pending_approvals:
            future = self._pending_approvals[action_id]
            if not future.done():
                future.set_result(True)

        logger.info("Action approved: action_id=%s", action_id)
        return True

    def reject_action(self, action_id: str) -> bool:
        with self._lock:
            pending = self._pending_action_approvals.get(action_id)
            if pending is None:
                logger.warning("reject_action: no pending action with id=%s", action_id)
                return False
            if pending["status"] != "pending":
                logger.warning("reject_action: action %s already resolved as %s", action_id, pending["status"])
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
        verdicts = []
        for rule in self.rules:
            if self._level_ord(self.level) < self._level_ord(rule.min_level):
                continue
            if re.search(rule.pattern, content, re.IGNORECASE):
                verdicts.append(SafetyVerdict(
                    action=rule.action,
                    reason=rule.reason,
                    requires_approval=rule.action in (SafetyAction.PREVIEW, SafetyAction.BLOCK),
                    metadata={"rule": rule.name, "pattern": rule.pattern},
                ))

        if not verdicts:
            if self.level == SafetyLevel.L3:
                return SafetyVerdict(
                    action=SafetyAction.PREVIEW,
                    reason="L3 requires approval for all actions",
                    requires_approval=True,
                )
            return SafetyVerdict(action=SafetyAction.ALLOW)

        most_restrictive = max(verdicts, key=lambda v: self._action_ord(v.action))

        if most_restrictive.action == SafetyAction.REDACT:
            redacted = content
            for rule in self.rules:
                if rule.action == SafetyAction.REDACT:
                    redacted = re.sub(rule.pattern, "[REDACTED]", redacted, flags=re.IGNORECASE)
            most_restrictive.redacted_content = redacted

        return most_restrictive

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
            logger.warning("No approver set for L%d, denying: %s", self._level_ord(self.level), action_id)
            return False

        try:
            approved = await asyncio.wait_for(
                self.approver(action_id, description),
                timeout=timeout,
            )
            logger.info("Approval %s for %s", "granted" if approved else "denied", action_id)
            return approved
        except asyncio.TimeoutError:
            logger.warning("Approval timed out for %s", action_id)
            return False

    def check_and_approve_sync(self, content: str, context: str = "") -> SafetyVerdict:
        verdict = self.check(content, context)

        if self.level == SafetyLevel.L1 and verdict.action != SafetyAction.BLOCK:
            verdict.requires_approval = False

        return verdict

    def set_level(self, level: SafetyLevel) -> None:
        old = self.level
        self.level = level
        logger.info("Safety level changed: %s -> %s", old, level)

    @staticmethod
    def _level_ord(level: SafetyLevel) -> int:
        return {SafetyLevel.L1: 1, SafetyLevel.L2: 2, SafetyLevel.L3: 3}[level]

    @staticmethod
    def _action_ord(action: SafetyAction) -> int:
        return {
            SafetyAction.ALLOW: 0,
            SafetyAction.REDACT: 1,
            SafetyAction.PREVIEW: 2,
            SafetyAction.BLOCK: 3,
        }[action]
