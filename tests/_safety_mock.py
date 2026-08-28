"""Shared mock harness for SafetyGateway guard-delegation tests (#258).

Extracted from tests/test_guard_client.py. MockGuardClient stands in for the
native fusion_guard client: injecting it via SafetyGateway(guard_client=...) (or
GuardSafetyBackend(client=...)) bypasses the real UDS socket so tests are
deterministic and CI-green without fusion_guard installed.
"""

from __future__ import annotations

import uuid


class MockRule:
    def __init__(
        self,
        name,
        pattern,
        action="allow",
        risk_level="l1",
        reason="",
        stage="regex",
        scope="all",
    ):
        self.name = name
        self.pattern = pattern
        self.action = action
        self.risk_level = risk_level
        self.reason = reason
        self.stage = stage
        self.scope = scope


class MockVerdict:
    def __init__(
        self,
        action="allow",
        risk_level="l1",
        reason="",
        requires_approval=False,
        redacted_content=None,
        action_id=None,
        verdict_epoch=1,
        stage="regex",
        inferred_category="",
        category_hint=None,
        verdict_ttl_secs=300,
        seatbelt_required=False,
    ):
        self.action = action
        self.risk_level = risk_level
        self.reason = reason
        self.requires_approval = requires_approval
        self.redacted_content = redacted_content
        self.action_id = action_id
        self.verdict_epoch = verdict_epoch
        self.stage = stage
        self.inferred_category = inferred_category
        self.category_hint = category_hint
        self.verdict_ttl_secs = verdict_ttl_secs
        self.seatbelt_required = seatbelt_required


class MockGuardClient:
    """In-process stand-in for NativeGuardClient. Records calls for assertion."""

    def __init__(
        self,
        verdict=None,
        rules=None,
        epoch=1,
        raise_stale=False,
        unique_action_ids=False,
    ):
        self._verdict = verdict or MockVerdict()
        self._rules = rules or []
        self._epoch = epoch
        self._raise_stale = raise_stale
        self._unique_action_ids = unique_action_ids
        self.evaluate_calls = []
        self.confirm_calls = []
        self.list_rules_calls = 0

    def list_rules(self):
        self.list_rules_calls += 1
        return self._rules, self._epoch

    def evaluate(
        self,
        content,
        caller_epoch=0,
        tenant_id=None,
        requester=None,
        content_type="shell",
        category_hint=None,
    ):
        self.evaluate_calls.append(
            {
                "content": content,
                "caller_epoch": caller_epoch,
                "content_type": content_type,
                "category_hint": category_hint,
            }
        )
        if self._raise_stale and caller_epoch != self._epoch:
            raise RuntimeError(
                f"stale epoch: caller={caller_epoch} guard={self._epoch}"
            )
        verdict = self._verdict
        if self._unique_action_ids and getattr(verdict, "action_id", None) is None:
            verdict = MockVerdict(
                action=verdict.action,
                risk_level=verdict.risk_level,
                reason=verdict.reason,
                requires_approval=verdict.requires_approval,
                redacted_content=verdict.redacted_content,
                action_id=f"mock-{uuid.uuid4().hex[:8]}",
                verdict_epoch=verdict.verdict_epoch,
                stage=verdict.stage,
                inferred_category=verdict.inferred_category,
                category_hint=verdict.category_hint,
                verdict_ttl_secs=verdict.verdict_ttl_secs,
                seatbelt_required=verdict.seatbelt_required,
            )
        return verdict

    def confirm(self, action_id, approved, approved_by=None, tenant_id=None):
        self.confirm_calls.append(
            {
                "action_id": action_id,
                "approved": approved,
                "approved_by": approved_by,
            }
        )
        return MockVerdict(action="allow", reason="confirmed")
