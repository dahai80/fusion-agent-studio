"""N0 context compactor: window compaction + retention (#357 shim)."""

#357: canonical kernel lives in fusion-harness (pip install fusion-harness).
# In-tree copy kept as _compactor_local fallback so CI (no harness extra) stays green.

try:
    from fusion_harness.compactor import *  # noqa: F401,F403

    KERNEL_SOURCE = "fusion-harness"
except ImportError:
    from ._compactor_local import *  # noqa: F401,F403

    KERNEL_SOURCE = "local"
