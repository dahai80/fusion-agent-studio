from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .knowledge_engine import KnowledgeEngine, KnowledgeEntry
from .llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


@dataclass
class RAGConfig:
    top_k: int = 5
    similarity_threshold: float = 0.3
    rerank: bool = True
    mode: str = "hybrid"
    scope: str = ""
    max_context_tokens: int = 3000

    def to_dict(self) -> dict[str, Any]:
        return {
            "top_k": self.top_k,
            "similarity_threshold": self.similarity_threshold,
            "rerank": self.rerank,
            "mode": self.mode,
            "scope": self.scope,
            "max_context_tokens": self.max_context_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RAGConfig:
        return cls(
            top_k=data.get("top_k", 5),
            similarity_threshold=data.get("similarity_threshold", 0.3),
            rerank=data.get("rerank", True),
            mode=data.get("mode", "hybrid"),
            scope=data.get("scope", ""),
            max_context_tokens=data.get("max_context_tokens", 3000),
        )


@dataclass
class RAGResult:
    query: str = ""
    documents: list[KnowledgeEntry] = field(default_factory=list)
    context_text: str = ""
    scores: list[float] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "documents": [d.to_dict() for d in self.documents],
            "context_text": self.context_text,
            "scores": self.scores,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RAGResult:
        return cls(
            query=data.get("query", ""),
            documents=[KnowledgeEntry.from_dict(d) for d in data.get("documents", [])],
            context_text=data.get("context_text", ""),
            scores=data.get("scores", []),
            metadata=data.get("metadata", {}),
        )


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _assemble_context(documents: list[KnowledgeEntry], max_tokens: int) -> tuple[str, list[float]]:
    parts: list[str] = []
    token_counts: list[int] = []
    total_tokens = 0
    for doc in documents:
        chunk = f"[{doc.id}] (scope={doc.scope})\n{doc.content}"
        chunk_tokens = _estimate_tokens(chunk)
        if total_tokens + chunk_tokens > max_tokens:
            remaining = max_tokens - total_tokens
            if remaining > 20:
                char_budget = remaining * 4
                truncated = chunk[:char_budget] + "..."
                parts.append(truncated)
                token_counts.append(_estimate_tokens(truncated))
                total_tokens += _estimate_tokens(truncated)
            break
        parts.append(chunk)
        token_counts.append(chunk_tokens)
        total_tokens += chunk_tokens

    context_text = "\n---\n".join(parts) if parts else ""
    scores = [1.0 - (i * 0.05) for i in range(len(parts))]
    return context_text, scores


class RAGPipeline:
    def __init__(self, knowledge_engine: KnowledgeEngine | None = None, gateway: LLMGateway | None = None):
        self.knowledge = knowledge_engine
        self.gateway = gateway

    def retrieve(self, query: str, config: RAGConfig | None = None) -> RAGResult:
        cfg = config or RAGConfig()
        logger.info("RAG retrieve: query=%r mode=%s top_k=%d scope=%r", query, cfg.mode, cfg.top_k, cfg.scope)

        if not self.knowledge:
            logger.warning("RAG retrieve: no knowledge engine, returning empty result")
            return RAGResult(query=query, metadata={"mode": cfg.mode, "scope": cfg.scope, "error": "no knowledge engine"})

        safe_query = self._sanitize_fts_query(query)
        logger.debug("RAG sanitized query: %r -> %r", query, safe_query)

        documents = self.knowledge.search(
            query=safe_query,
            scope=cfg.scope,
            mode=cfg.mode,
            limit=cfg.top_k * 2 if cfg.rerank else cfg.top_k,
        )
        logger.debug("RAG initial search returned %d documents", len(documents))

        if cfg.rerank and len(documents) > cfg.top_k:
            documents = self._rerank(documents, query)[: cfg.top_k]
            logger.debug("RAG reranked to %d documents", len(documents))

        documents = documents[: cfg.top_k]

        context_text, scores = _assemble_context(documents, cfg.max_context_tokens)
        logger.info(
            "RAG retrieve complete: %d docs, context_tokens~%d, context_chars=%d",
            len(documents),
            _estimate_tokens(context_text),
            len(context_text),
        )

        return RAGResult(
            query=query,
            documents=documents,
            context_text=context_text,
            scores=scores,
            metadata={
                "mode": cfg.mode,
                "scope": cfg.scope,
                "top_k": cfg.top_k,
                "rerank": cfg.rerank,
                "retrieved_at": time.time(),
            },
        )

    _FTS_STOP_WORDS = frozenset({"a", "an", "the", "is", "are", "was", "were", "be",
        "been", "being", "have", "has", "had", "do", "does", "did", "will",
        "would", "could", "should", "may", "might", "shall", "can", "to",
        "of", "in", "for", "on", "with", "at", "by", "from", "as", "it",
        "its", "this", "that", "these", "those", "and", "or", "but", "not",
        "what", "which", "who", "whom", "how", "when", "where", "why"})

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        cleaned = re.sub(r'[^\w\s]', ' ', query)
        terms = cleaned.split()
        terms = [t for t in terms if t.lower() not in RAGPipeline._FTS_STOP_WORDS and len(t) > 1]
        if not terms:
            terms = [w for w in query.split() if len(w) > 1][:3]
        return " OR ".join(terms) if terms else query

    def _rerank(self, documents: list[KnowledgeEntry], query: str) -> list[KnowledgeEntry]:
        query_lower = query.lower()
        query_terms = set(query_lower.split())

        def score(doc: KnowledgeEntry) -> float:
            content_lower = doc.content.lower()
            s = 0.0
            for term in query_terms:
                count = content_lower.count(term)
                s += count * 0.1
            if query_lower in content_lower:
                s += 1.0
            s += len(doc.content) / 10000.0
            return s

        scored = [(doc, score(doc)) for doc in documents]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [doc for doc, _ in scored]

    async def generate(self, query: str, context_text: str, model: str = "", system_prompt: str = "") -> str:
        logger.info("RAG generate: query=%r model=%r context_len=%d", query, model, len(context_text))

        if not self.gateway:
            logger.warning("No LLM gateway configured, returning stub answer")
            return f"[RAG stub] Based on {len(context_text)} chars of retrieved context, I would answer your question: {query}"

        rag_system = system_prompt or "You are a helpful assistant. Answer the user's question based on the provided context."
        if context_text:
            rag_system += "\n\n--- Retrieved Context ---\n" + context_text + "\n--- End of Context ---"

        messages = [
            {"role": "system", "content": rag_system},
            {"role": "user", "content": query},
        ]

        try:
            resp = await asyncio.wait_for(
                self.gateway.chat(messages=messages, model=model or ""),
                timeout=120.0,
            )
            if resp.finish_reason == "error" and resp.usage.get("error"):
                logger.error("RAG gateway error: %s", resp.usage["error"])
                return f"[RAG error] {resp.usage['error']}"
            content = resp.content
            logger.info("RAG generate complete: %d chars", len(content))
            return content
        except asyncio.TimeoutError:
            logger.error("RAG generate timeout for query=%r", query)
            return "[RAG error] Generation timed out"
        except Exception as exc:
            logger.error("RAG generate exception: %s", exc)
            return f"[RAG error] {exc}"

    async def agenerate(self, query: str, context_text: str, model: str = "", system_prompt: str = "") -> str:
        logger.info("RAG agenerate: query=%r model=%r", query, model)

        if not self.gateway:
            logger.warning("No LLM gateway configured, returning stub answer")
            return f"[RAG stub] Based on {len(context_text)} chars of retrieved context, I would answer your question: {query}"

        rag_system = system_prompt or "You are a helpful assistant. Answer the user's question based on the provided context."
        if context_text:
            rag_system += "\n\n--- Retrieved Context ---\n" + context_text + "\n--- End of Context ---"

        messages = [
            {"role": "system", "content": rag_system},
            {"role": "user", "content": query},
        ]

        try:
            resp = await asyncio.wait_for(
                self.gateway.chat(messages=messages, model=model or ""),
                timeout=120.0,
            )
            if resp.finish_reason == "error" and resp.usage.get("error"):
                logger.error("RAG gateway error: %s", resp.usage["error"])
                return f"[RAG error] {resp.usage['error']}"
            content = resp.content
            logger.info("RAG agenerate complete: %d chars", len(content))
            return content
        except asyncio.TimeoutError:
            logger.error("RAG agenerate timeout for query=%r", query)
            return "[RAG error] Generation timed out"
        except Exception as exc:
            logger.error("RAG agenerate exception: %s", exc)
            return f"[RAG error] {exc}"

    async def query(self, query: str, config: RAGConfig | None = None, model: str = "", system_prompt: str = "") -> dict[str, Any]:
        logger.info("RAG query: query=%r", query)
        rag_result = self.retrieve(query, config)
        answer = await self.generate(query, rag_result.context_text, model=model, system_prompt=system_prompt)

        result = {
            "answer": answer,
            "sources": [
                {"id": doc.id, "scope": doc.scope, "content_preview": doc.content[:200]}
                for doc in rag_result.documents
            ],
            "context_tokens": _estimate_tokens(rag_result.context_text),
            "query": query,
            "metadata": rag_result.metadata,
        }
        logger.info("RAG query complete: sources=%d, context_tokens=%d", len(result["sources"]), result["context_tokens"])
        return result

    async def aquery(self, query: str, config: RAGConfig | None = None, model: str = "", system_prompt: str = "") -> dict[str, Any]:
        logger.info("RAG aquery: query=%r", query)
        rag_result = self.retrieve(query, config)
        answer = await self.agenerate(query, rag_result.context_text, model=model, system_prompt=system_prompt)

        result = {
            "answer": answer,
            "sources": [
                {"id": doc.id, "scope": doc.scope, "content_preview": doc.content[:200]}
                for doc in rag_result.documents
            ],
            "context_tokens": _estimate_tokens(rag_result.context_text),
            "query": query,
            "metadata": rag_result.metadata,
        }
        logger.info("RAG aquery complete: sources=%d, context_tokens=%d", len(result["sources"]), result["context_tokens"])
        return result


class RAGNodeMixin:
    def build_rag_context(self, query: str, config_dict: dict[str, Any]) -> RAGResult:
        cfg = RAGConfig.from_dict(config_dict)
        if not hasattr(self, "_rag_pipeline") or self._rag_pipeline is None:
            logger.error("RAG pipeline not initialized on runtime, cannot build context")
            return RAGResult(query=query, metadata={"error": "RAG pipeline not initialized"})

        logger.info("RAGNodeMixin build_rag_context: query=%r mode=%s", query, cfg.mode)
        return self._rag_pipeline.retrieve(query, cfg)
