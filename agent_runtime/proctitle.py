"""Process title helper — long-running processes must show the product name (#490).

Layered resolution: fusion_core.set_process_title (shared in-tree helper)
first, then a direct setproctitle import, then a no-op fallback so every
entrypoint still starts when neither is importable.
"""
import logging

logger = logging.getLogger(__name__)


def set_process_title(title: str) -> None:
    try:
        from fusion_core import set_process_title as _core_set

        _core_set(title)
        return
    except ImportError:
        pass
    except Exception as e:
        logger.warning("fusion_core.set_process_title failed (%s): %s", title, e)
        return
    try:
        import setproctitle

        setproctitle.setproctitle(title)
    except Exception as e:
        logger.debug("setproctitle unavailable, title stays default (%s): %s", title, e)
