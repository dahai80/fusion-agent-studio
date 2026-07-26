"""Tests for AgentPackage and AgentManifest — .fusion-agent package system."""

import json
import pytest
import tempfile

from agent_runtime.agent_package import (
    AgentManifest,
    AgentPackage,
    FUSION_AGENT_DIR,
    MANIFEST_FILE,
    SOUL_FILE,
    MEMORY_FILE,
)


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def sample_manifest():
    return AgentManifest(
        name="test-agent",
        version="1.0.0",
        description="A test agent",
        model="qwen3.5-9b",
        system_prompt="You are a test agent.",
        temperature=0.5,
        max_tokens=2048,
        tools=["search", "calculator"],
        capabilities=["code", "search"],
        safety_level="L2",
        tags=["test", "demo"],
        author="test-author",
    )


class TestAgentManifest:
    def test_default_values(self):
        m = AgentManifest()
        assert m.name == ""
        assert m.version == "0.1.0"
        assert m.temperature == 0.7
        assert m.max_tokens == 4096
        assert m.tools == []
        assert m.safety_level == "L1"

    def test_to_dict(self, sample_manifest):
        d = sample_manifest.to_dict()
        assert d["name"] == "test-agent"
        assert d["version"] == "1.0.0"
        assert d["model"] == "qwen3.5-9b"
        assert d["tools"] == ["search", "calculator"]
        assert d["safety_level"] == "L2"

    def test_from_dict(self, sample_manifest):
        d = sample_manifest.to_dict()
        m = AgentManifest.from_dict(d)
        assert m.name == sample_manifest.name
        assert m.version == sample_manifest.version
        assert m.model == sample_manifest.model
        assert m.tools == sample_manifest.tools

    def test_roundtrip(self, sample_manifest):
        d = sample_manifest.to_dict()
        m = AgentManifest.from_dict(d)
        d2 = m.to_dict()
        assert d == d2

    def test_from_dict_missing_fields(self):
        m = AgentManifest.from_dict({"name": "minimal"})
        assert m.name == "minimal"
        assert m.version == "0.1.0"
        assert m.tools == []


class TestAgentPackageInit:
    def test_init_creates_structure(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        assert pkg.pkg_path.is_dir()
        assert (pkg.pkg_path / "knowledge").is_dir()
        assert (pkg.pkg_path / "skills").is_dir()
        assert pkg.manifest_path.exists()
        assert pkg.soul_path.exists()
        assert pkg.memory_path.exists()

    def test_init_with_manifest(self, tmp_dir, sample_manifest):
        pkg = AgentPackage(tmp_dir)
        pkg.init(manifest=sample_manifest)
        loaded = pkg.load_manifest()
        assert loaded.name == "test-agent"

    def test_init_with_soul(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init(soul="# Custom Soul\n\nBe helpful.")
        assert "Custom Soul" in pkg.load_soul()

    def test_exists_property(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        assert not pkg.exists
        pkg.init()
        assert pkg.exists


class TestAgentPackageManifest:
    def test_save_and_load(self, tmp_dir, sample_manifest):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_manifest(sample_manifest)
        loaded = pkg.load_manifest()
        assert loaded.name == "test-agent"
        assert loaded.model == "qwen3.5-9b"

    def test_load_missing_manifest(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        m = pkg.load_manifest()
        assert m.name == ""

    def test_overwrite_manifest(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_manifest(AgentManifest(name="v1"))
        pkg.save_manifest(AgentManifest(name="v2"))
        assert pkg.load_manifest().name == "v2"


class TestAgentPackageSoul:
    def test_save_and_load(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_soul("# My Soul\n\nBe wise.")
        assert "Be wise" in pkg.load_soul()

    def test_load_missing_soul(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        assert pkg.load_soul() == ""


class TestAgentPackageMemory:
    def test_save_and_load(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_memory("First memory")
        assert pkg.load_memory() == "First memory"

    def test_append_memory(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_memory("First")
        pkg.append_memory("Second")
        content = pkg.load_memory()
        assert "First" in content
        assert "Second" in content

    def test_append_to_empty(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.append_memory("First entry")
        assert pkg.load_memory() == "First entry"


class TestAgentPackageSkills:
    def test_save_and_list_skills(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_skill("search", {"type": "search", "engine": "duckduckgo"})
        skills = pkg.list_skills()
        assert "search" in skills

    def test_load_skill(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_skill("calc", {"type": "calculator", "precision": 2})
        skill = pkg.load_skill("calc")
        assert skill["type"] == "calculator"

    def test_load_missing_skill(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        assert pkg.load_skill("nonexistent") == {}

    def test_delete_skill(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_skill("temp", {"type": "temp"})
        assert pkg.delete_skill("temp")
        assert "temp" not in pkg.list_skills()

    def test_delete_missing_skill(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        assert not pkg.delete_skill("nonexistent")


class TestAgentPackageSystemPrompt:
    def test_soul_takes_precedence(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_manifest(AgentManifest(system_prompt="From manifest"))
        pkg.save_soul("# From soul")
        prompt = pkg.get_system_prompt()
        assert "From soul" in prompt

    def test_manifest_fallback(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        pkg.save_manifest(AgentManifest(system_prompt="From manifest"))
        pkg.save_soul("")
        prompt = pkg.get_system_prompt()
        assert prompt == "From manifest"


class TestAgentPackageToGraphConfig:
    def test_to_graph_config(self, tmp_dir, sample_manifest):
        pkg = AgentPackage(tmp_dir)
        pkg.init(manifest=sample_manifest)
        config = pkg.to_graph_config()
        assert config["name"] == "test-agent"
        assert config["model"] == "qwen3.5-9b"
        assert "search" in config["tools"]


class TestAgentPackageDestroy:
    def test_destroy(self, tmp_dir):
        pkg = AgentPackage(tmp_dir)
        pkg.init()
        assert pkg.exists
        pkg.destroy()
        assert not pkg.exists
