"""N0 permission engine: deny-first rule evaluation (#357 shim)."""

#357: canonical kernel lives in fusion-harness (pip install fusion-harness).
# In-tree copy kept as _permissions_local fallback so CI (no harness extra) stays green.

try:
    from fusion_harness.permissions import *  # noqa: F401,F403

    KERNEL_SOURCE = "fusion-harness"
except ImportError:
    from ._permissions_local import *  # noqa: F401,F403

    KERNEL_SOURCE = "local"
