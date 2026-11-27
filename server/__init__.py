"""Server layer — HTTP client and process manager for fusion-mlx and fusion-rag."""
# Importers: knowledge_base.py, daemon_server.py, api_server.py
# Affected API: adds FusionRAGClient, SearchResult, AskResult, DocumentInfo exports
# User instruction: "fusion-rag has completed its issues and PRs; related
# implementation work can now proceed."

from .fusion_mlx_client import FusionMLXClient, LLMResponse
from .fusion_rag_client import AskResult, DocumentInfo, FusionRAGClient, SearchResult
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
