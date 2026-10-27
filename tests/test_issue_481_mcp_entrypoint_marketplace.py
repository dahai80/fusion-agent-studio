"""#481: MCP launch entrypoint + local server family + marketplace audit/tenant.

ACs:
1. Documented, client-reachable launch path for the MCP stdio server,
   verified with a standard initialize/tools-list handshake.
2. rag/model-hub/comfyui registries implemented with the same launch path.
3. Marketplace install/publish produce tenant-scoped audit records; tenant
   B cannot list/install tenant A's private packages.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mcp_handshake(registry: str) -> dict:
    # standard MCP client sequence over stdio, newline-delimited JSON-RPC
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "proof-client", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, "-m", "agent_runtime.mcp_server", "--registry", registry],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    by_id = {r["id"]: r for r in responses if "id" in r}
    assert by_id[1]["result"]["protocolVersion"] == "2024-11-05"
    return by_id[2]["result"]["tools"]


class TestMCPLaunchEntrypoint:
    def test_mlx_handshake_lists_tools(self):
        tools = _mcp_handshake("mlx")
        names = {t["name"] for t in tools}
        assert "mlx_chat" in names and "mlx_status" in names

    def test_rag_handshake_lists_tools(self):
        tools = _mcp_handshake("rag")
        names = {t["name"] for t in tools}
        assert {"kb_search", "kb_ask", "kb_list_bases"} <= names

    def test_model_hub_handshake_lists_tools(self):
        tools = _mcp_handshake("model-hub")
        names = {t["name"] for t in tools}
        assert {"modelhub_list", "modelhub_search"} <= names

    def test_comfyui_handshake_lists_tools(self):
        tools = _mcp_handshake("comfyui")
        names = {t["name"] for t in tools}
        assert {"comfy_status", "comfy_nodes"} <= names

    def test_help_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "-m", "agent_runtime.mcp_server", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0
        assert "--registry" in proc.stdout

    def test_invalid_registry_rejected(self):
        proc = subprocess.run(
            [sys.executable, "-m", "agent_runtime.mcp_server", "--registry", "nope"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode != 0

    def test_console_script_registered(self):
        content = (REPO_ROOT / "pyproject.toml").read_text()
        assert 'fusion-mcp = "agent_runtime.mcp_server:main"' in content


def _mp(tmp_path):
    from agent_runtime.agent_marketplace import AgentMarketplace, MarketEntry

    mp = AgentMarketplace(store_dir=tmp_path / "market")

    def entry(name, visibility="public"):
        return MarketEntry(
            name=name, graph_data={"nodes": [name]}, visibility=visibility
        )

    return mp, entry


class TestMarketplaceTenantIsolation:

    def test_private_entry_hidden_from_other_tenant(self, tmp_path):
        mp, entry = _mp(tmp_path)
        mp.publish(entry("secret-agent", "private"), tenant="tenantA")
        mp.publish(entry("open-agent"), tenant="tenantA")

        # tenant A sees both
        names_a = {e.name for e in mp.search(tenant="tenantA")}
        assert names_a == {"secret-agent", "open-agent"}
        # tenant B sees only the public one
        names_b = {e.name for e in mp.search(tenant="tenantB")}
        assert names_b == {"open-agent"}

    def test_tenant_b_cannot_get_or_install_private(self, tmp_path):
        mp, entry = _mp(tmp_path)
        entry_id = mp.publish(entry("secret-agent", "private"), tenant="tenantA")

        assert mp.get(entry_id, tenant="tenantB") is None
        assert mp.get(entry_id, tenant="tenantA") is not None
        assert mp.install(entry_id, target_dir=tmp_path / "out", tenant="tenantB") is None
        installed = mp.install(entry_id, target_dir=tmp_path / "out2", tenant="tenantA")
        assert installed is not None and Path(installed).exists()

    def test_tenant_b_cannot_unpublish_private(self, tmp_path):
        mp, entry = _mp(tmp_path)
        entry_id = mp.publish(entry("secret-agent", "private"), tenant="tenantA")
        assert mp.unpublish(entry_id, tenant="tenantB") is False
        assert mp.get(entry_id, tenant="tenantA") is not None
        assert mp.unpublish(entry_id, tenant="tenantA") is True

    def test_public_entry_visible_across_tenants(self, tmp_path):
        mp, entry = _mp(tmp_path)
        entry_id = mp.publish(entry("open-agent"), tenant="tenantA")
        assert mp.get(entry_id, tenant="tenantB") is not None
        assert mp.install(entry_id, target_dir=tmp_path / "out", tenant="tenantB") is not None


class TestMarketplaceAudit:
    def test_publish_install_produce_audit_records(self, tmp_path):
        mp, entry = _mp(tmp_path)
        entry_id = mp.publish(entry("audited-agent"), tenant="tenantA")
        mp.install(entry_id, target_dir=tmp_path / "out", tenant="tenantA")

        records = mp.audit_records(tenant="tenantA")
        events = [r["event"] for r in records]
        assert "publish" in events and "install" in events
        rec = next(r for r in records if r["event"] == "install")
        assert rec["entry_id"] == entry_id and rec["tenant"] == "tenantA"

    def test_refusals_audited(self, tmp_path):
        mp, entry = _mp(tmp_path)
        entry_id = mp.publish(entry("secret-agent", "private"), tenant="tenantA")
        mp.install(entry_id, target_dir=tmp_path / "out", tenant="tenantB")

        records_b = mp.audit_records(tenant="tenantB")
        assert any(r["event"] == "install_refused" for r in records_b)
        # tenant A's tail does not contain B's refused attempt
        records_a = mp.audit_records(tenant="tenantA")
        assert not any(r["event"] == "install_refused" for r in records_a)
        # admin view (tenant=None) sees all
        records_all = mp.audit_records()
        assert any(r["event"] == "install_refused" for r in records_all)

    def test_tampered_install_refused_and_audited(self, tmp_path):
        mp, entry = _mp(tmp_path)
        entry_id = mp.publish(entry("tamper-agent"), tenant="tenantA")
        # tamper after publish
        e = mp.get(entry_id, tenant="tenantA")
        e.graph_data = {"nodes": ["tampered"]}
        assert mp.install(entry_id, target_dir=tmp_path / "out", tenant="tenantA") is None
        records = mp.audit_records(tenant="tenantA")
        refused = [r for r in records if r["event"] == "install_refused"]
        assert any("signature mismatch" in r.get("detail", "") for r in refused)
