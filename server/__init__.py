"""Server layer — HTTP client and process manager for fusion-mlx and fusion-rag."""
# Importers: knowledge_base.py, daemon_server.py, api_server.py
# Affected API: adds FusionRAGClient, SearchResult, AskResult, DocumentInfo exports
# User instruction: "fusion-rag 已经完成issue和pr，可以开展相关的工作落地"

from .fusion_mlx_client import FusionMLXClient, LLMResponse
from .fusion_rag_client import FusionRAGClient, SearchResult, AskResult, DocumentInfo
from .process_manager import FusionMLXProcessManager

__all__ = [
    "FusionMLXClient",
    "LLMResponse",
    "FusionRAGClient",
    "SearchResult",
    "AskResult",
    "DocumentInfo",
    "FusionMLXProcessManager",
]