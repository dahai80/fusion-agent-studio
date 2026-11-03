"""Sub-dispatcher: ReviewerDispatcher — reviewer packs (#549)."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable

from .base import SubDispatcher

logger = logging.getLogger(__name__)


class ReviewerDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "reviewer.packs": self._handle_packs,
            "reviewer.schema": self._handle_schema,
            "reviewer.seed": self._handle_seed,
            "reviewer.run": self._handle_run,
        }

    def _llm_fn(self):
        daemon = self._daemon

        async def _invoke(messages: list[dict], model: str = "") -> str:
            gateway = daemon._get_llm_gateway()
            response = await gateway.chat(messages=messages, model=model or "")
            content = getattr(response, "content", None)
            if content is None and isinstance(response, dict):
                choices = response.get("choices") or []
                content = (choices[0].get("message") or {}).get("content", "") if choices else ""
            return content or ""

        return _invoke

    def _runner(self):
        from ..reviewer_packs import ReviewerPackRunner

        cached = getattr(self._daemon, "_reviewer_pack_runner", None)
        if cached is None:
            cached = ReviewerPackRunner(self._llm_fn())
            self._daemon._reviewer_pack_runner = cached
        return cached

    async def _handle_packs(self, params: dict) -> dict:
        from ..reviewer_packs import list_packs

        return {"status": "ok", "packs": list_packs(), "count": len(list_packs())}

    async def _handle_schema(self, params: dict) -> dict:
        from ..reviewer_packs import REVIEW_FINDINGS_SCHEMA, SEVERITIES

        return {"status": "ok", "schema": REVIEW_FINDINGS_SCHEMA, "severities": list(SEVERITIES)}

    async def _handle_seed(self, params: dict) -> dict:
        from ..reviewer_packs import write_bundle_dirs

        base = str(params.get("base_dir", "") or "").strip()
        target = Path(base) if base else Path(tempfile.mkdtemp(prefix="reviewer-packs-"))
        dirs = write_bundle_dirs(target)
        results = []
        installer = self._daemon._get_handler("bundle.install")
        for d in dirs:
            if installer is None:
                results.append({"name": d.name, "path": str(d), "status": "written"})
                continue
            res = await installer({"path": str(d)})
            results.append(
                {
                    "name": d.name,
                    "path": str(d),
                    "status": res.get("status", "error"),
                    "message": res.get("message", ""),
                }
            )
        logger.info("reviewer.seed: %d bundles processed into %s", len(dirs), target)
        return {"status": "ok", "base": str(target), "bundles": results, "count": len(dirs)}

    async def _handle_run(self, params: dict) -> dict:
        artifact = str(params.get("artifact", "") or "")
        if not artifact.strip():
            return {"status": "error", "message": "artifact required"}
        original_request = str(params.get("request", "") or "")
        pack_keys = params.get("packs") or []
        if isinstance(pack_keys, str):
            pack_keys = [k.strip() for k in pack_keys.split(",") if k.strip()]
        blind = params.get("blind", True)
        params_by_pack = params.get("params_by_pack") or {}
        try:
            merged = await self._runner().run(
                pack_keys=list(pack_keys),
                artifact=artifact,
                original_request=original_request,
                author_notes=str(params.get("author_notes", "") or ""),
                blind=bool(blind),
                params_by_pack=params_by_pack,
            )
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        return {"status": "ok", **merged}
