"""Local Claude credentials and connection status; no secrets in public responses.

Only the OS credential vault or process memory stores keys. Environment credentials
remain an explicit developer option. Provider exceptions are never shown verbatim.
"""
from __future__ import annotations

import os
import threading

from . import settings

SERVICE = "R Cubed"
ACCOUNT = "anthropic-api-key"
_lock = threading.RLock()
_session_key: str | None = None
_connection = "untested"


class ProviderError(ValueError):
    """A fixed, secret-free error suitable for the UI."""


def _vault():
    try:
        import keyring
        backend = keyring.get_keyring()
        candidates = list(getattr(backend, "backends", [])) or [backend]
        for candidate in candidates:
            module = type(candidate).__module__
            if module in {"keyring.backends.Windows", "keyring.backends.macOS",
                          "keyring.backends.SecretService", "keyring.backends.kwallet"} and candidate.priority > 0:
                return candidate
    except Exception:
        pass
    raise ProviderError("Secure storage is unavailable. Choose session-only storage or unlock your system credential store.")


def _key(value: str) -> str:
    if not isinstance(value, str):
        raise ProviderError("Enter your Claude API key.")
    value = value.strip()
    if not (12 <= len(value) <= 4096) or any(c.isspace() for c in value) or not value.isascii():
        raise ProviderError("Enter a valid Claude API key without spaces.")
    return value


def _credential() -> tuple[str | None, str]:
    with _lock:
        if _session_key:
            return _session_key, "session"
        if settings.load()["credential_disabled"]:
            return None, "none"
        try:
            value = _vault().get_password(SERVICE, ACCOUNT)
            if value:
                return value, "system"
        except Exception:
            pass
        value = os.environ.get("ANTHROPIC_API_KEY")
        return (value, "environment") if value else (None, "none")


def status() -> dict:
    values = settings.load()
    with _lock:
        key, source = _credential()
        try:
            _vault()
            storage = True
        except ProviderError:
            storage = False
        return {"configured": bool(key), "credential_source": source, "storage_available": storage,
                "connection": "offline" if not settings.network_allowed() else (_connection if key else "not_configured"),
                "status": "offline" if not settings.network_allowed() else (_connection if key else "not_configured"),
                **{k: values[k] for k in settings.PUBLIC_KEYS},
                "network_enabled": settings.network_allowed(),
                "sourcing_enabled": settings.sourcing_enabled(), "telemetry_enabled": settings.telemetry_enabled(),
                "calibration_enabled": False}


def _forget_stored_key() -> bool:
    try:
        backend = _vault()
    except ProviderError:
        return False
    try:
        if backend.get_password(SERVICE, ACCOUNT) is not None:
            backend.delete_password(SERVICE, ACCOUNT)
        return True
    except Exception:
        raise ProviderError("Claude is disconnected, but the saved key could not be removed. Unlock your credential store and try Remove again.") from None


def save_key(key: str, storage: str = "system", model: str | None = None) -> dict:
    global _session_key, _connection
    value = _key(key)
    if storage not in {"system", "session"}:
        raise ProviderError("Choose system or session-only storage.")
    changes = {"credential_disabled": storage == "session"}
    if model is not None:
        try:
            settings._validate({"model": model})
        except ValueError:
            raise ProviderError("Enter a valid Claude model ID, not a credential.") from None
        changes["model"] = model
    removed = True
    with _lock:
        if storage == "system":
            try:
                _vault().set_password(SERVICE, ACCOUNT, value)
            except ProviderError:
                raise
            except Exception:
                raise ProviderError("The key could not be saved securely. Unlock your credential store or choose session-only storage.") from None
            _session_key = None
        else:
            # A prior persistent key must not reappear unexpectedly after restart.
            settings.save({"credential_disabled": True}, internal=True)
            _session_key = None
            try:
                removed = _forget_stored_key()
            except ProviderError:
                # The persistent key is already masked. A locked vault must not
                # prevent using a new key only for the lifetime of this process.
                removed = False
            _session_key = value
        settings.save(changes, internal=True)
        _connection = "untested"
    result = status()
    if not removed:
        result["warning"] = "Using a session-only key. A previously saved key could not be checked while secure storage is unavailable; it is disabled."
    return result


def remove_key() -> dict:
    global _session_key, _connection
    with _lock:
        settings.save({"credential_disabled": True}, internal=True)
        _session_key = None
        _connection = "not_configured"
        removed = _forget_stored_key()
    result = status()
    if not removed:
        result["warning"] = "Claude disconnected. Secure storage is unavailable; unlock it and use Remove again to delete any previously saved key."
    return result


def update_preferences(changes: dict) -> dict:
    global _connection
    try:
        settings.save(changes)
    except ValueError:
        raise ProviderError("Invalid preferences. Use a model ID and true/false network options; never put credentials in preferences.") from None
    if "model" in changes:
        _connection = "untested"
    return status()


def model() -> str:
    return settings.load()["model"]


def client():
    if not settings.network_allowed():
        raise ProviderError("Network access is disabled. Using simple offline rules.")
    key, _ = _credential()
    if not key:
        raise ProviderError("Claude is not connected. Add a key in Settings, or use simple offline rules.")
    import anthropic
    return anthropic.Anthropic(api_key=key, timeout=30.0, max_retries=0)


def error_message(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ProviderError):
        return "unavailable", str(exc)
    status_code = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if status_code in (401, 403):
        return "invalid_key", "Claude rejected the key or its permissions. Replace it in Settings."
    if status_code == 404:
        return "model_unavailable", "The selected Claude model is unavailable. Choose another model in Settings."
    if status_code == 429:
        return "rate_limited", "Claude is rate limited or the account has reached its allowance. Try again later."
    if "Timeout" in name or "Connection" in name:
        return "offline", "Cannot reach Claude. Check your connection and try again."
    return "provider_error", "Claude could not complete the request. Check your connection and model in Settings."


def note_error(exc: Exception) -> str:
    global _connection
    code, message = error_message(exc)
    with _lock:
        _connection = code
    return message


def close_client(api) -> None:
    if api is not None:
        try:
            api.close()
        except Exception:
            pass


def test_connection(key: str | None = None, model: str | None = None) -> dict:
    global _connection
    if not settings.network_allowed():
        return {"ok": False, "status": "offline", "message": "Enable network access to test Claude.", "models": []}
    models = []
    try:
        import anthropic
        value = _key(key) if key else _credential()[0]
        if not value:
            raise ProviderError("Enter a key or connect Claude first.")
        selected = model or settings.load()["model"]
        settings._validate({"model": selected})
        with anthropic.Anthropic(api_key=value, timeout=15.0, max_retries=0) as api:
            # No generation request or design data is sent by this connection test.
            page = api.models.list(limit=100)
            models = [{"id": item.id, "display_name": getattr(item, "display_name", item.id)} for item in page.data]
            found = any(item["id"] == selected for item in models)
            if not found:
                api.models.retrieve(selected)
        result = {"ok": True, "status": "connected", "message": "Key and model access verified. Design generation has not been tested.", "models": models}
    except Exception as exc:
        code, message = error_message(exc)
        result = {"ok": False, "status": code, "message": message, "models": models}
    if key is None and (model is None or model == settings.load()["model"]):
        with _lock:
            _connection = result["status"]
    return result
