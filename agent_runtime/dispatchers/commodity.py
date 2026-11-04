"""Sub-dispatcher: CommodityDispatcher — agent commodity standard (#570)."""

from __future__ import annotations

import logging
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class CommodityDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "commodity.validate": self._handle_validate,
            "commodity.from_reviewer_pack": self._handle_from_reviewer_pack,
            "commodity.publish": self._handle_publish,
            "commodity.eval_report": self._handle_eval_report,
        }

    async def _handle_validate(self, params: dict) -> dict:
        from ..agent_commodity import AgentCommodity

        raw = params.get("commodity")
        if not isinstance(raw, dict):
            return {"status": "error", "message": "commodity object required"}
        errors = AgentCommodity.from_dict(raw).validate()
        return {
            "status": "ok" if not errors else "invalid",
            "errors": errors,
            "valid": not errors,
        }

    async def _handle_from_reviewer_pack(self, params: dict) -> dict:
        from ..agent_commodity import build_commodity_from_reviewer_pack

        key = str(params.get("pack_key", "") or "")
        commodity = build_commodity_from_reviewer_pack(key)
        if commodity is None:
            return {"status": "error", "message": f"unknown reviewer pack: {key}"}
        return {"status": "ok", "commodity": commodity.to_dict()}

    async def _handle_publish(self, params: dict) -> dict:
        from ..agent_commodity import AgentCommodity, docs_pack_complete

        raw = params.get("commodity")
        if not isinstance(raw, dict):
            return {"status": "error", "message": "commodity object required"}
        commodity = AgentCommodity.from_dict(raw)
        errors = commodity.validate()
        if errors:
            logger.warning("commodity.publish rejected: %d errors", len(errors))
            return {
                "status": "error",
                "message": "commodity listing failed validation",
                "errors": errors,
            }
        mp = self._daemon._get_marketplace()
        from ..agent_marketplace import MarketEntry

        entry = MarketEntry(
            name=commodity.name,
            author=commodity.author or params.get("author", ""),
            description=commodity.description,
            category="agent-commodity",
            tags=commodity.tags,
            version=commodity.version,
            graph_data={
                "commodity": commodity.to_dict(),
                "capability_fingerprint": commodity.capability_fingerprint(),
            },
            visibility=str(params.get("visibility", "public") or "public"),
        )
        entry_id = mp.publish(entry, tenant=str(params.get("tenant", "default") or "default"))
        logger.info(
            "commodity.publish: id=%s agent=%s skus=%s",
            entry_id,
            commodity.agent_id,
            ",".join(commodity.skus()),
        )
        return {
            "status": "ok",
            "entry_id": entry_id,
            "agent_id": commodity.agent_id,
            "skus": commodity.skus(),
            "capability_fingerprint": commodity.capability_fingerprint(),
            "docs_pack_complete": docs_pack_complete(commodity.docs),
        }

    async def _handle_eval_report(self, params: dict) -> dict:
        from ..agent_commodity import AgentCommodity, eval_report_markdown

        raw = params.get("commodity")
        if not isinstance(raw, dict):
            return {"status": "error", "message": "commodity object required"}
        commodity = AgentCommodity.from_dict(raw)
        eval_set = params.get("eval_set")
        if not isinstance(eval_set, list) or not eval_set:
            return {"status": "error", "message": "eval_set (non-empty list) required"}
        sku = str(params.get("sku", "") or "")
        binding = commodity.model_bindings.get(sku)
        if binding is None:
            return {
                "status": "error",
                "message": f"unknown sku '{sku}' — available: {', '.join(commodity.skus())}",
            }
        from ..reviewer_packs import ReviewerPackRunner

        daemon = self._daemon

        async def _invoke(messages: list[dict], model: str = "") -> str:
            gateway = daemon._get_llm_gateway()
            resp = await gateway.chat(messages=messages, model=binding.model_name)
            content = getattr(resp, "content", None)
            if content is None and isinstance(resp, dict):
                choices = resp.get("choices") or []
                content = (choices[0].get("message") or {}).get("content", "") if choices else ""
            return content or ""

        runner = ReviewerPackRunner(_invoke)
        cases = []
        passed = 0
        for case in eval_set:
            case_id = str(case.get("case_id", "") or "")
            artifact = str(case.get("artifact", "") or "")
            min_findings = int(case.get("min_findings", 1) or 1)
            try:
                merged = await runner.run(
                    pack_keys=[commodity.agent_id.replace("reviewer-", "")],
                    artifact=artifact,
                    original_request=str(case.get("request", "") or ""),
                )
            except ValueError as exc:
                cases.append(
                    {
                        "case_id": case_id,
                        "expect": f">= {min_findings} findings",
                        "got": f"error: {exc}",
                        "passed": False,
                    }
                )
                continue
            findings = merged.get("findings") or []
            got = len(findings)
            ok = got >= min_findings
            if ok:
                passed += 1
            cases.append(
                {
                    "case_id": case_id,
                    "expect": f">= {min_findings} findings",
                    "got": f"{got} findings",
                    "passed": ok,
                }
            )
        report = {
            "agent_id": commodity.agent_id,
            "version": commodity.version,
            "sku": sku,
            "model_name": binding.model_name,
            "eval_set_ref": commodity.eval_set_ref,
            "total": len(cases),
            "passed": passed,
            "pass_rate": round(passed * 100 / len(cases), 1) if cases else 0.0,
            "cases": cases,
            "known_limitations": [
                "Findings quality depends on the bound model; local small models "
                "may miss subtle violations (see case-level results).",
            ],
        }
        logger.info(
            "commodity.eval_report: agent=%s sku=%s pass_rate=%s",
            commodity.agent_id,
            sku,
            report["pass_rate"],
        )
        return {
            "status": "ok",
            "report": report,
            "markdown": eval_report_markdown(report),
        }
