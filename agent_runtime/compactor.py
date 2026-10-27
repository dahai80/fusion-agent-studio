"""N0 context compactor — canonical kernel re-export (fusion-harness, #357/#2)."""

import asyncio
import logging

import fusion_harness.compactor as _fh_compactor
from fusion_harness.compactor import *  # noqa: F401,F403

logger = logging.getLogger(__name__)

_HarnessCompactor = _fh_compactor.Compactor


class Compactor(_HarnessCompactor):
    # #478: harness kernel fires COMPACT_BEFORE as a hook but only pushes
    # compact_after onto the lifecycle bus — the COMPACT_AFTER HookEvent
    # never fired for hook subscribers. Subclass fires it after compaction
    # settles (success and failure alike), mirroring the upstream
    # _fire_compact_before pattern (running-loop guard + ensure_future).
    def compact(self, messages, level="warning", summarize=None):
        try:
            msgs = super().compact(messages, level=level, summarize=summarize)
        except Exception as exc:
            self._fire_compact_after(
                {
                    "level": level,
                    "before_msgs": len(messages),
                    "success": False,
                    "error": str(exc),
                }
            )
            raise
        self._fire_compact_after(
            {
                "level": level,
                "before_msgs": len(messages),
                "after_msgs": len(msgs),
                "success": True,
            }
        )
        return msgs

    async def compact_async(self, messages, level="warning"):
        try:
            msgs = await super().compact_async(messages, level=level)
        except Exception as exc:
            self._fire_compact_after(
                {
                    "level": level,
                    "before_msgs": len(messages),
                    "success": False,
                    "error": str(exc),
                }
            )
            raise
        self._fire_compact_after(
            {
                "level": level,
                "before_msgs": len(messages),
                "after_msgs": len(msgs),
                "success": True,
            }
        )
        return msgs

    def _fire_compact_after(self, payload: dict) -> None:
        if self.hooks is None:
            return
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("COMPACT_AFTER skipped: no running event loop")
                return
            asyncio.ensure_future(self.hooks.fire("COMPACT_AFTER", payload))
        except Exception as exc:
            logger.warning("COMPACT_AFTER hook dispatch failed: %s", exc)
