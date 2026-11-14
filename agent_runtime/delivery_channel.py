# Issue #702 P7: worker delivery discipline (fusion-harness 0.11
# artifact_channel). Full worker output lands on disk through ArtifactStore;
# the orchestrator context only receives a deterministic summary plus an
# artifact ref, injected front-of-context by SummaryInjector (idempotent
# re-inject replaces the previous worker-summaries segment).
import logging

from fusion_harness.artifact_channel import (
    ArtifactStore,
    SummaryInjector,
    WorkerDelivery,
)

from .paths import studio_home

logger = logging.getLogger(__name__)


def worker_artifacts_enabled() -> bool:
    import os

    return os.environ.get("FUSION_WORKER_ARTIFACTS", "1") != "0"


class WorkerDeliveryChannel:
    def __init__(self, root=None, summarize=None):
        root = root or (studio_home() / "worker_artifacts")
        self.store = ArtifactStore(str(root))
        self.injector = SummaryInjector(summarize=summarize)

    def deliver(
        self, run_id: str, worker_name: str, content: str, output_format: str = ""
    ) -> WorkerDelivery:
        ref = self.store.write_artifact(run_id, worker_name, content)
        summary = self.injector.summarize_delivery(worker_name, content, output_format)
        logger.info(
            "worker delivery run=%s worker=%s artifact=%s summary_len=%d",
            run_id, worker_name, ref, len(summary),
        )
        return WorkerDelivery(worker_name=worker_name, artifact_ref=ref, summary=summary)

    def inject_front(self, messages: list[dict], deliveries: list[WorkerDelivery]) -> list[dict]:
        return self.injector.inject_front(messages, deliveries)
