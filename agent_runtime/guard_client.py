"""Guard safety backend — thin client delegating SafetyGateway judgment to fusion-guard.

fusion-guard (per-host zero-trust authorization daemon, UDS JSON-RPC at
/tmp/fusion-guard.sock) is the single security-rule SSOT. This adapter mirrors
the fusion_memory_adapter env-gated pattern: lazy-import fusion_guard (local
maturin build, NOT a PyPI dep), duck-type the judgment surface, degrade to a
fail-closed floor when guard is unreachable (never open).

Verdict mapping (guard 4-level -> agent-studio SafetyVerdict):
  l1/allow   -> ALLOW
  l2/preview -> PREVIEW (diff added by SafetyGateway for file_write/code_edit)
  l3/block   -> BLOCK + requires_approval + action_id in metadata (guard.confirm)
  l4/block   -> BLOCK absolute (H8: no confirm, requires_approval=False)
  redact     -> REDACT + redacted_content from guard
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from .safety import (
    CAT_CODE_ANALYSIS,
    CAT_CODE_EDIT,
    CAT_FILE_READ,
    CAT_FILE_WRITE,
    CAT_SHELL_EXEC,
    SafetyAction,
    SafetyVerdict,
)

logger = logging.getLogger(__name__)

_GUARD_SOCK_DEFAULT = "/tmp/fusion-guard.sock"
_RULES_CACHE_PATH = os.path.expanduser("~/.fusion-guard/rules-cache.json")

# content_type per category (guard evaluate param). shell=tokenize, code=tree-sitter,
# text=regex-only. Conservative: only shell_exec -> shell, code_* -> code, rest text.
_CONTENT_TYPE = {
    CAT_SHELL_EXEC: "shell",
    CAT_CODE_EDIT: "code",
    CAT_CODE_ANALYSIS: "code",
    CAT_FILE_WRITE: "text",
    CAT_FILE_READ: "text",
}

# Fail-closed floor: guard-down + no cache. High-risk -> BLOCK, secret -> REDACT,
# benign -> ALLOW. "never open" — never allow destructive ops when guard unreachable.
_FAIL_CLOSED_FLOOR = [
    {
        "name": "rm-rf-root",
        "pattern": r"rm\s+-rf\s+/",
        "action": SafetyAction.BLOCK,
        "reason": "Destructive filesystem command (guard-down floor)",
        "risk": "l4",
    },
    {
        "name": "drop-table",
        "pattern": r"DROP\s+TABLE",
        "action": SafetyAction.BLOCK,
        "reason": "Destructive database operation (guard-down floor)",
        "risk": "l4",
    },
    {
        "name": "env-secrets",
        "pattern": r"(?i)(password|secret|token|api_key)\s*=\s*\S+",
        "action": SafetyAction.REDACT,
        "reason": "Potential secret exposure (guard-down floor)",
        "risk": "l1",
    },
]


class GuardSafetyBackend:
    """Thin client delegating safety judgment to the fusion-guard daemon."""

    def __init__(
        self,
        sock_path: str | None = None,
        tenant_id: str | None = None,
        requester: str | None = None,
        client=None,
    ):
        self._sock_path = sock_path or os.environ.get("FUSION_GUARD_SOCK", _GUARD_SOCK_DEFAULT)
        self._tenant_id = tenant_id
        self._requester = requester
        self._client = client
        self._epoch = 0
        self._rules_cache: list[dict] | None = None
        self._available: bool | None = None
        self._connect()

    def _resolve_tenant_id(self) -> str | None:
        # #271: identity on -> source tenant_id from verified TenantContext (set by
        # fusion-core TenantMiddleware per-request contextvar). Identity off or no
        # active context -> fall back to caller-supplied self._tenant_id.
        try:
            from agent_runtime.identity_integration import is_identity_enabled

            if is_identity_enabled():
                from fusion_core.tenant.context import current

                ctx = current()
                if ctx is not None:
                    return ctx.tenant_id
        except Exception as e:
            logger.debug("resolve tenant context failed: %s", e)
        return self._tenant_id

    # --- connection + rules cache ---

    def _connect(self) -> None:
        # lazy import; fusion_guard is a local maturin build, absent in CI.
        if self._client is not None:
            self._available = True
            self._ensure_rules()
            return
        try:
            from fusion_guard import NativeGuardClient
            self._client = NativeGuardClient(sock_path=self._sock_path)
            self._client.ping()
            self._available = True
            logger.info("GuardSafetyBackend connected to guard at %s", self._sock_path)
            self._ensure_rules()
        except Exception as e:
            self._available = False
            self._client = None
            logger.warning("GuardSafetyBackend guard-down (%s); fail-closed floor active", e)
            self._load_cached_rules()

    def is_available(self) -> bool:
        if self._available is None:
            self._connect()
        return bool(self._available)

    def _ensure_rules(self) -> None:
        # fetch list_rules -> cache caller_epoch + write H2 cache file.
        if self._client is None:
            self._load_cached_rules()
            return
        try:
            rules, epoch = self._client.list_rules()
            self._epoch = epoch
            self._rules_cache = [
                {
                    "name": r.name,
                    "pattern": r.pattern,
                    "stage": r.stage,
                    "action": r.action,
                    "risk_level": r.risk_level,
                    "reason": r.reason,
                    "scope": r.scope,
                }
                for r in rules
            ]
            self._write_rules_cache(epoch)
            logger.info("GuardSafetyBackend rules fetched: epoch=%d count=%d", epoch, len(rules))
        except Exception as e:
            logger.warning("GuardSafetyBackend list_rules failed: %s; loading cache", e)
            self._load_cached_rules()

    def _write_rules_cache(self, epoch: int) -> None:
        # H2 cache: ~/.fusion-guard/rules-cache.json {rules, epoch, cached_at}
        try:
            cache_dir = os.path.dirname(_RULES_CACHE_PATH)
            if cache_dir and not os.path.isdir(cache_dir):
                os.makedirs(cache_dir, exist_ok=True)
            payload = {
                "rules": self._rules_cache or [],
                "epoch": epoch,
                "cached_at": time.time(),
            }
            with open(_RULES_CACHE_PATH, "w") as f:
                json.dump(payload, f)
        except Exception as e:
            logger.warning("GuardSafetyBackend rules cache write failed: %s", e)

    def _load_cached_rules(self) -> None:
        try:
            if os.path.isfile(_RULES_CACHE_PATH):
                with open(_RULES_CACHE_PATH) as f:
                    payload = json.load(f)
                self._rules_cache = payload.get("rules", [])
                self._epoch = payload.get("epoch", 0)
                logger.info(
                    "GuardSafetyBackend loaded cached rules: epoch=%d count=%d",
                    self._epoch,
                    len(self._rules_cache or []),
                )
            else:
                self._rules_cache = None
        except Exception as e:
            logger.warning("GuardSafetyBackend rules cache load failed: %s", e)
            self._rules_cache = None

    # --- evaluation ---

    def evaluate(self, category: str, content: str, context: str = "") -> SafetyVerdict:
        if not self.is_available():
            return self._fail_closed(category, content)
        try:
            return self._eval_with_retry(category, content, context)
        except Exception as e:
            logger.warning("GuardSafetyBackend evaluate failed: %s; fail-closed", e)
            self._available = False
            return self._fail_closed(category, content)

    def _eval_with_retry(self, category: str, content: str, context: str) -> SafetyVerdict:
        # stale-epoch (-32003) -> refetch rules -> retry once with new epoch.
        content_type = _CONTENT_TYPE.get(category, "text")
        try:
            gv = self._client.evaluate(
                content=content,
                caller_epoch=self._epoch,
                tenant_id=self._resolve_tenant_id(),
                requester=self._requester,
                content_type=content_type,
                category_hint=category if category else None,
            )
            return self._map_verdict(gv, category, content)
        except RuntimeError as e:
            if "stale epoch" in str(e).lower():
                logger.info("GuardSafetyBackend stale epoch, refetching rules: %s", e)
                self._ensure_rules()
                gv = self._client.evaluate(
                    content=content,
                    caller_epoch=self._epoch,
                    tenant_id=self._resolve_tenant_id(),
                    requester=self._requester,
                    content_type=content_type,
                    category_hint=category if category else None,
                )
                return self._map_verdict(gv, category, content)
            raise

    def _map_verdict(self, gv, category: str, content: str) -> SafetyVerdict:
        # NativeGuardVerdict -> SafetyVerdict. guard action lowercase -> SafetyAction.
        action_str = (gv.action or "allow").lower()
        risk = (gv.risk_level or "l1").lower()
        action = {
            "allow": SafetyAction.ALLOW,
            "preview": SafetyAction.PREVIEW,
            "block": SafetyAction.BLOCK,
            "redact": SafetyAction.REDACT,
        }.get(action_str, SafetyAction.ALLOW)

        # l4 = absolute block (H8): no confirm. l3 block = requires_approval.
        requires_approval = bool(gv.requires_approval)
        if action == SafetyAction.BLOCK and risk == "l4":
            requires_approval = False

        metadata = {
            "category": category,
            "risk_level": risk,
            "inferred_category": getattr(gv, "inferred_category", "") or "",
            "verdict_epoch": getattr(gv, "verdict_epoch", 0),
            "stage": getattr(gv, "stage", "") or "",
            "backend": "guard",
        }
        action_id = getattr(gv, "action_id", None)
        if action_id:
            metadata["action_id"] = action_id
            metadata["level"] = "L3" if risk == "l3" else ("L4" if risk == "l4" else "L2")

        redacted = getattr(gv, "redacted_content", None) or ""

        logger.info(
            "GuardSafetyBackend verdict: action=%s risk=%s category=%s action_id=%s",
            action.value,
            risk,
            category,
            action_id,
        )
        return SafetyVerdict(
            action=action,
            reason=gv.reason or "",
            redacted_content=redacted,
            requires_approval=requires_approval,
            metadata=metadata,
        )

    # --- approval ---

    def confirm(self, action_id: str, approved: bool, approved_by: str | None = None) -> bool:
        if not self.is_available() or self._client is None:
            logger.warning("GuardSafetyBackend.confirm guard-down: action_id=%s", action_id)
            return False
        try:
            self._client.confirm(
                action_id=action_id,
                approved=approved,
                approved_by=approved_by,
                tenant_id=self._resolve_tenant_id(),
            )
            logger.info("GuardSafetyBackend.confirm: action_id=%s approved=%s", action_id, approved)
            return True
        except Exception as e:
            logger.warning("GuardSafetyBackend.confirm failed: action_id=%s err=%s", action_id, e)
            return False

    # --- guard-down fail-closed ---

    def _fail_closed(self, category: str, content: str) -> SafetyVerdict:
        # guard-down: use cached rules if present, else floor. high-risk -> block.
        if self._rules_cache:
            verdict = self._match_cached_rules(category, content)
            if verdict is not None:
                return verdict
        return self._match_floor(category, content)

    def _match_cached_rules(self, category: str, content: str) -> SafetyVerdict | None:
        # cached guard rules: l3/l4 -> block, l2 -> preview, l1 -> allow. most-restrictive wins.
        if not content:
            return None
        best: SafetyVerdict | None = None
        best_ord = -1
        order = {SafetyAction.ALLOW: 0, SafetyAction.REDACT: 1, SafetyAction.PREVIEW: 2, SafetyAction.BLOCK: 3}
        for rule in self._rules_cache or []:
            pattern = rule.get("pattern", "")
            if not pattern:
                continue
            try:
                if not re.search(pattern, content, re.IGNORECASE):
                    continue
            except re.error:
                continue
            r_action = rule.get("action", "allow").lower()
            r_risk = rule.get("risk_level", "l1").lower()
            action = {
                "allow": SafetyAction.ALLOW,
                "preview": SafetyAction.PREVIEW,
                "block": SafetyAction.BLOCK,
                "redact": SafetyAction.REDACT,
            }.get(r_action, SafetyAction.ALLOW)
            requires_approval = action in (SafetyAction.PREVIEW, SafetyAction.BLOCK) and r_risk != "l4"
            v = SafetyVerdict(
                action=action,
                reason=rule.get("reason", "guard-down cached rule match"),
                requires_approval=requires_approval,
                metadata={
                    "category": category,
                    "risk_level": r_risk,
                    "rule": rule.get("name", ""),
                    "backend": "guard-cache",
                },
            )
            if order[action] > best_ord:
                best, best_ord = v, order[action]
        if best is not None and best.action == SafetyAction.REDACT:
            best.redacted_content = self._redact_secrets(content)
        return best

    def _match_floor(self, category: str, content: str) -> SafetyVerdict:
        # minimal fail-closed: rm-rf/DROP TABLE -> block, secret -> redact, else allow.
        if content:
            for rule in _FAIL_CLOSED_FLOOR:
                if re.search(rule["pattern"], content, re.IGNORECASE):
                    logger.warning(
                        "GuardSafetyBackend floor match: %s -> %s", rule["name"], rule["action"].value
                    )
                    v = SafetyVerdict(
                        action=rule["action"],
                        reason=rule["reason"],
                        requires_approval=False,
                        metadata={
                            "category": category,
                            "risk_level": rule["risk"],
                            "rule": rule["name"],
                            "backend": "guard-floor",
                        },
                    )
                    if rule["action"] == SafetyAction.REDACT:
                        v.redacted_content = self._redact_secrets(content)
                    return v
        return SafetyVerdict(
            action=SafetyAction.ALLOW,
            reason="guard-down floor: benign content",
            requires_approval=False,
            metadata={"category": category, "backend": "guard-floor"},
        )

    @staticmethod
    def _redact_secrets(content: str) -> str:
        return re.sub(
            r"(?i)(password|secret|token|api_key)\s*=\s*\S+",
            "[REDACTED]",
            content,
        )
