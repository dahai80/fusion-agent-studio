"""Permission engine: layered rules + content scan + plan gate + mode policy."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PERMISSIONS_FILE = Path.home() / ".fusion-agent-studio" / "permissions.json"

DANGEROUS_PATTERNS = (
    r"rm\s+-rf\s+/",
    r"DROP\s+TABLE",
    r"mkfs",
    r"sudo\s+rm",
    r"curl\s+[^|]*\|\s*(ba)?sh",
    r"shutil\.rmtree\(\s*['\"]/(etc|usr|bin|sbin|var|System)",
)

_VALID_ACTIONS = ("deny", "ask", "allow")
_VALID_LAYERS = ("global", "graph", "node")
_RULE_SCAN_ORDER = ("node", "graph", "global")


class PermissionMode(str, Enum):
    DEFAULT = "default"
    APPROVED = "approved"
    BYPASS = "bypass"


@dataclass
class PermissionRule:
    action: str
    tool: str
    path_scope: list[str] = field(default_factory=list)
    layer: str = "global"
    description: str = ""
    created_at: float = 0.0
    # R2 (#347): role scoping (#323 roles metadata). Empty = applies to all
    # roles; "all" in roles = same. ctx.role outside roles -> rule skipped.
    roles: list[str] = field(default_factory=list)

    def matches(self, tool_name: str) -> bool:
        if not self.tool or self.tool == "*":
            return True
        try:
            return re.search(self.tool, tool_name) is not None
        except re.error as exc:
            logger.warning("invalid rule regex tool=%r error=%s", self.tool, exc)
            return False

    def matches_role(self, ctx_role: str) -> bool:
        if not self.roles or "all" in self.roles:
            return True
        return ctx_role in self.roles

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "tool": self.tool,
            "path_scope": list(self.path_scope),
            "layer": self.layer,
            "description": self.description,
            "created_at": self.created_at,
            "roles": list(self.roles),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PermissionRule":
        data = data or {}
        layer = str(data.get("layer", "global"))
        if layer not in _VALID_LAYERS:
            logger.warning("unknown rule layer %r, fallback global", layer)
            layer = "global"
        return cls(
            action=str(data.get("action", "ask")),
            tool=str(data.get("tool", "*")),
            path_scope=list(data.get("path_scope") or []),
            layer=layer,
            description=str(data.get("description", "")),
            created_at=float(data.get("created_at", 0.0)),
            roles=list(data.get("roles") or []),
        )


@dataclass
class PermissionDecision:
    decision: str
    rule_source: str = ""
    reason: str = ""


def _serialize_args(args: Any) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args

    def _walk(obj: Any, out: list) -> None:
        if isinstance(obj, dict):
            for value in obj.values():
                _walk(value, out)
        elif isinstance(obj, (list, tuple, set)):
            for item in obj:
                _walk(item, out)
        elif isinstance(obj, str):
            out.append(obj)

    collected: list = []
    _walk(args, collected)
    if collected:
        return "\n".join(collected)
    try:
        return json.dumps(args, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


class PermissionEngine:
    def __init__(
        self,
        rules: Optional[list[PermissionRule]] = None,
        mode: PermissionMode | str = PermissionMode.DEFAULT,
        readonly_tools: Optional[set[str]] = None,
        plan_readonly_tools: Optional[set[str]] = None,
    ):
        self.mode = self._normalize_mode(mode)
        self._readonly_tools = set(readonly_tools or ())
        # Legacy plan-mode readonly allowlist (runtime _plan_readonly_tools) —
        # honored by the plan gate so stub/legacy registries keep behavior.
        self._plan_readonly_tools = set(plan_readonly_tools or ())
        self._rules_by_layer: dict[str, list[PermissionRule]] = {
            "global": [],
            "graph": [],
            "node": [],
        }
        for rule in rules or []:
            self.add_rule(rule, layer=rule.layer)
        try:
            loaded = self.load_file_rules(PERMISSIONS_FILE)
            if loaded:
                logger.info("loaded %d global permission rules from %s", loaded, PERMISSIONS_FILE)
        except Exception as exc:
            logger.warning("failed to load permission file %s: %s", PERMISSIONS_FILE, exc)

    @staticmethod
    def _normalize_mode(mode: PermissionMode | str) -> PermissionMode:
        if isinstance(mode, PermissionMode):
            return mode
        try:
            return PermissionMode(str(mode).lower())
        except ValueError:
            logger.warning("unknown permission mode %r, fallback default", mode)
            return PermissionMode.DEFAULT

    def add_rule(self, rule: PermissionRule, layer: str = "") -> None:
        target = layer or rule.layer
        if target not in _VALID_LAYERS:
            logger.warning("invalid rule layer %r, fallback global", target)
            target = "global"
        if rule.action not in _VALID_ACTIONS:
            logger.warning("invalid rule action %r on tool=%r", rule.action, rule.tool)
            return
        rule.layer = target
        if rule.created_at <= 0.0:
            rule.created_at = time.time()
        self._rules_by_layer[target].append(rule)
        logger.debug("rule added layer=%s action=%s tool=%s", target, rule.action, rule.tool)

    def set_mode(self, mode: PermissionMode | str) -> None:
        self.mode = self._normalize_mode(mode)
        logger.info("permission mode set to %s", self.mode.value)

    def get_mode(self) -> str:
        return self.mode.value

    def evaluate(
        self,
        tool_name: str,
        args: Any,
        ctx: Any = None,
        readonly_hint: bool = False,
    ) -> PermissionDecision:
        if self.mode == PermissionMode.BYPASS:
            return PermissionDecision("allow", rule_source="mode:bypass")

        if (
            ctx is not None
            and getattr(ctx, "plan_mode", False)
            and not readonly_hint
            and tool_name not in self._plan_readonly_tools
        ):
            return PermissionDecision(
                "deny",
                rule_source="plan_mode",
                reason="plan mode read-only gate",
            )

        blob = _serialize_args(args)
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, blob, re.IGNORECASE):
                logger.warning("content scan blocked tool=%s pattern=%s", tool_name, pattern)
                return PermissionDecision(
                    "deny",
                    rule_source="content_scan",
                    reason=f"dangerous pattern matched: {pattern}",
                )

        pending_ask: Optional[tuple[str, PermissionRule]] = None
        pending_allow: Optional[tuple[str, PermissionRule]] = None
        for layer in _RULE_SCAN_ORDER:
            for rule in self._rules_by_layer[layer]:
                if not rule.matches(tool_name):
                    continue
                # R2: role-scoped rules only apply to matching ctx roles.
                if not rule.matches_role(getattr(ctx, "role", "all") if ctx is not None else "all"):
                    continue
                if rule.action == "deny":
                    logger.info("rule deny tool=%s layer=%s pattern=%s", tool_name, layer, rule.tool)
                    return PermissionDecision(
                        "deny",
                        rule_source=f"rule:{layer}:{rule.tool}",
                        reason=rule.description or f"denied by {layer} rule {rule.tool}",
                    )
                if rule.action == "ask" and pending_ask is None:
                    pending_ask = (layer, rule)
                elif rule.action == "allow" and pending_allow is None:
                    pending_allow = (layer, rule)

        if pending_ask is not None:
            ask_layer, ask_rule = pending_ask
            return PermissionDecision(
                "ask",
                rule_source=f"rule:ask:{ask_layer}",
                reason=ask_rule.description or f"approval required by {ask_layer} rule",
            )
        if pending_allow is not None:
            allow_layer, allow_rule = pending_allow
            return PermissionDecision(
                "allow",
                rule_source=f"rule:allow:{allow_layer}",
                reason=allow_rule.description or f"allowed by {allow_layer} rule",
            )

        if readonly_hint:
            return PermissionDecision(
                "allow",
                rule_source="heuristic_readonly",
                reason="readonly tool auto-allow",
            )

        if self.mode == PermissionMode.APPROVED:
            return PermissionDecision("allow", rule_source="mode:approved")
        if not self.is_armed():
            # Strangler compat: no rules + DEFAULT mode -> legacy behavior
            # (allow; approval semantics stay with SafetyGateway). Plan gate
            # and content scan above remain active regardless.
            return PermissionDecision("allow", rule_source="legacy_passthrough")
        return PermissionDecision(
            "ask",
            rule_source="mode:default",
            reason="default mode requires approval",
        )

    def is_armed(self) -> bool:
        # Strangler-compat: an unarmed engine (no rules, DEFAULT mode) is a
        # passthrough so legacy callers keep exact pre-N2 behavior. The engine
        # arms when any rule exists, the mode is non-default, or the env
        # explicitly selects a mode.
        if self.mode != PermissionMode.DEFAULT:
            return True
        if any(self._rules_by_layer[layer] for layer in _VALID_LAYERS):
            return True
        return os.environ.get("FUSION_PERMISSION_MODE", "").strip() != ""

    def list_rules(self) -> list[dict]:
        out: list[dict] = []
        for layer in _VALID_LAYERS:
            for rule in self._rules_by_layer[layer]:
                out.append(rule.to_dict())
        return out

    def rules_count(self) -> int:
        return sum(len(rules) for rules in self._rules_by_layer.values())

    def save_to_file(self, path: Optional[Path] = None) -> bool:
        target = Path(path) if path else PERMISSIONS_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {"rules": [rule.to_dict() for rule in self._rules_by_layer["global"]]}
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")
            os.chmod(target, 0o600)
            logger.info("saved %d global rules to %s", len(payload["rules"]), target)
            return True
        except OSError as exc:
            logger.error("failed to save permissions to %s: %s", target, exc)
            return False

    def load_file_rules(self, path: Optional[Path] = None) -> int:
        target = Path(path) if path else PERMISSIONS_FILE
        if not target.exists():
            logger.debug("no permission file at %s", target)
            return 0
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            entries = payload.get("rules", []) if isinstance(payload, dict) else []
            count = 0
            for entry in entries:
                try:
                    rule = PermissionRule.from_dict(entry)
                except (TypeError, ValueError) as exc:
                    logger.warning("skip malformed rule in %s: %s", target, exc)
                    continue
                self._rules_by_layer["global"].append(rule)
                count += 1
            logger.info("loaded %d rules from %s", count, target)
            return count
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("failed to parse permission file %s: %s", target, exc)
            return 0
