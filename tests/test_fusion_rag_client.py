# Tests for FusionRAGClient
# Importers: pytest runner
# API: FusionRAGClient, SearchResult, AskResult, DocumentInfo, RAG_BASE_URL, RAG_PORT
# Schemas: SearchResult(content,score,source,metadata,chunk_id), AskResult(answer,sources,confidence)
# User instruction: "fusion-rag 已经完成issue和pr，可以开展相关的工作落地"

import pytest
from unittest.mock import AsyncMock, MagicMock

from server.fusion_rag_client import (
    FusionRAGClient,
    SearchResult,
    AskResult,
    DocumentInfo,
    RAG_BASE_URL,
    RAG_PORT,
)


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _mocked_client():
    client = FusionRAGClient()
    mock_http = MagicMock()
    mock_http.get = AsyncMock(return_value=_mock_response({}))
    mock_http.post = AsyncMock(return_value=_mock_response({}))
    mock_http.delete = AsyncMock(return_value=_mock_response({}))
    mock_http.put = AsyncMock(return_value=_mock_response({}))
    client._client = mock_http
    return client, mock_http


class TestFusionRAGClientInit:
    def test_default_config(self):
        client = FusionRAGClient()
        assert client.base_url == f"http://127.0.0.1:{RAG_PORT}"
        assert client.api_key == "local"
        assert client.timeout == 60.0

    def test_custom_config(self):
        client = FusionRAGClient(
            base_url="http://custom:9999", api_key="test", timeout=30.0
        )
        assert client.base_url == "http://custom:9999"

    def test_base_url_trailing_slash_stripped(self):
        client = FusionRAGClient(base_url="http://localhost:11436/")
        assert client.base_url == "http://localhost:11436"


