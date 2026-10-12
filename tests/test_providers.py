"""Credential lifecycle and privacy boundaries, without real keys or provider calls."""
import json
from types import SimpleNamespace

import pytest

from morph import providers, settings


class Vault:
    def __init__(self):
        self.value = None

    def set_password(self, service, account, value):
        assert (service, account) == (providers.SERVICE, providers.ACCOUNT)
        self.value = value

    def get_password(self, *args):
        return self.value

    def delete_password(self, *args):
        self.value = None


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setenv("MORPH_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("MORPH_OFFLINE", raising=False)
    monkeypatch.delenv("MORPH_TELEMETRY_ENABLED", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(providers, "_session_key", None)
    monkeypatch.setattr(providers, "_connection", "untested")
    vault = Vault()
    monkeypatch.setattr(providers, "_vault", lambda: vault)
    return tmp_path, vault


def test_system_key_never_appears_in_preferences_or_public_status(local):
    folder, vault = local
    secret = "sk-ant-test-never-print-this"
    result = providers.save_key(secret, model="claude-test-model")
    assert vault.value == secret and result["configured"]
    assert result["credential_source"] == "system"
    assert secret not in json.dumps(result)
    assert secret not in (folder / "settings.json").read_text()
    providers.remove_key()
    assert vault.value is None and not providers.status()["configured"]


def test_session_key_does_not_reappear_after_restart(local, monkeypatch):
    folder, vault = local
    vault.value = "sk-ant-previous-persistent"
    providers.save_key("sk-ant-session-only-test", storage="session")
    assert vault.value is None
    assert providers.status()["credential_source"] == "session"
    assert "sk-ant-" not in (folder / "settings.json").read_text()
    monkeypatch.setattr(providers, "_session_key", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-developer-env-test")
    assert not providers.status()["configured"]


def test_remove_masks_environment_key_and_remains_disconnected(local, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-developer-env-test")
    assert providers.status()["credential_source"] == "environment"
    providers.remove_key()
    assert not providers.status()["configured"]
    assert settings.load()["credential_disabled"]


def test_secure_storage_failure_has_no_plaintext_fallback(local, monkeypatch):
    folder, _ = local
    def unavailable():
        raise providers.ProviderError("Secure storage is unavailable.")
    monkeypatch.setattr(providers, "_vault", unavailable)
    with pytest.raises(providers.ProviderError):
        providers.save_key("sk-ant-private-key-test")
    assert not list(folder.glob("*"))
    result = providers.save_key("sk-ant-private-key-test", storage="session")
    assert result["credential_source"] == "session"
    removed = providers.remove_key()
    assert not removed["configured"] and "warning" in removed


def test_plaintext_keyring_backend_is_rejected(tmp_path, monkeypatch):
    import keyring
    unsafe = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file", "priority": 1})()
    monkeypatch.setattr(keyring, "get_keyring", lambda: unsafe)
    with pytest.raises(providers.ProviderError, match="Secure storage"):
        providers._vault()


def test_locked_vault_allows_session_fallback_and_masks_old_key(local, monkeypatch):
    _, vault = local
    vault.value = "sk-ant-old-saved-key"
    def locked(*args):
        raise RuntimeError("credential store locked")
    monkeypatch.setattr(vault, "delete_password", locked)
    result = providers.save_key("sk-ant-new-session-key", storage="session")
    assert result["credential_source"] == "session" and "warning" in result
    assert vault.value == "sk-ant-old-saved-key"
    monkeypatch.setattr(providers, "_session_key", None)
    assert not providers.status()["configured"]


def test_research_closes_client_and_sanitizes_failure(local, monkeypatch):
    from morph import agent
    closed = []
    def rejected(**kwargs):
        raise type("Unauthorized", (Exception,), {"status_code": 401})("sk-ant-never-expose")
    api = SimpleNamespace(messages=SimpleNamespace(create=rejected), close=lambda: closed.append(True))
    monkeypatch.setattr(providers, "client", lambda: api)
    result = agent.research("find a motor")
    assert closed == [True] and "rejected" in result and "sk-ant" not in result


@pytest.mark.parametrize("model", ["sk-ant-this-is-a-secret", "kwpat-private-token", "a/b", "", "x" * 129])
def test_model_field_cannot_store_secrets_or_paths(local, model):
    folder, vault = local
    with pytest.raises(ValueError):
        providers.save_key("sk-ant-private-key-test", model=model)
    assert vault.value is None and not list(folder.glob("*"))


def test_unknown_or_wrongly_typed_preferences_are_rejected(local):
    with pytest.raises(ValueError):
        providers.update_preferences({"api_key": "secret"})
    with pytest.raises(ValueError):
        providers.update_preferences({"telemetry_enabled": "true"})
    assert not settings.telemetry_enabled() and not settings.sourcing_enabled()


def test_offline_mode_prevents_client_creation_and_key_test(local, monkeypatch):
    import anthropic
    providers.save_key("sk-ant-private-key-test", storage="session")
    providers.update_preferences({"network_enabled": False, "sourcing_enabled": True, "telemetry_enabled": True})
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: pytest.fail("network client created while offline"))
    with pytest.raises(providers.ProviderError, match="Network access"):
        providers.client()
    assert not providers.test_connection()["ok"]
    assert not settings.telemetry_enabled() and not settings.sourcing_enabled()


def test_provider_exception_does_not_leak_request_or_key(local, monkeypatch):
    import anthropic
    class Rejected(Exception):
        status_code = 401
    def rejected(**kwargs):
        raise Rejected("request contains sk-ant-secret-must-not-leak")
    monkeypatch.setattr(anthropic, "Anthropic", rejected)
    result = providers.test_connection(key="sk-ant-private-key-test")
    assert result["status"] == "invalid_key"
    assert "sk-ant" not in json.dumps(result)


def test_connection_checks_model_without_sending_design(local, monkeypatch):
    import anthropic
    seen = {}
    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.models = SimpleNamespace(list=lambda **kw: SimpleNamespace(data=[SimpleNamespace(id="test-model", display_name="Test model")]))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    result = providers.test_connection(key="sk-ant-private-key-test", model="test-model")
    assert result["ok"] and "generation has not been tested" in result["message"]
    assert seen["max_retries"] == 0
    assert not providers.status()["configured"]  # testing never saves a key


def test_unsaved_model_test_does_not_change_saved_connection_status(local, monkeypatch):
    import anthropic
    providers.save_key("sk-ant-private-key-test", storage="session", model="saved-model")
    class Client:
        def __init__(self, **kwargs):
            self.models = SimpleNamespace(list=lambda **kw: SimpleNamespace(data=[SimpleNamespace(id="another-model", display_name="Another")]))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr(anthropic, "Anthropic", Client)
    assert providers.test_connection(model="another-model")["ok"]
    assert providers.status()["connection"] == "untested"


def test_design_chat_reports_provider_failure_when_using_rules(local, monkeypatch):
    from morph.studio import intent
    from morph.spec import Design
    def invalid():
        exc = type("Unauthorized", (Exception,), {"status_code": 401})("sk-ant-do-not-leak")
        raise exc
    monkeypatch.setattr(providers, "client", invalid)
    result = intent.interpret("use an SG90 servo instead", Design())
    assert result["source"] == "rules" and result["changes"]
    assert "rejected" in result["reply"] and "sk-ant-" not in result["reply"]
