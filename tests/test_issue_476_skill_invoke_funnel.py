from pathlib import Path

import pytest

import agent_runtime.permissions as perms_mod
from agent_runtime.dispatchers.skills import SkillsDispatcher
from agent_runtime.funnels import ToolFunnel
from agent_runtime.permissions import PermissionEngine, PermissionRule
from agent_runtime.token_budget import TokenBudget

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(perms_mod, "PERMISSIONS_FILE", tmp_path / "permissions.json")
    monkeypatch.delenv("FUSION_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("FUSION_PERMISSION_PASSTHROUGH", raising=False)
    monkeypatch.delenv("FUSION_PERMISSION_A3", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_REQUIRE_PERMISSIONS", raising=False)
    monkeypatch.delenv("FUSION_FUNNEL_PERMISSIONS", raising=False)


class _StoreStub:
    def __init__(self):
        self.invocations = []

    def invoke_skill(self, name, values):
        self.invocations.append((name, values))
        return {"ok": True, "prompt": "rendered"}


class _RuntimeStub:
    def __init__(self, funnel):
        self.tool_funnel = funnel


class _DaemonStub:
    def __init__(self, runtime, store):
        self._runtime = runtime
        self._skills_store = store

    def _get_runtime(self):
        return self._runtime


def _dispatcher(engine, budget=None):
    funnel = ToolFunnel(permissions=engine)
    if budget is not None:
        funnel.set_budget_ref(budget)
    store = _StoreStub()
    disp = SkillsDispatcher(_DaemonStub(_RuntimeStub(funnel), store))
    return disp, store


class TestIssue476SkillInvokeFunnel:
    @pytest.mark.asyncio
    async def test_deny_rule_blocks_skill_invoke(self):
        engine = PermissionEngine(
            rules=[PermissionRule(action="deny", tool="skill.invoke")]
        )
        disp, store = _dispatcher(engine)
        result = await disp._handle_invoke({"name": "deploy", "params": {"env": "prod"}})
        assert result["status"] == "error"
        assert "Blocked by permission" in result["message"]
        assert store.invocations == []

    @pytest.mark.asyncio
    async def test_allow_rule_reaches_store(self):
        engine = PermissionEngine(
            rules=[PermissionRule(action="allow", tool="skill.invoke")]
        )
        disp, store = _dispatcher(engine)
        result = await disp._handle_invoke({"name": "deploy", "params": {"env": "prod"}})
        assert result["status"] == "ok"
        assert store.invocations == [("deploy", {"env": "prod"})]

    @pytest.mark.asyncio
    async def test_budget_exhaustion_blocks_skill_invoke(self):
        engine = PermissionEngine(
            rules=[PermissionRule(action="allow", tool="skill.invoke")]
        )
        budget = TokenBudget(day_max_tokens=10)
        budget.record_usage(50, 50)
        disp, store = _dispatcher(engine, budget=budget)
        result = await disp._handle_invoke({"name": "deploy", "params": {}})
        assert result["status"] == "error"
        assert "Blocked by budget" in result["message"]
        assert store.invocations == []

    @pytest.mark.asyncio
    async def test_param_validation_precedes_funnel(self):
        engine = PermissionEngine(
            rules=[PermissionRule(action="deny", tool="skill.invoke")]
        )
        disp, store = _dispatcher(engine)
        result = await disp._handle_invoke({"name": ""})
        assert result["status"] == "error"
        assert result["message"] == "name required"
        assert store.invocations == []


class TestIssue476CommentAccuracy:
    def test_protocol_plane_comment_no_false_tool_claim(self):
        text = (
            REPO_ROOT / "agent_runtime" / "protocol_plane.py"
        ).read_text(encoding="utf-8")
        assert "skill.execute" not in text
        assert "skills.invoke" in text
