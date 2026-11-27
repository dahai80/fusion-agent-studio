"""Subagent privacy routing (biz delivery pipeline).

Tests: SubAgentSpec.privacy field + to_dict, kernel_wiring metadata
forwarding (model + privacy), _resolve_model enforcement (local model
passes, non-local falls back to local default, no local model fails
closed), gateway get_model accessor.
"""

import os
import sys
from dataclasses import dataclass, field

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_runtime import kernel_wiring
from agent_runtime.llm_gateway import LLMGateway, ModelConfig
from agent_runtime.subagent import PRIVACY_LOCAL_ONLY, SubAgentRunner, SubAgentSpec


@dataclass
class _FakeCfg:
    name: str
    base_url: str


@dataclass
class _FakeSpec:
    name: str = "w1"
    prompt: str = "do"
    tool_allowlist: list = field(default_factory=list)
    permission_mode: str = ""
    max_turns: int = 8
    budget_tokens: int = 0
    metadata: dict = field(default_factory=dict)


class _FakeClient:
    def __init__(self, base_url):
        self.base_url = base_url


class _FakeGateway:
    def __init__(self, models=None, default_model="", default_base=""):
        self._models = models or {}
        self.default_model = default_model
        self._default_client = _FakeClient(default_base) if default_base else None

    def get_model(self, name):
        return self._models.get(name)

    def route(self):
        if self.default_model:
            return _FakeCfg(
                self.default_model, self._default_client.base_url if self._default_client else ""
            )
        return None


LOCAL = "http://localhost:11432/v1"
CLOUD = "https://api.cloud.example/v1"


def _runner(gateway):
    class RT:
        llm_gateway = gateway

    return SubAgentRunner.__new__(SubAgentRunner), RT()


class TestSpecField:
    def test_default_empty(self):
        assert SubAgentSpec(name="w", task="t").privacy == ""

    def test_to_dict_carries_privacy(self):
        d = SubAgentSpec(name="w", task="t", privacy=PRIVACY_LOCAL_ONLY).to_dict()
        assert d["privacy"] == PRIVACY_LOCAL_ONLY


class TestKernelWiringForwarding:
    def test_metadata_model_and_privacy_forwarded(self):
        """agent_fn must carry spec.metadata model/privacy into the studio spec."""
        import inspect

        src = inspect.getsource(kernel_wiring.make_studio_agent_fn)
        assert 'metadata or {}).get("model"' in src
        assert 'metadata or {}).get("privacy"' in src


class TestEnforcePrivacy:
    def test_no_constraint_passthrough(self):
        runner, rt = _runner(_FakeGateway())
        spec = SubAgentSpec(name="w", task="t")
        assert runner._enforce_privacy(spec, "cloud-big") == "cloud-big"

    def test_local_model_passes(self, monkeypatch):
        import server.fusion_mlx_client as fmc

        monkeypatch.setattr(fmc, "default_base_url", lambda: LOCAL)
        runner, rt = _runner(
            _FakeGateway(
                models={"loc-1": _FakeCfg("loc-1", LOCAL)},
                default_model="loc-1",
                default_base=LOCAL,
            )
        )
        runner.parent_runtime = rt
        spec = SubAgentSpec(name="w", task="t", privacy=PRIVACY_LOCAL_ONLY)
        assert runner._enforce_privacy(spec, "loc-1") == "loc-1"

    def test_non_local_falls_back_to_local_default(self, monkeypatch):
        import server.fusion_mlx_client as fmc

        monkeypatch.setattr(fmc, "default_base_url", lambda: LOCAL)
        runner, rt = _runner(
            _FakeGateway(
                models={"cloud-1": _FakeCfg("cloud-1", CLOUD)},
                default_model="loc-1",
                default_base=LOCAL,
            )
        )
        runner.parent_runtime = rt
        spec = SubAgentSpec(name="w", task="t", privacy=PRIVACY_LOCAL_ONLY)
        assert runner._enforce_privacy(spec, "cloud-1") == "loc-1"

    def test_no_local_model_fails_closed(self, monkeypatch):
        import server.fusion_mlx_client as fmc

        monkeypatch.setattr(fmc, "default_base_url", lambda: LOCAL)
        runner, rt = _runner(
            _FakeGateway(
                models={"cloud-1": _FakeCfg("cloud-1", CLOUD)},
                default_model="cloud-2",
                default_base=CLOUD,
            )
        )
        runner.parent_runtime = rt
        spec = SubAgentSpec(name="w", task="t", privacy=PRIVACY_LOCAL_ONLY)
        with pytest.raises(ValueError):
            runner._enforce_privacy(spec, "cloud-1")

    def test_unknown_model_base_fails_closed(self, monkeypatch):
        import server.fusion_mlx_client as fmc

        monkeypatch.setattr(fmc, "default_base_url", lambda: LOCAL)
        runner, rt = _runner(_FakeGateway(default_model="", default_base=""))
        runner.parent_runtime = rt
        spec = SubAgentSpec(name="w", task="t", privacy=PRIVACY_LOCAL_ONLY)
        with pytest.raises(ValueError):
            runner._enforce_privacy(spec, "mystery")


class TestGatewayGetModel:
    def test_get_model_hit_and_miss(self):
        gw = LLMGateway()
        cfg = ModelConfig(
            name="m1",
            base_url=LOCAL,
            api_key="",
            priority=0,
            capabilities=set(),
            context_length=8192,
        )
        gw.register_model(cfg)
        assert gw.get_model("m1") is cfg
        assert gw.get_model("nope") is None
