"""N0 hook engine: lifecycle hook registry + dispatch (#357 shim)."""

#357: canonical kernel lives in fusion-harness (pip install fusion-harness).
# In-tree copy kept as _hooks_local fallback so CI (no harness extra) stays green.

try:
    from fusion_harness.hooks import *  # noqa: F401,F403
    from fusion_harness.hooks import _DECORATOR_REGISTRY  # noqa: F401

    KERNEL_SOURCE = "fusion-harness"
except ImportError:
    from ._hooks_local import *  # noqa: F401,F403

    KERNEL_SOURCE = "local"
