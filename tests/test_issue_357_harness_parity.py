"""#357/#2: fusion-harness kernel — hard dependency + contract conformance.

The in-tree fallback copies are deleted (issue #2 R1): fusion-harness is a
hard dependency and an import failure is a loud ImportError. The behavioral
contract all runtimes must reproduce lives in fusion-harness docs/contract.md.
"""

import subprocess
import sys
from pathlib import Path

import pytest

AGENT_RT = Path(__file__).resolve().parent.parent / "agent_runtime"

KERNEL_MODULES = ["funnels", "permissions", "hooks", "compactor"]


def _kernel_repo() -> Path | None:
    for cand in (
        Path(__file__).resolve().parent.parent.parent / "fusion-harness",
        Path.home() / "fusion" / "fusion-harness",
    ):
        if (cand / "docs" / "contract.md").exists():
            return cand
    return None


def test_kernel_source_is_hard_dependency():
    # #2 R1: KERNEL_SOURCE shims are gone; the kernel IS the fusion-harness
    # package. A missing install must fail loudly, not fall back.
    import agent_runtime.funnels as funnels

    assert not hasattr(funnels, "KERNEL_SOURCE")
    import fusion_harness

    # 0.2.0 flipped enforcement to fail-closed by default (#1).
    major, minor, _patch = (int(x) for x in fusion_harness.KERNEL_VERSION.split("."))
    assert (major, minor) >= (0, 2)


def test_public_surface_matches_local():
    import agent_runtime.compactor as cmod
    import agent_runtime.funnels as fmod
    import agent_runtime.hooks as hmod
    import agent_runtime.permissions as pmod

    for mod, names in [
        (fmod, ["LifecycleBus", "LLMFunnel", "ToolFunnel"]),
        (pmod, ["PermissionEngine", "PermissionMode", "PermissionRule"]),
        (hmod, ["HookConfig", "HookEngine", "HookResult"]),
        (cmod, ["CompactionConfig", "Compactor"]),
    ]:
        for name in names:
            assert hasattr(mod, name), f"{mod.__name__} missing {name}"
            assert getattr(mod, name) is not None


def test_no_fallback_copies_remain():
    # #2 AC1: zero hits for _funnels_local/KERNEL_SOURCE in the runtime.
    leftovers = sorted(p.name for p in AGENT_RT.glob("_*_local.py"))
    assert leftovers == [], f"kernel fallback copies still present: {leftovers}"


@pytest.mark.skipif(_kernel_repo() is None, reason="fusion-harness sibling repo not found")
def test_contract_doc_exists_and_is_referenced():
    # #2 AC3: the parity guard points at the kernel contract, not at
    # duplicated source copies.
    contract = _kernel_repo() / "docs" / "contract.md"
    text = contract.read_text()
    for section in ("Funnel Semantics", "Permission Engine", "Hook Engine", "Compactor"):
        assert section in text, f"contract.md missing section: {section}"


def test_kernel_import_error_is_loud():
    # Simulate a missing fusion-harness install: the shim must NOT swallow
    # the ImportError into a local-copy fallback.
    code = (
        "import sys\n"
        "sys.modules['fusion_harness'] = None\n"
        "sys.modules['fusion_harness.funnels'] = None\n"
        "try:\n"
        "    import agent_runtime.funnels\n"
        "except ImportError:\n"
        "    print('LOUD_IMPORT_ERROR')\n"
        "    sys.exit(0)\n"
        "print('UNEXPECTED_SUCCESS')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert "LOUD_IMPORT_ERROR" in out.stdout, out.stdout + out.stderr
