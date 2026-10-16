"""#357: fusion-harness kernel extraction — shim routing + drift parity."""

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

AGENT_RT = Path(__file__).resolve().parent.parent / "agent_runtime"

KERNEL_MODULES = ["funnels", "permissions", "hooks", "compactor"]

try:
    import fusion_harness  # noqa: F401

    HAVE_KERNEL = True
except ImportError:
    HAVE_KERNEL = False


def _local_ast(mod: str) -> str:
    src = (AGENT_RT / f"_{mod}_local.py").read_text()
    return ast.dump(ast.parse(src))


def test_kernel_source_present():
    import agent_runtime.funnels as funnels

    assert funnels.KERNEL_SOURCE in ("fusion-harness", "local")


def test_fallback_import_works():
    import importlib

    with patch.dict(sys.modules, {"fusion_harness": None, "fusion_harness.funnels": None}):
        import agent_runtime.funnels as funnels

        importlib.reload(funnels)
        assert funnels.KERNEL_SOURCE == "local"
        assert hasattr(funnels, "LLMFunnel")
        assert hasattr(funnels, "ToolFunnel")
        assert hasattr(funnels, "LifecycleBus")
    importlib.reload(funnels)
    assert hasattr(funnels, "LLMFunnel")


@pytest.mark.skipif(not HAVE_KERNEL, reason="fusion-harness not installed")
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


@pytest.mark.skipif(not HAVE_KERNEL, reason="fusion-harness not installed")
def test_kernel_no_drift_vs_local_copy():
    repo_root = AGENT_RT.parent
    kernel_dir = None
    for cand in (repo_root.parent / "fusion-harness" / "fusion_harness",):
        if cand.exists():
            kernel_dir = cand
            break
    if kernel_dir is None:
        pytest.skip("fusion-harness sibling repo not found locally")
    for mod in KERNEL_MODULES:
        kernel_tree = ast.parse((kernel_dir / f"{mod}.py").read_text())
        # kernel copy imports sibling via absolute package path; local uses
        # relative — normalize so the ASTs are directly comparable.
        for node in ast.walk(kernel_tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("fusion_harness."):
                node.module = node.module.split(".", 1)[1]
                node.level = 1
        # kernel copy appends an __all__ export list the fallback lacks — drop it.
        if kernel_tree.body and isinstance(kernel_tree.body[-1], ast.Assign):
            targets = kernel_tree.body[-1].targets
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == "__all__":
                kernel_tree.body.pop()
        local_tree = ast.parse((AGENT_RT / f"_{mod}_local.py").read_text())
        assert ast.dump(kernel_tree) == ast.dump(local_tree), f"{mod}: kernel drifted from local copy"


def test_local_copy_parseable():
    for mod in KERNEL_MODULES:
        tree = ast.parse((AGENT_RT / f"_{mod}_local.py").read_text())
        assert tree.body
