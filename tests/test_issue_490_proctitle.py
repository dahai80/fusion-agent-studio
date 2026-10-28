"""#490: long-running processes must show the product name, not bare Python."""
import subprocess
import sys

import pytest

pytest.importorskip("setproctitle")


def test_helper_prefers_fusion_core(monkeypatch):
    from agent_runtime import proctitle

    calls = []
    import types

    fake_core = types.ModuleType("fusion_core")

    def _core_set(title):
        calls.append(("core", title))

    fake_core.set_process_title = _core_set
    monkeypatch.setitem(sys.modules, "fusion_core", fake_core)
    proctitle.set_process_title("t-core")
    assert calls == [("core", "t-core")]


def test_helper_falls_back_to_setproctitle(monkeypatch):
    # simulate fusion_core absent (CI installs only [test] extras)
    from agent_runtime import proctitle

    monkeypatch.setitem(sys.modules, "fusion_core", None)
    calls = []
    import setproctitle

    monkeypatch.setattr(setproctitle, "setproctitle", lambda t: calls.append(t))
    proctitle.set_process_title("t-direct")
    assert calls == ["t-direct"]


def test_helper_noop_when_both_missing(monkeypatch, caplog):
    import logging

    from agent_runtime import proctitle

    monkeypatch.setitem(sys.modules, "fusion_core", None)
    monkeypatch.setitem(sys.modules, "setproctitle", None)
    caplog.set_level(logging.DEBUG, logger="agent_runtime.proctitle")
    proctitle.set_process_title("t-noop")
    assert any("setproctitle unavailable" in r.getMessage() for r in caplog.records)


def test_api_server_run_server_sets_title(monkeypatch):
    import agent_runtime.api_server as api_server
    import agent_runtime.proctitle as proctitle

    calls = []
    monkeypatch.setattr(proctitle, "set_process_title", lambda t: calls.append(t))

    import types

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda *a, **k: calls.append(("uvicorn", a, k))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    api_server.run_server(port=11442)
    assert calls[0] == "fusion-agent-studio api :11442"
    assert calls[1][0] == "uvicorn"


def test_daemon_process_title_visible_in_ps(tmp_path):
    # acceptance criterion #490: ps must show the product name, not bare Python
    script = tmp_path / "title_probe.py"
    script.write_text(
        "from agent_runtime.proctitle import set_process_title\n"
        "import time\n"
        "set_process_title('fusion-agent-studio test-probe')\n"
        "print('ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        out = subprocess.run(
            ["ps", "-axww", "-o", "comm=", "-p", str(proc.pid)],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        assert "fusion-agent-studio test-probe" in out, out
    finally:
        proc.kill()
        proc.wait()


def test_start_sh_pgrep_matches_title_pattern():
    # acceptance criterion #490: start.sh process matching must accept the
    # post-setproctitle title (argv is rewritten on macOS)
    src = open("start.sh").read()
    pattern = 'pgrep -f "agent_runtime.daemon_server|fusion-agent-studio daemon"'
    assert src.count(pattern) == 2