class TestFusionRAGClientHealth:
    @pytest.mark.asyncio
    async def test_health_ok(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(return_value=_mock_response({}, 200))
        result = await client.health()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_fail(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(side_effect=Exception("connection refused"))
        result = await client.health()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_non_200(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(return_value=_mock_response({}, 503))
        result = await client.health()
        assert result is False


class TestFusionRAGClientKB:
    @pytest.mark.asyncio
    async def test_list_bases(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(
            return_value=_mock_response({"bases": [{"id": "kb1"}]})
        )
        result = await client.list_bases()
        assert len(result) == 1
        assert result[0]["id"] == "kb1"

    @pytest.mark.asyncio
    async def test_list_bases_array(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(return_value=_mock_response([{"id": "kb1"}]))
        result = await client.list_bases()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_create_base(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(
            return_value=_mock_response({"id": "kb1", "name": "test"})
        )
        result = await client.create_base(name="test", description="desc")
        assert result["name"] == "test"

    @pytest.mark.asyncio
    async def test_get_base(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(
            return_value=_mock_response({"id": "kb1", "name": "test"})
        )
        result = await client.get_base("kb1")
        assert result["id"] == "kb1"

    @pytest.mark.asyncio
    async def test_delete_base(self):
        client, mock_http = _mocked_client()
        mock_http.delete = AsyncMock(return_value=_mock_response({"deleted": True}))
        result = await client.delete_base("kb1")
        assert result["deleted"] is True


class TestFusionRAGClientSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                {
                    "results": [
                        {
                            "content": "hello",
                            "score": 0.95,
                            "source": "a.txt",
                            "metadata": {},
                            "chunk_id": "c1",
                        },
                    ]
                }
            )
        )
        results = await client.search(kb_id="kb1", query="hello")
        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_search_with_hybrid(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(return_value=_mock_response({"results": []}))
        await client.search(kb_id="kb1", query="test", hybrid=True, hybrid_alpha=0.5)
        call_args = mock_http.post.call_args
        payload = call_args[1]["json"]
        assert payload["hybrid"] is True
        assert payload["hybrid_alpha"] == 0.5

    @pytest.mark.asyncio
    async def test_search_with_rerank(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(return_value=_mock_response({"results": []}))
        await client.search(kb_id="kb1", query="test", rerank=True)
        call_args = mock_http.post.call_args
        payload = call_args[1]["json"]
        assert payload["rerank"] is True

    @pytest.mark.asyncio
    async def test_search_with_folder_prefix(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(return_value=_mock_response({"results": []}))
        await client.search(kb_id="kb1", query="test", folder_prefix="docs/")
        call_args = mock_http.post.call_args
        payload = call_args[1]["json"]
        assert payload["folder_prefix"] == "docs/"


class TestFusionRAGClientAsk:
    @pytest.mark.asyncio
    async def test_ask_returns_answer(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(
            return_value=_mock_response(
                {
                    "answer": "42",
                    "sources": [{"source": "a.txt"}],
                    "confidence": 0.95,
                }
            )
        )
        result = await client.ask(kb_id="kb1", question="what?")
        assert isinstance(result, AskResult)
        assert result.answer == "42"
        assert result.confidence == 0.95
        assert len(result.sources) == 1


class TestFusionRAGClientDocuments:
    @pytest.mark.asyncio
    async def test_list_documents(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(
            return_value=_mock_response({"documents": [{"doc_id": "d1"}]})
        )
        result = await client.list_documents("kb1")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_ingest_content(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(
            return_value=_mock_response({"doc_id": "d1", "status": "indexed"})
        )
        result = await client.ingest_content(kb_id="kb1", content="hello", title="test")
        assert result["doc_id"] == "d1"

    @pytest.mark.asyncio
    async def test_delete_document(self):
        client, mock_http = _mocked_client()
        mock_http.delete = AsyncMock(return_value=_mock_response({"deleted": True}))
        result = await client.delete_document(kb_id="kb1", doc_id="d1")
        assert result["deleted"] is True


class TestFusionRAGClientScan:
    @pytest.mark.asyncio
    async def test_scan_directory(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(
            return_value=_mock_response({"scanned": 5, "indexed": 3})
        )
        result = await client.scan_directory(
            kb_id="kb1", path="/data/docs", recursive=True
        )
        assert result["scanned"] == 5


class TestFusionRAGClientProjects:
    @pytest.mark.asyncio
    async def test_map_project(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(
            return_value=_mock_response({"project_id": "p1", "kb_id": "kb1"})
        )
        result = await client.map_project(project_id="p1", kb_id="kb1")
        assert result["project_id"] == "p1"

    @pytest.mark.asyncio
    async def test_get_project_kb(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(
            return_value=_mock_response({"project_id": "p1", "kb_id": "kb1"})
        )
        result = await client.get_project_kb("p1")
        assert result["kb_id"] == "kb1"

    @pytest.mark.asyncio
    async def test_unmap_project(self):
        client, mock_http = _mocked_client()
        mock_http.delete = AsyncMock(return_value=_mock_response({"unmapped": True}))
        result = await client.unmap_project("p1")
        assert result["unmapped"] is True


class TestFusionRAGClientWatch:
    @pytest.mark.asyncio
    async def test_watch_directory(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(return_value=_mock_response({"watching": True}))
        result = await client.watch_directory(kb_id="kb1", path="/data/docs")
        assert result["watching"] is True

    @pytest.mark.asyncio
    async def test_unwatch_directory(self):
        client, mock_http = _mocked_client()
        mock_http.post = AsyncMock(return_value=_mock_response({"stopped": True}))
        result = await client.unwatch_directory("kb1")
        assert result["stopped"] is True

    @pytest.mark.asyncio
    async def test_watch_status(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(return_value=_mock_response({"watching": True}))
        result = await client.watch_status("kb1")
        assert result["watching"] is True


class TestFusionRAGClientClose:
    @pytest.mark.asyncio
    async def test_close(self):
        client = FusionRAGClient()
        mock_inner = AsyncMock()
        client._client = mock_inner
        await client.close()
        mock_inner.aclose.assert_called_once()
        assert client._client is None

    @pytest.mark.asyncio
    async def test_close_no_client(self):
        client = FusionRAGClient()
        await client.close()
        assert client._client is None


class TestFusionRAGClientStatus:
    @pytest.mark.asyncio
    async def test_status_ok(self):
        client, mock_http = _mocked_client()
        mock_http.get = AsyncMock(
            return_value=_mock_response({"available": True, "version": "0.1.0"})
        )
        result = await client.status()
        assert result["available"] is True

    @pytest.mark.asyncio
    async def test_status_error(self):
        client = FusionRAGClient()
        client._client = MagicMock()
        client._client.get = AsyncMock(side_effect=Exception("fail"))
        result = await client.status()
        assert result["available"] is False


class TestDataClasses:
    def test_search_result_defaults(self):
        sr = SearchResult()
        assert sr.content == ""
        assert sr.score == 0.0

    def test_ask_result_defaults(self):
        ar = AskResult()
        assert ar.answer == ""
        assert ar.confidence == 0.0

    def test_document_info_defaults(self):
        di = DocumentInfo()
        assert di.chunks == 0


class TestRAGConstants:
    def test_rag_port(self):
        assert RAG_PORT == 11436

    def test_rag_base_url(self):
        assert RAG_BASE_URL == "http://127.0.0.1:11436"
