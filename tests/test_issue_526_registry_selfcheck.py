"""#526: start.sh register-then-start self-check against port-registry.yaml."""
import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extract_check_fn():
    res = subprocess.run(
        ["sed", "-n", r"/^check_port_registry()/,/^}/p", f"{REPO}/start.sh"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "check_port_registry()" in res.stdout, "check_port_registry missing from start.sh"
    return res.stdout


def _run_check(registry, port="11442", env_extra=None, tmp_home=None):
    log_shims = "\n".join(
        [
            "log_info()  { printf '[INFO] %s\\n' \"$*\"; }",
            "log_warn()  { printf '[WARN] %s\\n' \"$*\" >&2; }",
            "log_error() { printf '[ERROR] %s\\n' \"$*\" >&2; }",
            _extract_check_fn(),
            f"check_port_registry {port}",
        ]
    )
    env = dict(os.environ)
    env.pop("FUSION_SKIP_REGISTRY_CHECK", None)
    env.pop("FUSION_REGISTRY", None)
    if tmp_home is not None:
        env["HOME"] = str(tmp_home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", log_shims],
        capture_output=True,
        text=True,
        env=env,
    )


def _write_registry(path, entries):
    lines = ["services:"]
    for name, port in entries:
        lines.append(f"  {name}:")
        lines.append(f"    port: {port}")
        lines.append("    purpose: test")
    path.write_text("\n".join(lines) + "\n")


def test_start_sh_calls_check_inside_start():
    src = open(f"{REPO}/start.sh").read()
    assert "if ! check_port_registry" in src
    # call site must live inside start() (before daemon launch)
    start_body = src.split("start() {", 1)[1].split("\nstop()", 1)[0]
    assert "check_port_registry" in start_body


def test_refuses_when_port_missing_from_registry(tmp_path):
    reg = tmp_path / "port-registry.yaml"
    _write_registry(reg, [("some-other-service", "9999")])
    res = _run_check(reg, env_extra={"FUSION_REGISTRY": str(reg)})
    assert res.returncode == 1
    assert "refusing to start" in res.stderr


def test_refuses_when_registered_with_wrong_port(tmp_path):
    reg = tmp_path / "port-registry.yaml"
    _write_registry(reg, [("fusion-agent-studio", "11555")])
    res = _run_check(reg, env_extra={"FUSION_REGISTRY": str(reg)})
    assert res.returncode == 1
    assert "refusing to start" in res.stderr


def test_passes_when_entry_present(tmp_path):
    reg = tmp_path / "port-registry.yaml"
    _write_registry(reg, [("fusion-agent-studio", "11442")])
    res = _run_check(reg, env_extra={"FUSION_REGISTRY": str(reg)})
    assert res.returncode == 0
    assert "refusing" not in res.stderr


def test_bypass_env_allows_unregistered(tmp_path):
    reg = tmp_path / "port-registry.yaml"
    _write_registry(reg, [])
    res = _run_check(
        reg,
        env_extra={"FUSION_REGISTRY": str(reg), "FUSION_SKIP_REGISTRY_CHECK": "1"},
    )
    assert res.returncode == 0
    assert "bypassed" in res.stderr


def test_missing_registry_file_warns_and_continues(tmp_path):
    reg = tmp_path / "nope" / "port-registry.yaml"
    res = _run_check(reg, env_extra={"FUSION_REGISTRY": str(reg)})
    assert res.returncode == 0
    assert "self-check skipped" in res.stderr


def test_default_registry_path_under_home(tmp_path):
    # FUSION_REGISTRY unset: falls back to $HOME/fusion/architecture/port-registry.yaml
    home = tmp_path / "home"
    (home / "fusion" / "architecture").mkdir(parents=True)
    reg = home / "fusion" / "architecture" / "port-registry.yaml"
    _write_registry(reg, [("fusion-agent-studio", "11442")])
    res = _run_check(reg, tmp_home=home)
    assert res.returncode == 0


@pytest.mark.skipif(
    not os.path.exists(
        os.path.expanduser("~/fusion/architecture/port-registry.yaml")
    ),
    reason="real workspace registry not present",
)
def test_real_workspace_registry_passes():
    res = _run_check(None)
    assert res.returncode == 0
