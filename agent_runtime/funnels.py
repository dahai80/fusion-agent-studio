"""N0 harness funnels: tool/LLM/lifecycle control points (#357 shim)."""

#357: canonical kernel lives in fusion-harness (pip install fusion-harness).
# In-tree copy kept as _funnels_local fallback so CI (no harness extra) stays green.

try:
    from fusion_harness.funnels import *  # noqa: F401,F403

    KERNEL_SOURCE = "fusion-harness"
except ImportError:
    from ._funnels_local import *  # noqa: F401,F403

    KERNEL_SOURCE = "local"
