# Issue #775: adopt the P-series compaction knobs (fusion-harness 0.26.0).
#
# - CompactionConfig.trigger_ratio (kernel #122): threshold-triggered
#   compaction — the AS Compactor.should_compact choke point consults
#   should_auto_compact and forces a "warning" pass when the estimate
#   crosses ratio*window even when the heuristic levels say none.
#   Default off (None) keeps the legacy consumer-invoked behavior.
# - Post-compaction validator (kernel #131): build_compaction_validator
#   checks the compacted state still carries the system prompt and task
#   checklist markers; the kernel logs the verdict and emits
#   compaction_validated — observational, never gates.
# - build_compaction_config ships both behind env: default trigger 0.85,
#   FUSION_COMPACTION_TRIGGER_RATIO=0 disables; FUSION_COMPACTION_VALIDATOR=0
#   disables the validator.
from __future__ import annotations

import pytest
from fusion_harness.compactor import CompactionConfig

from agent_runtime.compactor import Compactor
from agent_runtime.kernel_wiring import (
    build_compaction_config,
    build_compaction_validator,
)


def _msgs_with_tokens(approx_tokens: int) -> list[dict]:
    # HeuristicTokenCounter ~ 4 chars per token; one big user message.
    return [{"role": "user", "content": "x" * (approx_tokens * 4)}]


def msgs_trigger_window() -> list[dict]:
    # Alternating rounds sized so the estimate lands between trigger 8500
    # and warning 9500 (context_window=10000): ~8 chars/token heuristic.
    msgs = [{"role": "system", "content": "sys prompt"}]
    for i in range(12):
        msgs.append({"role": "user", "content": f"round {i}: " + "x" * 1450})
        msgs.append({"role": "assistant", "content": f"reply {i}: " + "y" * 1450})
    msgs.append({"role": "user", "content": "latest"})
    return msgs


@pytest.fixture
def _clean_env(monkeypatch):
    monkeypatch.delenv("FUSION_TOKEN_COUNTER", raising=False)
    monkeypatch.delenv("FUSION_COMPACTION_TRIGGER_RATIO", raising=False)
    monkeypatch.delenv("FUSION_COMPACTION_VALIDATOR", raising=False)
    yield


class TestTriggerRatioChokePoint:
    def _compactor(self, **cfg) -> Compactor:
        return Compactor(config=CompactionConfig(**cfg))

    def test_trigger_forces_warning_between_thresholds(self):
        # context_window=10000, trigger 0.85 -> 8500; warning threshold
        # 10000-500=9500. Estimate 8800 sits between: heuristic says none,
        # threshold policy says compact.
        c = self._compactor(
            context_window=10000,
            warning_buffer=500,
            error_buffer=400,
            manual_buffer=100,
            trigger_ratio=0.85,
        )
        msgs = _msgs_with_tokens(8800)
        assert super(Compactor, c).should_compact(msgs) == "none"
        assert c.should_compact(msgs) == "warning"

    def test_no_trigger_keeps_heuristic_only(self):
        c = self._compactor(
            context_window=10000,
            warning_buffer=500,
            error_buffer=400,
            manual_buffer=100,
        )
        msgs = _msgs_with_tokens(8800)
        assert c.should_compact(msgs) == "none"

    def test_heuristic_levels_still_win_when_higher(self):
        c = self._compactor(
            context_window=10000,
            warning_buffer=500,
            error_buffer=400,
            manual_buffer=100,
            trigger_ratio=0.85,
        )
        msgs = _msgs_with_tokens(9800)
        # estimate 9800 >= error threshold 9600 -> heuristic "error" wins.
        assert c.should_compact(msgs) == "error"

    def test_trigger_threshold_math(self):
        cfg = CompactionConfig(context_window=32768, trigger_ratio=0.85)
        assert cfg.trigger_threshold() == 27852
        assert cfg.should_auto_compact(27852)
        assert not cfg.should_auto_compact(27851)

    def test_invalid_ratio_rejected(self):
        with pytest.raises(ValueError):
            CompactionConfig(trigger_ratio=1.5)

    def test_end_to_end_funnel_compacts_on_threshold(self):
        import asyncio

        from fusion_harness.funnels import LLMFunnel

        c = self._compactor(
            context_window=10000,
            warning_buffer=500,
            error_buffer=400,
            manual_buffer=100,
            trigger_ratio=0.85,
        )
        funnel = LLMFunnel(compactor=c)
        msgs = [{"role": "system", "content": "sys prompt"}]
        for i in range(12):
            msgs.append({"role": "user", "content": f"round {i}: " + "x" * 1650})
            msgs.append({"role": "assistant", "content": f"reply {i}: " + "y" * 1650})
        msgs.append({"role": "user", "content": "latest"})
        # Heuristic alone: warning level reached (kernel warning threshold
        # 9500). Funnel drives compaction -> round eviction shrinks count.
        assert super(Compactor, c).should_compact(msgs) != "none"
        proceed, reason, out = asyncio.run(funnel.pre_llm(None, "n", "m", msgs))
        assert proceed
        assert len(out) < len(msgs)

    def test_funnel_forces_pass_in_trigger_window(self):
        # Estimate between trigger (8500) and warning (9500): heuristic says
        # none, the #775 choke point forces level="warning", and the kernel
        # compaction pass runs (compact_before reaches the lifecycle bus).
        import asyncio

        from fusion_harness.funnels import LifecycleBus, LLMFunnel

        c = self._compactor(
            context_window=10000,
            warning_buffer=500,
            error_buffer=400,
            manual_buffer=100,
            trigger_ratio=0.85,
        )
        assert super(Compactor, c).should_compact(msgs_trigger_window()) == "none"
        assert c.should_compact(msgs_trigger_window()) == "warning"
        bus = LifecycleBus()
        seen = []
        bus.subscribe(lambda e: seen.append(e))
        funnel = LLMFunnel(compactor=c, lifecycle_bus=bus)
        proceed, _, out = asyncio.run(
            funnel.pre_llm(None, "n", "m", msgs_trigger_window())
        )
        assert proceed
        events = [getattr(e, "event", "") for e in seen]
        assert "compact_before" in events


