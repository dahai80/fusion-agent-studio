"""Sub-dispatcher modules for DaemonServer RPC decomposition."""

from .agent import AgentDispatcher
from .artifact import ArtifactDispatcher
from .base import SubDispatcher
from .chat import ChatDispatcher
from .deploy import DeployDispatcher
from .infra import InfraDispatcher
from .knowledge import KnowledgeDispatcher
from .marketplace import MarketplaceDispatcher
from .mcp import McpDispatcher
from .memory import MemoryDispatcher
from .planner import PlannerDispatcher
from .plugin import PluginDispatcher
from .safety import SafetyDispatcher
from .team import TeamDispatcher
from .trainer import TrainerDispatcher
from .workflow import WorkflowDispatcher

__all__ = [
    "SubDispatcher",
    "MarketplaceDispatcher",
    "McpDispatcher",
    "DeployDispatcher",
    "KnowledgeDispatcher",
    "AgentDispatcher",
    "ChatDispatcher",
    "TeamDispatcher",
    "InfraDispatcher",
    "WorkflowDispatcher",
    "SafetyDispatcher",
    "PlannerDispatcher",
    "MemoryDispatcher",
    "PluginDispatcher",
    "ArtifactDispatcher",
    "TrainerDispatcher",
]
