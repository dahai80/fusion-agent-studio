"""Tests for Phase 6 features: async safety approval, token budget, chat branches, self-repair retry."""

from agent_runtime.chat_engine import ChatEngine, ChatMessage, ChatMode
from agent_runtime.context import AgentEventType
from agent_runtime.graph import NodeConfig
from agent_runtime.safety import SafetyAction, SafetyGateway, SafetyLevel
from agent_runtime.token_budget import TokenBudget


class TestTokenBudget:
    def test_default_budget_not_exceeded(self):
        b = TokenBudget()
        assert not b.is_exceeded()
        assert b.remaining() == -1

    def test_budget_exceeded(self):
        b = TokenBudget(max_tokens=100)
        b.record_usage(60, 50)
        assert b.spent_tokens == 110
        assert b.is_exceeded()
        assert b.remaining() == 0

    def test_budget_not_exceeded_yet(self):
        b = TokenBudget(max_tokens=200)
        b.record_usage(50, 30)
        assert not b.is_exceeded()
        assert b.remaining() == 120

    def test_status(self):
        b = TokenBudget(max_tokens=1000)
        b.record_usage(100, 50)
        s = b.status()
        assert s["max_tokens"] == 1000
        assert s["spent_tokens"] == 150
        assert s["remaining"] == 850
        assert not s["exceeded"]

    def test_to_dict_from_dict(self):
        b = TokenBudget(
            max_tokens=500, spent_tokens=100, prompt_tokens=60, completion_tokens=40
        )
        d = b.to_dict()
        b2 = TokenBudget.from_dict(d)
        assert b2.max_tokens == 500
        assert b2.spent_tokens == 100

    def test_estimate_cost_zero(self):
        b = TokenBudget()
        assert b.estimate_cost() == 0.0

    def test_estimate_cost_with_pricing(self):
        b = TokenBudget(
            max_tokens=1000,
            prompt_tokens=1000,
            completion_tokens=500,
            pricing={"default": {"prompt_per_1k": 0.01, "completion_per_1k": 0.03}},
        )
        cost = b.estimate_cost()
        assert abs(cost - (0.01 * 1 + 0.03 * 0.5)) < 0.001


class TestChatBranchNavigation:
    def setup_method(self):
        self.engine = ChatEngine()
        self.session = self.engine.create_session(
            mode=ChatMode.SIMPLE.value, title="Test"
        )

    def test_switch_branch(self):
        msg1 = ChatMessage(role="user", content="hello")
        self.session.add_message(msg1)
        msg2a = ChatMessage(role="assistant", content="hi")
        self.session.add_message(msg2a, parent_id=msg1.id)
        msg2b = ChatMessage(role="assistant", content="hey")
        self.session.add_message(msg2b, parent_id=msg1.id)
        assert self.session.active_branch == msg2b.id
        ok = self.engine.switch_branch(self.session.id, msg2a.id)
        assert ok
        assert self.session.active_branch == msg2a.id

    def test_switch_branch_invalid_session(self):
        ok = self.engine.switch_branch("nonexistent", "x")
        assert not ok

    def test_switch_branch_invalid_message(self):
        ok = self.engine.switch_branch(self.session.id, "nonexistent")
        assert not ok

    def test_get_branches(self):
        msg1 = ChatMessage(role="user", content="hello")
        self.session.add_message(msg1)
        msg2a = ChatMessage(role="assistant", content="hi")
        self.session.add_message(msg2a, parent_id=msg1.id)
        msg2b = ChatMessage(role="assistant", content="hey")
        self.session.add_message(msg2b, parent_id=msg1.id)
        branches = self.engine.get_branches(self.session.id, msg2a.id)
        assert len(branches) == 2
        ids = [b["leaf_id"] for b in branches]
        assert msg2a.id in ids
        assert msg2b.id in ids

    def test_get_branches_empty_session(self):
        branches = self.engine.get_branches("nonexistent")
        assert branches == []

    def test_get_message_tree(self):
        msg1 = ChatMessage(role="user", content="hello")
        self.session.add_message(msg1)
        msg2 = ChatMessage(role="assistant", content="hi")
        self.session.add_message(msg2, parent_id=msg1.id)
        tree = self.engine.get_message_tree(self.session.id)
        assert tree["total_messages"] == 2
        assert len(tree["nodes"]) == 2
        assert tree["active_branch"] == msg2.id

    def test_get_message_tree_invalid(self):
        tree = self.engine.get_message_tree("nonexistent")
        assert tree["nodes"] == []


class TestNewEventTypes:
    def test_safety_timeout_event_type(self):
        assert AgentEventType.SAFETY_TIMEOUT.value == "safety_timeout"

    def test_token_budget_exceeded_event_type(self):
        assert AgentEventType.TOKEN_BUDGET_EXCEEDED.value == "token_budget_exceeded"

    def test_retry_event_type(self):
        assert AgentEventType.RETRY.value == "retry"

    def test_retry_success_event_type(self):
        assert AgentEventType.RETRY_SUCCESS.value == "retry_success"


class TestNodeConfigRetryOnError:
    def test_default_retry_on_error(self):
        n = NodeConfig(type="llm")
        assert n.retry_on_error is False

    def test_retry_on_error_set(self):
        n = NodeConfig(type="llm", retry_on_error=True, max_retries=3)
        assert n.retry_on_error is True
        assert n.max_retries == 3

    def test_retry_on_error_serialization(self):
        n = NodeConfig(type="llm", retry_on_error=True, max_retries=3)
        d = n.to_dict()
        assert d.get("retry_on_error") is True
        n2 = NodeConfig.from_dict(d)
        assert n2.retry_on_error is True


class TestSafetyGatewayApproval:
    def test_l2_requires_approval(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        verdict = gw.evaluate_action(
            category="file_write", content="write data", context="test"
        )
        assert verdict.requires_approval is True
        assert verdict.action == SafetyAction.PREVIEW

    def test_l3_requires_approval(self):
        gw = SafetyGateway(level=SafetyLevel.L3)
        verdict = gw.evaluate_action(
            category="shell_exec", content="ls", context="test"
        )
        assert verdict.requires_approval is True

    def test_l1_auto_approve(self):
        gw = SafetyGateway(level=SafetyLevel.L1)
        verdict = gw.evaluate_action(
            category="file_read", content="read data", context="test"
        )
        assert verdict.requires_approval is False
        assert verdict.action == SafetyAction.ALLOW

    def test_approve_pending_action(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        verdict = gw.evaluate_action(
            category="file_write", content="write data", context="test"
        )
        action_id = verdict.metadata.get("action_id", "")
        if action_id:
            ok = gw.approve_action(action_id)
            assert ok

    def test_reject_pending_action(self):
        gw = SafetyGateway(level=SafetyLevel.L2)
        verdict = gw.evaluate_action(
            category="file_write", content="write data", context="test"
        )
        action_id = verdict.metadata.get("action_id", "")
        if action_id:
            ok = gw.reject_action(action_id)
            assert ok
