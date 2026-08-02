"""Sub-dispatcher base class for DaemonServer RPC decomposition."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable
import logging

logger = logging.getLogger(__name__)


class SubDispatcher(ABC):
    def __init__(self, daemon: Any):
        self._daemon = daemon

    @abstractmethod
    def get_handlers(self) -> dict[str, Callable]:
        pass

    @staticmethod
    def _ok(result: Any) -> dict:
        return result

    @staticmethod
    def _err(msg: str) -> dict:
        return {"error": msg}
