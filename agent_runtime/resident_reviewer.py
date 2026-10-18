"""N14 resident reviewer (#353): verify node upgraded to a standing reviewer.

When a verify node is declared `resident: true`, the runtime attaches a
ResidentReviewer to the execution event stream. It deterministically flags:

- graph_mismatch: a node transition (by label) that has no declared edge, or
  a node_id that does not resolve to any node in the graph
- untraceable_conclusion: THINK events containing conclusion markers whose
  content shares no distinctive token with accumulated tool/RAG evidence

No LLM calls — Rule 5: decisions are deterministic, not model-routed.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CONCLUSION_MARKERS = re.compile(
    r"(因此|所以|综上|总之|结论|conclusion|in summary|to summarize|therefore)", re.IGNORECASE
)
TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿]{4,}")
EVIDENCE_CAP = 5000
DISTINCTIVE_LEN = 4
RUNTIME_REVIEW_ENV = "FUSION_RUNTIME_REVIEW"


@dataclass
class ReviewFinding:
    kind: str
    node_id: str
    detail: str
    severity: str = "warning"
    ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "node_id": self.node_id,
            "detail": self.detail,
            "severity": self.severity,
            "ts": self.ts,
        }


def _distinct_tokens(text: str) -> set[str]:
    text = text.lower()
    return {t for t in TOKEN_RE.findall(text) if len(t) >= DISTINCTIVE_LEN}


class ResidentReviewer:
    def __init__(self, graph: Any, level: str = "resident"):
        self.graph = graph
        self.level = level
        self._edges: set[tuple[str, str]] = set()
        self._known_ids: set[str] = set()
        nodes = getattr(graph, "nodes", None) or {}
        if isinstance(nodes, dict):
            for nid, cfg in nodes.items():
                self._known_ids.add(nid)
                label = getattr(cfg, "label", "") or ""
                if label:
                    self._known_ids.add(label)
        else:
            for node in nodes:
                nid = getattr(node, "id", "") or ""
                label = getattr(node, "label", "") or ""
                if nid:
                    self._known_ids.add(nid)
                if label:
                    self._known_ids.add(label)
        for edge in getattr(graph, "edges", []) or []:
            src = getattr(edge, "source_id", "")
            dst = getattr(edge, "target_id", "")
            if src and dst:
                self._edges.add((src, dst))
        self._current_node = ""
        self._evidence: set[str] = set()
        self.findings: list[ReviewFinding] = []

    def observe(self, event: Any) -> list[ReviewFinding]:
        node_id = getattr(event, "node_id", "") or ""
        content = getattr(event, "content", "") or ""
        etype = getattr(getattr(event, "type", None), "value", str(getattr(event, "type", "")))
        found: list[ReviewFinding] = []
        if etype == "tool_result":
            self._absorb_evidence(content)
        if node_id and node_id != self._current_node:
            found.extend(self._check_transition(node_id))
            self._current_node = node_id
        if etype == "think" and content and CONCLUSION_MARKERS.search(content):
            finding = self._check_traceability(content, node_id)
            if finding:
                found.append(finding)
        for f in found:
            self.findings.append(f)
            logger.warning(
                "resident_review finding kind=%s node=%s detail=%s", f.kind, f.node_id, f.detail
            )
        return found

    def _absorb_evidence(self, content: str) -> None:
        if not content:
            return
        self._evidence.update(_distinct_tokens(content))
        if len(self._evidence) > EVIDENCE_CAP:
            self._evidence = set(sorted(self._evidence)[-EVIDENCE_CAP:])

    def _check_transition(self, node_id: str) -> list[ReviewFinding]:
        prev = self._current_node
        if not prev:
            return []
        if node_id not in self._known_ids:
            return [
                ReviewFinding(
                    kind="graph_mismatch",
                    node_id=node_id,
                    detail=f"executed node '{node_id}' not present in graph",
                    ts=time.time(),
                )
            ]
        if (prev, node_id) in self._edges:
            return []
        if not any(s == prev for s, _ in self._edges):
            return []
        return [
            ReviewFinding(
                kind="graph_mismatch",
                node_id=node_id,
                detail=f"transition '{prev}' -> '{node_id}' has no declared edge",
                ts=time.time(),
            )
        ]

    def _check_traceability(self, content: str, node_id: str) -> ReviewFinding | None:
        if not self._evidence:
            return None
        claim_tokens = _distinct_tokens(content)
        if not claim_tokens:
            return None
        if claim_tokens & self._evidence:
            return None
        return ReviewFinding(
            kind="untraceable_conclusion",
            node_id=node_id,
            detail="conclusion shares no distinctive token with accumulated evidence",
            ts=time.time(),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "resident": True,
            "level": self.level,
            "findings": [f.to_dict() for f in self.findings],
            "counts": {
                "graph_mismatch": sum(1 for f in self.findings if f.kind == "graph_mismatch"),
                "untraceable_conclusion": sum(
                    1 for f in self.findings if f.kind == "untraceable_conclusion"
                ),
            },
        }


def graph_has_resident_verify(graph: Any) -> bool:
    nodes = getattr(graph, "nodes", None) or {}
    cfgs = nodes.values() if isinstance(nodes, dict) else nodes
    for node in cfgs:
        if getattr(node, "type", "") == "verify" and (getattr(node, "tool_params", {}) or {}).get(
            "resident"
        ):
            return True
    return False


def build_resident_reviewer(graph: Any) -> ResidentReviewer:
    return ResidentReviewer(graph)


def runtime_review_enabled() -> bool:
    # N10 #349: runtime-level standing review, on by default; set
    # FUSION_RUNTIME_REVIEW=0 to disable (e.g. debugging event volume).
    value = os.environ.get(RUNTIME_REVIEW_ENV, "1").strip().lower()
    return value not in ("0", "false", "off", "no")


def build_runtime_reviewer(graph: Any) -> ResidentReviewer:
    return ResidentReviewer(graph, level="runtime")
