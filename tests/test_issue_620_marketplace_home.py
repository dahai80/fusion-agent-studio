import agent_runtime.agent_marketplace as am
from agent_runtime.agent_marketplace import MarketEntry


def test_store_dir_resolved_lazily(tmp_path, monkeypatch):
    # #620: FUSION_AGENT_STUDIO_HOME set after module import must be honored.
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path))
    m = am.AgentMarketplace()
    assert m.store_dir == tmp_path / "marketplace"


def test_module_import_does_not_freeze_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_AGENT_STUDIO_HOME", str(tmp_path))
    m = am.AgentMarketplace()
    entry = MarketEntry(id="e1", name="LazyBot", author="test")
    m.publish(entry)
    assert (tmp_path / "marketplace" / "index.json").exists()
