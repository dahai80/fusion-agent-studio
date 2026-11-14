"""#675: fusion-harness 0.10 kernel surface wiring.

Layers:
  - token counter -> CompactionConfig.token_counter (CJK-aware) on runtime
  - sandbox policy gate on the tool funnel (BLOCKED tier denies admission)
  - SandboxExecRunner (darwin sandbox-exec, shell-command tools only)
  - skill registry + derive_permissions -> PermissionEngine rules
  - kernel CheckpointStore mirror at _save_checkpoint boundary
  - kernel orchestrator (SubagentRunner/BackgroundPool/ParallelTeam) with
    agent_fn delegating to the in-tree SubAgentRunner
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from agent_runtime import AgentRuntime
from agent_runtime.kernel_wiring import (
    KernelCheckpointer,
    SandboxExecRunner,
    build_compaction_config,
    build_kernel_orchestrator,
    build_sandbox_policy,
    wire_kernel_surfaces,
)
from server.fusion_mlx_client import FusionMLXClient


class _StubClient(FusionMLXClient):
    def __init__(self):
        super().__init__(base_url="http://127.0.0.1:1")

    async def chat(self, *a, **k):
        from server.fusion_mlx_client import LLMResponse

        return LLMResponse(content="ok", model="stub")


def _runtime(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    if "FUSION_SKILLS_DIR" not in os.environ:
        empty_skills = tmp_path / "empty-skills"
        empty_skills.mkdir(exist_ok=True)
        monkeypatch.setenv("FUSION_SKILLS_DIR", str(empty_skills))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return AgentRuntime(llm_gateway=_StubClient())


def _write_skill(root, name, frontmatter="", body="# Body\n\ndo things"):
    d = root / name
    d.mkdir(parents=True)
    text = f"---\nname: {name}\ndescription: test skill {name}\n{frontmatter}---\n{body}"
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


def _skill_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    skills = home / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(home))
    monkeypatch.setenv("FUSION_SKILLS_DIR", str(skills))
    return skills


# ---------------- token counter ----------------

def test_compaction_config_token_counter_on(monkeypatch):
    monkeypatch.setenv("FUSION_TOKEN_COUNTER", "1")
    cfg = build_compaction_config()
    assert cfg is not None and cfg.token_counter is not None
    assert cfg.token_counter.count_text("你好世界 test") > 0


def test_compaction_config_disabled(monkeypatch):
    monkeypatch.setenv("FUSION_TOKEN_COUNTER", "0")
    assert build_compaction_config() is None


def test_runtime_wires_token_counter(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="1")
    assert rt.compactor.config is not None
    assert rt.compactor.config.token_counter is not None
    assert "token_counter" in rt.kernel


# ---------------- sandbox policy ----------------

def test_sandbox_policy_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FUSION_SANDBOX_POLICY", raising=False)
    assert build_sandbox_policy() is None


def test_sandbox_policy_blocked_tier(monkeypatch):
    monkeypatch.setenv("FUSION_SANDBOX_POLICY", "1")
    monkeypatch.setenv("FUSION_SANDBOX_BLOCKED_TOOLS", "terminal_execute,rm_tool")
    policy = build_sandbox_policy()
    assert policy is not None
    d = policy.decide("terminal_execute", {}, (0.0, "readonly"))
    assert d.tier.name == "BLOCKED"
    ok = policy.decide("file_read", {}, (0.0, "readonly"))
    assert ok.tier.name == "PROCESS"


def test_sandbox_policy_runner_only_with_flag(monkeypatch):
    monkeypatch.setenv("FUSION_SANDBOX_POLICY", "1")
    monkeypatch.delenv("FUSION_SANDBOX_EXEC", raising=False)
    policy = build_sandbox_policy()
    assert policy is not None and policy.runner is None


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_sandbox_exec_runner_runs_command():
    runner = SandboxExecRunner()
    out = asyncio.run(
        runner.run_sandboxed("terminal_execute", {"command": "echo kernel-wired"})
    )
    assert "kernel-wired" in out


def test_sandbox_exec_runner_rejects_non_command():
    runner = SandboxExecRunner()
    with pytest.raises(ValueError):
        asyncio.run(runner.run_sandboxed("file_read", {"path": "/tmp/x"}))


def test_funnel_sandbox_gate_blocks(tmp_path, monkeypatch):
    rt = _runtime(
        tmp_path,
        monkeypatch,
        FUSION_TOKEN_COUNTER="0",
        FUSION_KERNEL_CHECKPOINTS="0",
        FUSION_SANDBOX_POLICY="1",
        FUSION_SANDBOX_BLOCKED_TOOLS="terminal_execute",
    )
    funnel = rt.tool_funnel
    assert funnel.sandbox_policy is not None
    outcome = asyncio.run(
        funnel.admit(tool_name="terminal_execute", args={"command": "ls"})
    )
    assert outcome.allowed is False
    assert outcome.blocked_by == "sandbox"


# ---------------- skill registry ----------------

def test_skill_registry_derives_rules(tmp_path, monkeypatch):
    skills = _skill_env(tmp_path, monkeypatch)
    _write_skill(
        skills,
        "shell-helper",
        frontmatter="permissions: allow_tools: file_read, file_write\n",
    )
    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0", FUSION_KERNEL_CHECKPOINTS="0")
    assert "skill_registry" in rt.kernel
    assert rt.kernel["skill_registry"].names() == ["shell-helper"]
    assert rt.kernel["derived_rules"] >= 1
    derived = [
        r
        for r in rt.permission_engine.list_rules()
        if "shell-helper" in str(r.get("description", ""))
    ]
    assert any("file_read" in str(r.get("tool", "")) for r in derived)


def test_skill_registry_harness_parse_permissions_clause(tmp_path, monkeypatch):
    skills = _skill_env(tmp_path, monkeypatch)
    _write_skill(
        skills,
        "parsed-skill",
        frontmatter="permissions: allow_tools: file_read; deny_tools: terminal_execute\n",
    )
    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0", FUSION_KERNEL_CHECKPOINTS="0")
    skill = rt.kernel["skill_registry"].get("parsed-skill")
    assert skill.grant.allow_tools == ["file_read"]
    assert skill.grant.deny_tools == ["terminal_execute"]
    deny_rules = [
        r
        for r in rt.permission_engine.list_rules()
        if r.get("action") == "deny" and "parsed-skill" in str(r.get("description", ""))
    ]
    assert deny_rules


def test_skill_registry_malformed_permissions_skipped(tmp_path, monkeypatch):
    skills = _skill_env(tmp_path, monkeypatch)
    _write_skill(skills, "bad-perms", frontmatter="permissions: bogus clause no colon\n")
    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0", FUSION_KERNEL_CHECKPOINTS="0")
    assert rt.kernel["skill_registry"].names() == []


# ---------------- kernel checkpoints ----------------

def test_kernel_checkpointer_roundtrip(tmp_path):
    cp = KernelCheckpointer(str(tmp_path / "kcp.db"))
    seq = cp.save(
        "s1", [{"role": "user", "content": "hi"}], budget_spent_tokens=42, metadata={"n": 1}
    )
    assert seq is not None
    state = cp.latest("s1")
    assert state is not None
    assert state.messages[0]["content"] == "hi"
    assert state.budget_spent_tokens == 42
    assert state.metadata["n"] == 1
    assert cp.latest("missing") is None


def test_runtime_wires_checkpointer(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0")
    assert "checkpointer" in rt.kernel
    assert rt.kernel_checkpoints is rt.kernel["checkpointer"]


def test_checkpoint_disabled_by_env(tmp_path, monkeypatch):
    rt = _runtime(
        tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0", FUSION_KERNEL_CHECKPOINTS="0"
    )
    assert "checkpointer" not in rt.kernel
    assert getattr(rt, "kernel_checkpoints", None) is None


# ---------------- orchestrator ----------------

def test_kernel_orchestrator_build(tmp_path, monkeypatch):
    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0", FUSION_KERNEL_CHECKPOINTS="0")
    orch = rt.kernel["orchestrator"]
    assert orch["runner"] is not None
    assert orch["pool"] is not None
    assert orch["team"] is not None
    assert callable(orch["agent_fn"])


def test_kernel_orchestrator_agent_fn_delegates(tmp_path, monkeypatch):
    from fusion_harness.orchestrator import SubagentSpec

    from agent_runtime.subagent import SubAgentReport

    rt = _runtime(tmp_path, monkeypatch, FUSION_TOKEN_COUNTER="0", FUSION_KERNEL_CHECKPOINTS="0")
    orch = build_kernel_orchestrator(rt)

    class _FakeStudioRunner:
        def __init__(self):
            self.seen = None

        async def run(self, spec):
            self.seen = spec
            return SubAgentReport(name=spec.name, ok=True, report="sub-done", turns=2)

    fake = _FakeStudioRunner()
    rt.subagent_runner = fake

    result = asyncio.run(
        orch["runner"].run(SubagentSpec(name="worker", prompt="do it"), orch["agent_fn"])
    )
    assert result.status == "completed"
    assert result.summary == "sub-done"
    assert fake.seen.task == "do it"
    assert fake.seen.name == "worker"


# ---------------- wire_kernel_surfaces aggregate ----------------

def test_wire_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSION_TOKEN_COUNTER", "0")
    monkeypatch.setenv("FUSION_KERNEL_CHECKPOINTS", "0")
    monkeypatch.delenv("FUSION_SANDBOX_POLICY", raising=False)
    empty = tmp_path / "no-skills"
    empty.mkdir()
    monkeypatch.setenv("FUSION_SKILLS_DIR", str(empty))

    class _Bare:
        pass

    wired = wire_kernel_surfaces(_Bare())
    assert "orchestrator" in wired
    assert "token_counter" not in wired
    assert "sandbox_policy" not in wired
    assert "checkpointer" not in wired
