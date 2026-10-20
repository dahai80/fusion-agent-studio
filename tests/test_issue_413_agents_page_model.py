from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB_SRC = ROOT / "web" / "src"


def test_model_label_normalizes_legacy_object():
    # #413: legacy agent manifests store model as {provider,name,temperature,
    # max_tokens}; rendering it as a React child throws error #31.
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("bun not installed")
    script = (
        "import {modelLabel} from './web/src/lib/agentModel.ts';"
        "const out=["
        "modelLabel({provider:'fusion-mlx',name:'mlx-community/Qwen3.5-4B-MLX-4bit',"
        "temperature:0.2,max_tokens:2048}),"
        "modelLabel('mlx-community/Qwen3.5-9B-4bit'),"
        "modelLabel(undefined),modelLabel(null),modelLabel({}),modelLabel('')];"
        "console.log(JSON.stringify(out));"
    )
    res = subprocess.run(
        [bun, "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    payload = res.stdout.strip().splitlines()[-1]
    assert json.loads(payload) == [
        "mlx-community/Qwen3.5-4B-MLX-4bit",
        "mlx-community/Qwen3.5-9B-4bit",
        "-",
        "-",
        "-",
        "-",
    ]


def test_agents_page_uses_model_label():
    src = (WEB_SRC / "pages" / "studio" / "AgentsPage.tsx").read_text(encoding="utf-8")
    assert "modelLabel(a.model)" in src
    assert "{a.model || '-'}" not in src
    assert "model?: AgentModel" in src


def test_router_has_error_boundary():
    app = (WEB_SRC / "App.tsx").read_text(encoding="utf-8")
    assert "<ErrorBoundary key={routeKey(route)}>" in app
    assert "</ErrorBoundary>" in app
    boundary = (WEB_SRC / "components" / "ErrorBoundary.tsx").read_text(encoding="utf-8")
    assert "getDerivedStateFromError" in boundary
    assert "componentDidCatch" in boundary
