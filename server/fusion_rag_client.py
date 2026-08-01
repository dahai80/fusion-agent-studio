"""FusionRAG HTTP client — Agent Studio's interface to fusion-rag.

Communicates exclusively through HTTP to fusion-rag's REST API.
No direct imports of fusion-rag internals.

Importers: daemon_server.py (_get_rag_client), knowledge_base.py (delegation)
API: FusionRAGClient with health/status/list_bases/create_base/get_base/
  delete_base/upload_document/batch_upload/ingest_content/delete_document/
  replace_document/list_documents/document_status/scan_directory/search/ask/
  watch_directory/unwatch_directory/watch_status/map_project/get_project_kb/unmap_project
Schemas: SearchResult(content,score,source,metadata,chunk_id),
  AskResult(answer,sources,confidence), DocumentInfo(doc_id,filename,status,chunks,metadata)
User instruction: "fusion-rag 已经完成issue和pr，可以开展相关的工作落地"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

RAG_PORT = 11436
RAG_BASE_URL = f"http://127.0.0.1:{RAG_PORT}"


@dataclass
class SearchResult:
    content: str = ""
    score: float = 0.0
    source: str = ""
    metadata: dict = field(default_factory=dict)
    chunk_id: str = ""


@dataclass
class AskResult:
    answer: str = ""
    sources: list[dict] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class DocumentInfo:
    doc_id: str = ""
    filename: str = ""
    status: str = ""
    chunks: int = 0
    metadata: dict = field(default_factory=dict)


class FusionRAGClient:
    """HTTP client for fusion-rag's REST API."""

    def __init__(
        self,
        base_url: str = RAG_BASE_URL,
        api_key: str = "local",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def health(self) -> bool:
        try:
            resp = await self.client.get("/health", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def status(self) -> dict[str, Any]:
        try:
            resp = await self.client.get("/kb/status", timeout=5.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("fusion-rag status check failed: %s", e)
            return {"available": False, "error": str(e)}

    async def list_bases(self) -> list[dict[str, Any]]:
        resp = await self.client.get("/kb/bases")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("bases", [])

    async def create_base(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "",
        **kwargs,
    ) -> dict[str, Any]:
        payload = {"name": name, "description": description}
        if embedding_model:
            payload["embedding_model"] = embedding_model
        payload.update(kwargs)
        resp = await self.client.post("/kb/bases", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def get_base(self, kb_id: str) -> dict[str, Any]:
        resp = await self.client.get(f"/kb/bases/{kb_id}")
        resp.raise_for_status()
        return resp.json()

    async def delete_base(self, kb_id: str) -> dict[str, Any]:
        resp = await self.client.delete(f"/kb/bases/{kb_id}")
        resp.raise_for_status()
        return resp.json()

    async def get_base_stats(self, kb_id: str) -> dict[str, Any]:
        resp = await self.client.get(f"/kb/bases/{kb_id}/stats")
        resp.raise_for_status()
        return resp.json()

    async def upload_document(
        self, kb_id: str, file_path: str, metadata: dict | None = None
    ) -> dict[str, Any]:
        import pathlib

        path = pathlib.Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        with open(path, "rb") as f:
            files = {"file": (path.name, f)}
            data = {}
            if metadata:
                data["metadata"] = str(metadata)
            resp = await self.client.post(
                f"/kb/bases/{kb_id}/documents", files=files, data=data
            )
        resp.raise_for_status()
        return resp.json()

    async def batch_upload(
        self, kb_id: str, file_paths: list[str], metadata: dict | None = None
    ) -> dict[str, Any]:
        import pathlib

        files_list = []
        for fp in file_paths:
            path = pathlib.Path(fp)
            if path.exists():
                files_list.append(("files", (path.name, open(path, "rb"))))

        try:
            data = {}
            if metadata:
                data["metadata"] = str(metadata)
            resp = await self.client.post(
                f"/kb/bases/{kb_id}/documents/batch", files=files_list, data=data
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            for _, (_, fh) in files_list:
                fh.close()

    async def ingest_content(
        self,
        kb_id: str,
        content: str,
        title: str = "",
        source: str = "",
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        payload = {"content": content}
        if title:
            payload["title"] = title
        if source:
            payload["source"] = source
        if metadata:
            payload["metadata"] = metadata
        resp = await self.client.post(
            f"/kb/bases/{kb_id}/documents/ingest", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_document(self, kb_id: str, doc_id: str) -> dict[str, Any]:
        resp = await self.client.delete(f"/kb/bases/{kb_id}/documents/{doc_id}")
        resp.raise_for_status()
        return resp.json()

    async def replace_document(
        self, kb_id: str, doc_id: str, file_path: str
    ) -> dict[str, Any]:
        import pathlib

        path = pathlib.Path(file_path)
        with open(path, "rb") as f:
            files = {"file": (path.name, f)}
            resp = await self.client.put(
                f"/kb/bases/{kb_id}/documents/{doc_id}", files=files
            )
        resp.raise_for_status()
        return resp.json()

    async def list_documents(self, kb_id: str) -> list[dict[str, Any]]:
        resp = await self.client.get(f"/kb/bases/{kb_id}/documents")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("documents", [])

    async def document_status(self, kb_id: str, doc_id: str) -> dict[str, Any]:
        resp = await self.client.get(f"/kb/bases/{kb_id}/documents/{doc_id}/status")
        resp.raise_for_status()
        return resp.json()

    async def scan_directory(
        self,
        kb_id: str,
        path: str,
        recursive: bool = True,
        file_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        payload = {"path": path, "recursive": recursive}
        if file_patterns:
            payload["file_patterns"] = file_patterns
        resp = await self.client.post(f"/kb/bases/{kb_id}/scan", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def search(
        self,
        kb_id: str,
        query: str,
        top_k: int = 5,
        threshold: float = 0.0,
        hybrid: bool = False,
        hybrid_alpha: float = 0.7,
        hybrid_method: str = "rrf",
        rerank: bool = False,
        folder_prefix: str = "",
        filter: dict | None = None,
        rewrite_mode: str = "",
    ) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": query,
            "top_k": top_k,
            "threshold": threshold,
        }
        if hybrid:
            payload["hybrid"] = True
            payload["hybrid_alpha"] = hybrid_alpha
            payload["hybrid_method"] = hybrid_method
        if rerank:
            payload["rerank"] = True
        if folder_prefix:
            payload["folder_prefix"] = folder_prefix
        if filter:
            payload["filter"] = filter
        if rewrite_mode:
            payload["rewrite_mode"] = rewrite_mode

        resp = await self.client.post(f"/kb/bases/{kb_id}/search", json=payload)
        resp.raise_for_status()
        data = resp.json()

        results = []
        raw = data if isinstance(data, list) else data.get("results", [])
        for item in raw:
            results.append(SearchResult(
                content=item.get("content", ""),
                score=item.get("score", 0.0),
                source=item.get("source", ""),
                metadata=item.get("metadata", {}),
                chunk_id=item.get("chunk_id", ""),
            ))
        return results

    async def ask(
        self,
        kb_id: str,
        question: str,
        model: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.7,
        hybrid: bool = False,
        rerank: bool = False,
        folder_prefix: str = "",
    ) -> AskResult:
        payload: dict[str, Any] = {
            "question": question,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if model:
            payload["model"] = model
        if hybrid:
            payload["hybrid"] = True
        if rerank:
            payload["rerank"] = True
        if folder_prefix:
            payload["folder_prefix"] = folder_prefix

        resp = await self.client.post(f"/kb/bases/{kb_id}/ask", json=payload)
        resp.raise_for_status()
        data = resp.json()

        return AskResult(
            answer=data.get("answer", ""),
            sources=data.get("sources", []),
            confidence=data.get("confidence", 0.0),
        )

    async def watch_directory(
        self, kb_id: str, path: str, recursive: bool = True
    ) -> dict[str, Any]:
        payload = {"path": path, "recursive": recursive}
        resp = await self.client.post(f"/kb/bases/{kb_id}/watch", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def unwatch_directory(self, kb_id: str) -> dict[str, Any]:
        resp = await self.client.post(f"/kb/bases/{kb_id}/unwatch")
        resp.raise_for_status()
        return resp.json()

    async def watch_status(self, kb_id: str) -> dict[str, Any]:
        resp = await self.client.get(f"/kb/bases/{kb_id}/watch/status")
        resp.raise_for_status()
        return resp.json()

    async def map_project(
        self, project_id: str, kb_id: str
    ) -> dict[str, Any]:
        payload = {"kb_id": kb_id}
        resp = await self.client.post(
            f"/kb/projects/{project_id}/kb", json=payload
        )
        resp.raise_for_status()
        return resp.json()

    async def get_project_kb(self, project_id: str) -> dict[str, Any]:
        resp = await self.client.get(f"/kb/projects/{project_id}/kb")
        resp.raise_for_status()
        return resp.json()

    async def unmap_project(self, project_id: str) -> dict[str, Any]:
        resp = await self.client.delete(f"/kb/projects/{project_id}/kb")
        resp.raise_for_status()
        return resp.json()