class TestCompactionValidator:
    def test_system_lost_fails(self):
        v = build_compaction_validator()
        ok, verdict = v([{"role": "user", "content": "Task: ship"}], 100)
        assert not ok and "system" in verdict

    def test_empty_list_fails(self):
        v = build_compaction_validator()
        ok, verdict = v([], 100)
        assert not ok

    def test_checklist_marker_passes(self):
        v = build_compaction_validator()
        ok, verdict = v(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "summary with Active tasks: a; b"},
            ],
            100,
        )
        assert ok and "Active tasks:" in verdict

    def test_no_marker_informational_pass(self):
        v = build_compaction_validator()
        ok, verdict = v([{"role": "system", "content": "sys"}], 100)
        assert ok and "informational" in verdict

    def test_kernel_runs_validator_and_emits_event(self):

        from fusion_harness.funnels import LifecycleBus

        c = Compactor(
            config=CompactionConfig(validator=build_compaction_validator()),
            lifecycle_bus=LifecycleBus(),
        )
        seen = []
        c.lifecycle_bus.subscribe(lambda e: seen.append(e))
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Task: x"}]
        c.compact(msgs, "warning")
        events = [e for e in seen if getattr(e, "event", "") == "compaction_validated"]
        assert events, "compaction_validated must reach the lifecycle bus"
        payload = getattr(events[0], "payload", {})
        assert payload.get("ok") is True

    def test_validator_failure_is_observational(self):
        import asyncio

        c = Compactor(
            config=CompactionConfig(validator=lambda m, t: (_ for _ in ()).throw(RuntimeError("boom")))
        )
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Task: x"}]
        out = asyncio.run(c.compact_async(msgs, "warning"))
        assert isinstance(out, list) and out


class TestConfigSurface:
    def test_default_ships_both_knobs(self, _clean_env):
        cfg = build_compaction_config()
        assert cfg is not None
        assert cfg.trigger_ratio == 0.85
        assert callable(cfg.validator)
        assert cfg.token_counter is None

    def test_trigger_ratio_disable(self, _clean_env, monkeypatch):
        monkeypatch.setenv("FUSION_COMPACTION_TRIGGER_RATIO", "0")
        cfg = build_compaction_config()
        assert cfg.trigger_ratio is None
        assert callable(cfg.validator)

    def test_trigger_ratio_invalid_value_disables(self, _clean_env, monkeypatch):
        monkeypatch.setenv("FUSION_COMPACTION_TRIGGER_RATIO", "bogus")
        cfg = build_compaction_config()
        assert cfg.trigger_ratio is None

    def test_validator_disable(self, _clean_env, monkeypatch):
        monkeypatch.setenv("FUSION_COMPACTION_VALIDATOR", "0")
        cfg = build_compaction_config()
        assert cfg.validator is None
        assert cfg.trigger_ratio == 0.85

    def test_all_knobs_off_returns_none(self, _clean_env, monkeypatch):
        monkeypatch.setenv("FUSION_COMPACTION_TRIGGER_RATIO", "0")
        monkeypatch.setenv("FUSION_COMPACTION_VALIDATOR", "0")
        assert build_compaction_config() is None

    def test_token_counter_still_wires(self, _clean_env, monkeypatch):
        monkeypatch.setenv("FUSION_TOKEN_COUNTER", "1")
        cfg = build_compaction_config()
        assert cfg.token_counter is not None

    def test_wire_kernel_surfaces_attaches_config(self, _clean_env):
        from agent_runtime.kernel_wiring import wire_kernel_surfaces

        class _Rt:
            compactor = Compactor()
            tool_funnel = None

        wired = wire_kernel_surfaces(_Rt())
        assert wired.get("compaction") is True
        assert _Rt.compactor.config.trigger_ratio == 0.85
