import logging

import pytest

from agent_runtime import ecom_client


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ecom_client._ECOM_KEY_ENV, raising=False)
    monkeypatch.delenv(ecom_client._ECOM_KEY_LEGACY_ENV, raising=False)
    monkeypatch.setattr(ecom_client, "_legacy_key_warned", False)


def test_default_key_empty():
    assert ecom_client._bridge_key() == ""


def test_standard_key_used_without_warning(monkeypatch, caplog):
    monkeypatch.setenv(ecom_client._ECOM_KEY_ENV, "new-key")
    with caplog.at_level(logging.WARNING, logger=ecom_client.__name__):
        assert ecom_client._bridge_key() == "new-key"
    assert not any("deprecated" in r.message for r in caplog.records)


def test_legacy_key_used_with_deprecation_warning(monkeypatch, caplog):
    monkeypatch.setenv(ecom_client._ECOM_KEY_LEGACY_ENV, "old-key")
    with caplog.at_level(logging.WARNING, logger=ecom_client.__name__):
        assert ecom_client._bridge_key() == "old-key"
    assert any(
        f"{ecom_client._ECOM_KEY_LEGACY_ENV} is deprecated" in r.message for r in caplog.records
    )


def test_legacy_warning_emitted_once(monkeypatch, caplog):
    monkeypatch.setenv(ecom_client._ECOM_KEY_LEGACY_ENV, "old-key")
    with caplog.at_level(logging.WARNING, logger=ecom_client.__name__):
        assert ecom_client._bridge_key() == "old-key"
        assert ecom_client._bridge_key() == "old-key"
    legacy_warnings = [r for r in caplog.records if "is deprecated" in r.message]
    assert len(legacy_warnings) == 1


def test_standard_key_wins_over_legacy(monkeypatch, caplog):
    monkeypatch.setenv(ecom_client._ECOM_KEY_ENV, "new-key")
    monkeypatch.setenv(ecom_client._ECOM_KEY_LEGACY_ENV, "old-key")
    with caplog.at_level(logging.WARNING, logger=ecom_client.__name__):
        assert ecom_client._bridge_key() == "new-key"
    assert not any("deprecated" in r.message for r in caplog.records)


def test_headers_use_resolved_key(monkeypatch):
    monkeypatch.setenv(ecom_client._ECOM_KEY_ENV, "new-key")
    client = ecom_client.EcomClient()
    assert client._headers() == {"Authorization": "Bearer new-key"}


def test_headers_empty_without_key():
    client = ecom_client.EcomClient()
    assert client._headers() == {}
