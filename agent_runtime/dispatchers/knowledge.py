"""Sub-dispatcher: KnowledgeDispatcher."""

from __future__ import annotations
import logging
from .base import SubDispatcher
from typing import Callable
from ..rag_pipeline import RAGConfig

logger = logging.getLogger(__name__)


class KnowledgeDispatcher(SubDispatcher):
    def get_handlers(self) -> dict[str, Callable]:
        return {
            "knowledge.search": self._handle_knowledge_search,
            "knowledge.ingest": self._handle_knowledge_ingest,
            "knowledge.delete": self._handle_knowledge_delete,
            "knowledge.list": self._handle_knowledge_list,
            "knowledge.count": self._handle_knowledge_count,
            "rag.query": self._handle_rag_query,
            "rag.retrieve": self._handle_rag_retrieve,
            "rag.vector_search": self._handle_rag_vector_search,
            "kb.build": self._handle_kb_build,
            "kb.status": self._handle_kb_status,
            "kb.query": self._handle_kb_query,
            "kb.search": self._handle_kb_search,
            "kb.ask": self._handle_kb_ask,
            "kb.scan": self._handle_kb_scan,
            "kb.health": self._handle_kb_health,
        }

    async def _handle_knowledge_search(self, params: dict) -> dict:
        query = params.get("query", "")
        limit = params.get("limit", 5)
        try:
            from ..knowledge_engine import KnowledgeEngine

            engine = KnowledgeEngine()
            results = engine.search(query, limit=limit)
            return {"results": [r.to_dict() for r in results]}
        except Exception as e:
            logger.warning("Knowledge search failed: %s", e)
            return {"results": [], "error": str(e)}

    async def _handle_knowledge_ingest(self, params: dict) -> dict:
        content = params.get("content", "")
        scope = params.get("scope", "default")
        metadata = params.get("metadata")
        if not content:
            return {"error": "content is required"}
        try:
            from ..knowledge_engine import KnowledgeEngine

            engine = KnowledgeEngine()
            entry = engine.ingest(content, scope=scope, metadata=metadata)
            logger.info("knowledge.ingest: entry_id=%s scope=%s", entry.id, scope)
            return entry.to_dict()
        except Exception as e:
            logger.error("knowledge.ingest failed: %s", e)
            return {"error": str(e)}

    async def _handle_knowledge_delete(self, params: dict) -> dict:
        entry_id = params.get("entry_id", "")
        if not entry_id:
            return {"error": "entry_id is required"}
        try:
            from ..knowledge_engine import KnowledgeEngine

            engine = KnowledgeEngine()
            ok = engine.delete(entry_id)
            logger.info("knowledge.delete: entry_id=%s ok=%s", entry_id, ok)
            return {"deleted": ok}
        except Exception as e:
            logger.error("knowledge.delete failed: %s", e)
            return {"error": str(e)}

    async def _handle_knowledge_list(self, params: dict) -> dict:
        scope = params.get("scope", "")
        limit = params.get("limit", 100)
        try:
            from ..knowledge_engine import KnowledgeEngine

            engine = KnowledgeEngine()
            entries = engine.list_entries(scope=scope, limit=limit)
            return {"entries": [e.to_dict() for e in entries]}
        except Exception as e:
            logger.error("knowledge.list failed: %s", e)
            return {"entries": [], "error": str(e)}

    async def _handle_knowledge_count(self, params: dict) -> dict:
        scope = params.get("scope", "")
        try:
            from ..knowledge_engine import KnowledgeEngine

            engine = KnowledgeEngine()
            n = engine.count(scope=scope)
            return {"count": n}
        except Exception as e:
            logger.error("knowledge.count failed: %s", e)
            return {"count": 0, "error": str(e)}

    async def _handle_rag_query(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "message": "query parameter required"}
        rag = self._daemon._get_rag()
        config_dict = params.get("config", {})
        config = RAGConfig.from_dict(config_dict) if config_dict else None
        result = await rag.query(
            query=query,
            config=config,
            model=params.get("model", ""),
            system_prompt=params.get("system_prompt", ""),
        )
        logger.info(
            "rag.query: query=%r sources=%d", query[:50], len(result.get("sources", []))
        )
        return result

    async def _handle_rag_retrieve(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "message": "query parameter required"}
        rag = self._daemon._get_rag()
        config_dict = params.get("config", {})
        config = RAGConfig.from_dict(config_dict) if config_dict else None
        rag_result = rag.retrieve(query, config=config)
        return {
            "query": rag_result.query,
            "context_text": rag_result.context_text,
            "documents": [
                {"id": d.id, "scope": d.scope, "content_preview": d.content[:200]}
                for d in rag_result.documents
            ],
            "metadata": rag_result.metadata,
        }

    def _get_vector_strategy(self, base_url: str = "http://localhost:8900"):
        from ..rag_pipeline import VectorRetrievalStrategy

        if not hasattr(self, "_vector_strategy") or self._vector_strategy is None:
            self._vector_strategy = VectorRetrievalStrategy(base_url=base_url)
            logger.info("Created cached VectorRetrievalStrategy for %s", base_url)
        elif self._vector_strategy.base_url != base_url.rstrip("/"):
            logger.warning(
                "VectorRetrievalStrategy base_url mismatch: cached=%s requested=%s, re-creating",
                self._vector_strategy.base_url,
                base_url,
            )
            self._vector_strategy = VectorRetrievalStrategy(base_url=base_url)
        return self._vector_strategy

    async def _handle_rag_vector_search(self, params: dict) -> dict:
        query = params.get("query", "")
        if not query:
            return {"status": "error", "message": "query parameter required"}
        base_url = params.get("base_url", "http://localhost:8900")
        strategy = self._daemon._get_vector_strategy(base_url)
        available = await strategy.is_available()
        if not available:
            return {
                "status": "error",
                "message": f"fusion-kb not reachable at {base_url}",
            }
        top_k = params.get("top_k", 5)
        scope = params.get("scope", "")
        entries = await strategy.search(query, top_k=top_k, scope=scope)
        return {
            "query": query,
            "results": [
                {
                    "id": e.id,
                    "content": e.content[:500],
                    "scope": e.scope,
                    "source": e.source,
                }
                for e in entries
            ],
            "count": len(entries),
        }

    # ── Cron handlers ──

    def _get_cron_manager(self):
        from ..triggers import CronManager

        if not hasattr(self, "_cron_manager") or self._cron_manager is None:
            import os

            db_path = os.path.expanduser("~/.fusion-agent-studio/cron.db")
            self._cron_manager = CronManager(db_path=db_path)
        return self._cron_manager

    async def _handle_kb_build(self, params: dict) -> dict:
        path = params.get("path", "")
        scope = params.get("scope", "project")
        mgr = self._daemon._get_kb_manager()
        if not path:
            return {"status": "error", "message": "path parameter required"}
        import os

        if not os.path.exists(path):
            return {"status": "error", "message": f"path not found: {path}"}
        kb_name = os.path.basename(path)
        kb = mgr.create_kb(name=kb_name, description=f"Built from {path}", scope=scope)
        kb_id = kb.kb_id if hasattr(kb, "kb_id") else kb.get("kb_id", "")
        file_count = 0
        for root, dirs, files in os.walk(path):
            for fn in files:
                fp = os.path.join(root, fn)
                ext = os.path.splitext(fn)[1].lower()
                if ext in (
                    ".py",
                    ".js",
                    ".ts",
                    ".md",
                    ".txt",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".toml",
                    ".rst",
                ):
                    try:
                        mgr.add_file(kb_id, fp)
                        file_count += 1
                    except Exception as e:
                        logger.warning("kb.build: skip file %s: %s", fp, e)
        logger.info("kb.build: kb_id=%s files=%d path=%s", kb_id, file_count, path)
        return {"kb_id": kb_id, "status": "built", "file_count": file_count}

    async def _handle_kb_status(self, params: dict) -> dict:
        mgr = self._daemon._get_kb_manager()
        kb_id = params.get("kb_id", "")
        if kb_id:
            kb = mgr.get_kb(kb_id)
            if kb is None:
                return {"status": "error", "message": f"kb not found: {kb_id}"}
            kb_dict = kb.to_dict() if hasattr(kb, "to_dict") else kb
            files = mgr.list_files(kb_id)
            return {
                "kbs": [kb_dict],
                "building": False,
                "progress": 1.0,
                "file_count": len(files),
            }
        result = mgr.list_kbs()
        kbs_list = result.get("data", result) if isinstance(result, dict) else result
        kbs_dicts = [k.to_dict() if hasattr(k, "to_dict") else k for k in kbs_list]
        return {"kbs": kbs_dicts, "building": False, "progress": 1.0}

    async def _handle_kb_query(self, params: dict) -> dict:
        query = params.get("query", "")
        kb_id = params.get("kb_id", "")
        limit = params.get("limit", 10)
        if not query:
            return {"status": "error", "message": "query parameter required"}
        mgr = self._daemon._get_kb_manager()
        results = []
        if kb_id:
            files = mgr.list_files(kb_id)
            for f in files[:limit]:
                f_dict = f.to_dict() if hasattr(f, "to_dict") else f
                f_dict["relevance"] = 1.0
                results.append(f_dict)
        else:
            all_kbs = mgr.list_kbs()
            kbs_list = (
                all_kbs.get("data", all_kbs) if isinstance(all_kbs, dict) else all_kbs
            )
            for kb in kbs_list:
                kid = kb.kb_id if hasattr(kb, "kb_id") else kb.get("kb_id", "")
                files = mgr.list_files(kid)
                for f in files[:limit]:
                    f_dict = f.to_dict() if hasattr(f, "to_dict") else f
                    f_dict["relevance"] = 0.8
                    results.append(f_dict)
                if len(results) >= limit:
                    break
            results = results[:limit]
        logger.info(
            "kb.query: query=%s kb_id=%s results=%d", query[:50], kb_id, len(results)
        )
        return {"results": results}

    async def _handle_kb_search(self, params: dict) -> dict:
        kb_id = params.get("kb_id", "")
        query = params.get("query", "")
        if not kb_id or not query:
            return {"status": "error", "message": "kb_id and query required"}
        mgr = self._daemon._get_kb_manager()
        search_kwargs = {}
        for key in (
            "top_k",
            "threshold",
            "hybrid",
            "hybrid_alpha",
            "hybrid_method",
            "rerank",
            "folder_prefix",
            "rewrite_mode",
        ):
            if key in params:
                search_kwargs[key] = params[key]
        if "filter" in params:
            search_kwargs["filter"] = params["filter"]
        result = await mgr.search(kb_id=kb_id, query=query, **search_kwargs)
        logger.info(
            "kb.search: kb_id=%s query=%s count=%d",
            kb_id,
            query[:50],
            result.get("count", 0),
        )
        return result

    async def _handle_kb_ask(self, params: dict) -> dict:
        kb_id = params.get("kb_id", "")
        question = params.get("question", "")
        if not kb_id or not question:
            return {"status": "error", "message": "kb_id and question required"}
        mgr = self._daemon._get_kb_manager()
        ask_kwargs = {}
        for key in (
            "model",
            "max_tokens",
            "temperature",
            "hybrid",
            "rerank",
            "folder_prefix",
        ):
            if key in params:
                ask_kwargs[key] = params[key]
        result = await mgr.ask(kb_id=kb_id, question=question, **ask_kwargs)
        logger.info("kb.ask: kb_id=%s question=%s", kb_id, question[:50])
        return result

    async def _handle_kb_scan(self, params: dict) -> dict:
        kb_id = params.get("kb_id", "")
        path = params.get("path", "")
        if not kb_id or not path:
            return {"status": "error", "message": "kb_id and path required"}
        mgr = self._daemon._get_kb_manager()
        scan_kwargs = {}
        if "recursive" in params:
            scan_kwargs["recursive"] = params["recursive"]
        if "file_patterns" in params:
            scan_kwargs["file_patterns"] = params["file_patterns"]
        result = await mgr.scan_directory(kb_id=kb_id, path=path, **scan_kwargs)
        logger.info("kb.scan: kb_id=%s path=%s", kb_id, path)
        return result

    async def _handle_kb_health(self, params: dict) -> dict:
        mgr = self._daemon._get_kb_manager()
        available = await mgr.is_rag_available()
        status = await mgr.rag_status()
        logger.info("kb.health: rag_available=%s", available)
        return {"rag_available": available, **status}
