import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DIR_NAME = ".fusion-agent-studio"


def studio_home() -> Path:
    override = os.environ.get("FUSION_AGENT_STUDIO_HOME")
    if override:
        return Path(override)
    return Path.home() / DIR_NAME
