# Issue #790: wire fusion-harness#151 select_sandbox_runner into
# build_sandbox_policy. FUSION_SANDBOX_BACKEND=fusion_executor opts into the
# fusion-executor adapter with deterministic fallback; the unset default must
# stay byte-identical to the hand-built SandboxExecRunner wiring.
from __future__ import annotations

import sys

import pytest
from fusion_harness.executor_backend import ExecutorBackendUnavailable

from agent_runtime.kernel_wiring import SandboxExecRunner, build_sandbox_policy


@pytest.fixture
def _policy_on(monkeypatch):
    monkeypatch.setenv("FUSION_SANDBOX_POLICY", "1")


class _StubAdapter:
    pass


class TestDefaultPathUnchanged:
    def test_no_exec_flag_runner_none(self, _policy_on, monkeypatch):
        monkeypatch.delenv("FUSION_SANDBOX_EXEC", raising=False)
        monkeypatch.delenv("FUSION_SANDBOX_BACKEND", raising=False)
        policy = build_sandbox_policy()
        assert policy is not None and policy.runner is None

    @pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
    def test_exec_flag_wires_hand_built_runner(self, _policy_on, monkeypatch):
        monkeypatch.setenv("FUSION_SANDBOX_EXEC", "1")
        monkeypatch.delenv("FUSION_SANDBOX_BACKEND", raising=False)
        policy = build_sandbox_policy()
        assert isinstance(policy.runner, SandboxExecRunner)


class TestFusionExecutorBackend:
    def test_adapter_wired_when_constructible(self, _policy_on, monkeypatch):
        import fusion_harness.executor_backend as eb

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setattr(eb, "FusionExecutorBackend", lambda **kw: _StubAdapter(), raising=True)
        policy = build_sandbox_policy()
        assert isinstance(policy.runner, _StubAdapter)

    def test_construction_failure_falls_back_to_default(self, _policy_on, monkeypatch):
        import fusion_harness.executor_backend as eb

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setenv("FUSION_SANDBOX_EXEC", "1")

        def _boom(**kw):
            raise ExecutorBackendUnavailable("fusion-executor binary missing")

        monkeypatch.setattr(eb, "FusionExecutorBackend", _boom, raising=True)
        policy = build_sandbox_policy()
        # fallback = the hand-built SandboxExecRunner (darwin) — not a crash
        assert isinstance(policy.runner, SandboxExecRunner)

    def test_fallback_without_exec_flag_is_none(self, _policy_on, monkeypatch):
        import fusion_harness.executor_backend as eb

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.delenv("FUSION_SANDBOX_EXEC", raising=False)
        monkeypatch.setattr(
            eb,
            "FusionExecutorBackend",
            lambda **kw: (_ for _ in ()).throw(ExecutorBackendUnavailable("x")),
            raising=True,
        )
        policy = build_sandbox_policy()
        assert policy.runner is None

    def test_backend_named_in_log(self, _policy_on, monkeypatch, caplog):
        import fusion_harness.executor_backend as eb

        monkeypatch.setenv("FUSION_SANDBOX_BACKEND", "fusion_executor")
        monkeypatch.setattr(eb, "FusionExecutorBackend", lambda **kw: _StubAdapter(), raising=True)
        with caplog.at_level("INFO", logger="agent_runtime.kernel_wiring"):
            build_sandbox_policy()
        assert "backend=fusion_executor" in caplog.text
