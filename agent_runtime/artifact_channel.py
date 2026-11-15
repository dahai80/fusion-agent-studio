"""#714 part 2: artifact channel — canonical kernel re-export shim.

Fleet-wide shim pattern (matches funnels.py / permissions.py / hooks.py /
compactor.py): re-export the fusion_harness.artifact_channel surface
(ArtifactStore / SummaryInjector / WorkerDelivery / default_summarize /
ArtifactTools, harness #27 + K3 #36) so studio consumers import the kernel
mechanism through a stable local module.

Studio-product capture behavior (ArtifactManager records/policies, budget
-aware context tiers in agent_runtime/artifact_tools.py, versioned run
records in run_artifacts.py) stays local — it is product logic, not the
worker full-output / ref-only mechanism this channel owns. Ref semantics:
full text lives ONLY on disk under the channel root; everything crossing
RPC / team / DAG message boundaries is a ref plus a short summary.
"""

import logging

from fusion_harness.artifact_channel import *  # noqa: F401,F403
from fusion_harness.artifact_channel import (  # noqa: F401 — explicit for IDE
    ArtifactStore,
    ArtifactTools,
    SummaryInjector,
    WorkerDelivery,
    default_summarize,
)

from .paths import studio_home

logger = logging.getLogger(__name__)


class StudioArtifactChannel:
    """Ready-to-use kernel channel rooted in the studio data home."""

    def __init__(self, root: str = ""):
        # Root resolved lazily: FUSION_AGENT_STUDIO_HOME may be set after
        # import (test isolation, #714 fixture pattern).
        self.root = str(root) or str(studio_home() / "artifact_channel")
        self.store = ArtifactStore(self.root)
        self.injector = SummaryInjector()
        self.tools = ArtifactTools(self.store)


_CHANNEL: StudioArtifactChannel | None = None


def studio_artifact_channel() -> StudioArtifactChannel:
    global _CHANNEL
    if _CHANNEL is None:
        _CHANNEL = StudioArtifactChannel()
        logger.info("studio artifact channel ready root=%s", _CHANNEL.root)
    return _CHANNEL
