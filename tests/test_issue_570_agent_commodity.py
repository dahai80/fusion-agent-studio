"""#570: agent commodity standard — capability/model separation, dual SKU,
docs five-pack release gate, marketplace listing validation.

ACs:
1. Commodity schema: capability fields vs replaceable model bindings.
2. Dual SKU: one reviewer agent, local + pro bindings, both executable.
3. Docs five-pack validation rejects missing items.
4. Marketplace publish rejects listing missing any five-pack item.
5. Eval report generated from eval infrastructure and attached.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime.agent_commodity import (
    DOC_FIVE_PACK,
    AgentCommodity,
    ModelBinding,
    all_reviewer_commodities,
    build_commodity_from_reviewer_pack,
    docs_pack_complete,
    eval_report_markdown,
    validate_docs_pack,
)
from agent_runtime.dispatchers.commodity import CommodityDispatcher
from agent_runtime.dispatchers.marketplace import MarketplaceDispatcher
from agent_runtime.llm_gateway import LLMGateway
from agent_runtime.reviewer_packs import REVIEWER_PACKS
from server.fusion_mlx_client import LLMResponse


class _Daemon:
    def __init__(self):
        self.entries = {}

    def _get_marketplace(self):
        from agent_runtime.agent_marketplace import AgentMarketplace

        if not hasattr(self, "_mp"):
            self._mp = AgentMarketplace(store_dir=self._tmpdir())
        return self._mp

    def _tmpdir(self):
        import tempfile

        if not hasattr(self, "_dir"):
            self._dir = tempfile.mkdtemp(prefix="c570-")
        return self._dir

    def _get_llm_gateway(self):
        return self._gw

    def _get_handler(self, name):
        return None


def _binding(sku: str, model: str) -> ModelBinding:
    return ModelBinding(sku=sku, provider="mlx", model_name=model)


def _full_commodity() -> AgentCommodity:
    c = build_commodity_from_reviewer_pack("architecture-contract")
    assert c is not None
    c.model_bindings = {
        "local": _binding("local", "mlx-community/Qwen3.5-4B-MLX-4bit"),
        "pro": _binding("pro", "glm-5"),
    }
    return c


def test_five_pack_missing_items_flagged():
    missing = validate_docs_pack({"readme": "x"})
    flagged = {m.split(": ")[1].split(" ")[0] for m in missing}
    assert flagged == {k for k in DOC_FIVE_PACK if k != "readme"}
    assert docs_pack_complete({k: "docs/x" for k in DOC_FIVE_PACK})
    assert not docs_pack_complete({"readme": "x"})


def test_schema_capability_vs_model_separation():
    c = _full_commodity()
    errors = c.validate()
    assert errors == []
    d = c.to_dict()
    assert "model_bindings" in d and "capability" in d
    assert "model" not in d["capability"]
    # swapping a binding keeps capability fingerprint identical
    fp1 = c.capability_fingerprint()
    c2 = c.with_model_binding("pro", _binding("pro", "glm-5-air"))
    assert c2.capability_fingerprint() == fp1
    assert set(c2.skus()) == {"local", "pro"}


def test_schema_validation_errors():
    c = AgentCommodity(name="x", version="1.0.0")
    errors = c.validate()
    assert any("system_prompt" in e for e in errors)
    assert any("tool_allowlist" in e for e in errors)
    assert any("eval_set_ref" in e for e in errors)
    assert any("model_bindings" in e for e in errors)


def test_reviewer_family_builds_commodities():
    commodities = all_reviewer_commodities()
    assert len(commodities) == len(REVIEWER_PACKS) >= 4
    ids = {c.agent_id for c in commodities}
    assert "reviewer-architecture-contract" in ids
    for c in commodities:
        assert c.system_prompt
        assert c.eval_set_ref
        assert set(c.docs) == set(DOC_FIVE_PACK)


def test_validate_rpc():
    d = CommodityDispatcher(_Daemon())
    out = asyncio.run(d._handle_validate({"commodity": _full_commodity().to_dict()}))
    assert out["valid"] and out["errors"] == []
    broken = _full_commodity()
    broken.docs.pop("eval_report")
    out2 = asyncio.run(d._handle_validate({"commodity": broken.to_dict()}))
    assert not out2["valid"]
    assert any("eval_report" in e for e in out2["errors"])


def test_from_reviewer_pack_rpc():
    d = CommodityDispatcher(_Daemon())
    out = asyncio.run(d._handle_from_reviewer_pack({"pack_key": "security"}))
    assert out["status"] == "ok" and out["commodity"]["agent_id"] == "reviewer-security"
    out2 = asyncio.run(d._handle_from_reviewer_pack({"pack_key": "nope"}))
    assert out2["status"] == "error"


def test_publish_rejects_missing_five_pack_item():
    d = CommodityDispatcher(_Daemon())
    broken = _full_commodity()
    broken.docs.pop("quickstart")
    out = asyncio.run(d._handle_publish({"commodity": broken.to_dict()}))
    assert out["status"] == "error"
    assert any("quickstart" in e for e in out["errors"])


def test_publish_rejects_via_marketplace_publish_path():
    d = _Daemon()
    md = MarketplaceDispatcher(d)
    commodity = _full_commodity()
    commodity.docs.pop("changelog")
    out = asyncio.run(
        md._handle_marketplace_publish(
            {
                "name": commodity.name,
                "graph_data": {"commodity": commodity.to_dict()},
            }
        )
    )
    assert out["status"] == "error"
    assert any("changelog" in e for e in out["errors"])


def test_publish_accepts_complete_commodity():
    d = _Daemon()
    out = asyncio.run(
        CommodityDispatcher(d)._handle_publish(
            {
                "commodity": _full_commodity().to_dict(),
                "eval_summary": {
                    "sku": "local",
                    "model_name": "mlx-community/Qwen3.5-4B-MLX-4bit",
                    "pass_rate": 100.0,
                    "passed": 3,
                    "total": 3,
                    "eval_set_ref": "eval-sets/reviewer-architecture-contract.json",
                    "raw": "dropped",
                },
            }
        )
    )
    assert out["status"] == "ok"
    assert out["skus"] == ["local", "pro"]
    entry = MarketplaceDispatcher(d)._handle_marketplace_get({"entry_id": out["entry_id"]})
    entry = asyncio.run(entry)
    assert entry["entry"]["category"] == "agent-commodity"
    assert entry["entry"]["graph_data"]["capability_fingerprint"]
    es = entry["entry"]["graph_data"]["eval_summary"]
    assert es["pass_rate"] == 100.0 and "raw" not in es


class _StubGateway(LLMGateway):
    def __init__(self, content: str):
        super().__init__()
        self._content = content
        self.models_seen = []

    async def chat(self, messages, model="", capability="", tools=None, **kwargs):
        self.models_seen.append(model)
        return LLMResponse(
            content=self._content,
            tool_calls=[],
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            finish_reason="stop",
        )


def _finding_reply(pack: str) -> str:
    import json

    return json.dumps(
        {
            "findings": [
                {
                    "severity": "P1",
                    "location": "a.py:10",
                    "rationale": "module reaches past seam",
                    "recommendation": "extract interface",
                    "pack": pack,
                }
            ]
        }
    )


def test_dual_sku_both_executable_eval_report():
    commodity = _full_commodity()
    for sku, model in (
        ("local", "mlx-community/Qwen3.5-4B-MLX-4bit"),
        ("pro", "glm-5"),
    ):
        d = _Daemon()
        gw = _StubGateway(_finding_reply("architecture"))
        d._gw = gw
        out = asyncio.run(
            CommodityDispatcher(d)._handle_eval_report(
                {
                    "commodity": commodity.to_dict(),
                    "sku": sku,
                    "eval_set": [
                        {
                            "case_id": "c1",
                            "artifact": "def leak(): reach past seam",
                            "request": "review",
                            "min_findings": 1,
                        }
                    ],
                }
            )
        )
        assert out["status"] == "ok", out
        assert gw.models_seen == [model], f"sku {sku} must bind its own model"
        report = out["report"]
        assert report["sku"] == sku and report["model_name"] == model
        assert report["passed"] == 1 and report["pass_rate"] == 100.0
        assert "## Known limitations" in out["markdown"]
        assert eval_report_markdown(report).startswith("# Eval Report")


def test_eval_report_unknown_sku_rejected():
    d = _Daemon()
    d._gw = _StubGateway("{}")
    out = asyncio.run(
        CommodityDispatcher(d)._handle_eval_report(
            {
                "commodity": _full_commodity().to_dict(),
                "sku": "quantum",
                "eval_set": [{"case_id": "c1", "artifact": "x"}],
            }
        )
    )
    assert out["status"] == "error" and "quantum" in out["message"]


def test_handler_registered():
    handlers = CommodityDispatcher(None).get_handlers()
    for name in (
        "commodity.validate",
        "commodity.from_reviewer_pack",
        "commodity.publish",
        "commodity.eval_report",
    ):
        assert name in handlers
        assert asyncio.iscoroutinefunction(handlers[name])


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
