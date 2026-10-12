"""Non-secret local preferences. Credentials never belong in this file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import threading

from platformdirs import user_config_path, user_data_path

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULTS = {"model": DEFAULT_MODEL, "network_enabled": True, "sourcing_enabled": False,
            "telemetry_enabled": False, "onboarding_complete": False, "credential_disabled": False}
PUBLIC_KEYS = frozenset(DEFAULTS) - {"credential_disabled"}
_lock = threading.RLock()


def config_dir() -> Path:
    return Path(os.environ.get("MORPH_CONFIG_DIR") or user_config_path("RCubed", appauthor=False))


def projects_dir() -> Path:
    return Path(os.environ.get("MORPH_HOME") or (user_data_path("RCubed", appauthor=False) / "projects"))


def _validate(changes: dict, internal: bool = False) -> dict:
    if not isinstance(changes, dict) or set(changes) - (set(DEFAULTS) if internal else PUBLIC_KEYS):
        raise ValueError("Unknown preference.")
    clean = {}
    for key, value in changes.items():
        if key == "model":
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value) or value.lower().startswith(("sk-", "kwpat-")):
                raise ValueError("Enter a valid Claude model ID.")
        elif not isinstance(value, bool):
            raise ValueError("Preferences must be true or false.")
        clean[key] = value
    return clean


def load() -> dict:
    with _lock:
        values = dict(DEFAULTS)
        try:
            raw = json.loads((config_dir() / "settings.json").read_text(encoding="utf-8"))
            # Unknown keys are not copied forward (in particular accidental secrets).
            for key in DEFAULTS:
                if isinstance(raw, dict) and key in raw:
                    try:
                        values.update(_validate({key: raw[key]}, internal=True))
                    except ValueError:
                        pass
        except (OSError, ValueError):
            pass
        return values


def save(changes: dict, *, internal: bool = False) -> dict:
    clean = _validate(changes, internal)
    with _lock:
        values = load()
        values.update(clean)
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(values, fh, indent=2)
                fh.write("\n")
            os.replace(name, directory / "settings.json")
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return values


def network_allowed() -> bool:
    return os.environ.get("MORPH_OFFLINE", "").lower() not in {"1", "true", "yes"} and load()["network_enabled"]


def sourcing_enabled() -> bool:
    return network_allowed() and load()["sourcing_enabled"]


def telemetry_enabled() -> bool:
    return network_allowed() and (load()["telemetry_enabled"] or os.environ.get("MORPH_TELEMETRY_ENABLED") == "1")
