"""Non-secret preferences: what Morph may send over the network, whether Keewano history may tune a design and whether
agents may run with tools (beta).

Stored as JSON in MORPH_HOME/.settings.json (the projects folder, git-ignored). API keys never go here: they
live in .env (morph/keys.py). Standard library only. MORPH_OFFLINE=1 turns every network call off whatever
the file says; MORPH_TELEMETRY_ENABLED=1 opts a scripted run into Keewano events.
"""

from __future__ import annotations

import json
import os
import threading

DEFAULTS = {
    "network_enabled": True,       # Claude chat, Darwin, SubQ, BoardRepo and Keewano may be called at all
    "sourcing_enabled": True,      # Darwin supplier lookups (Find agents, `morph source`)
    "telemetry_enabled": False,    # send build events to Keewano: opt-in
    "calibration_enabled": False,  # scale grip-force targets by Keewano build history: opt-in, demo data unless real
    "agent_tools_enabled": True,   # agents may run with tools (beta): Live prices from the BOM tab, keys asked for in cards
    "auto_price_check": False,     # start Live prices after a build changes the purchased parts: opt-in (it uses Claude)
}
LABELS = {"network_enabled": "network lookups", "sourcing_enabled": "Darwin sourcing",
          "telemetry_enabled": "Keewano events", "calibration_enabled": "Keewano calibration",
          "agent_tools_enabled": "agent tools", "auto_price_check": "price checks after builds"}
_LOCK = threading.Lock()


def home() -> str:
    return os.environ.get("MORPH_HOME") or "projects"


def path() -> str:
    return os.path.join(home(), ".settings.json")


PROBLEM = ("Morph couldn't read its settings file ({file}: damaged, or edited by hand), so every switch below is off, "
           "network lookups included, until you set them again.")


def _raw():
    """The settings file's object; None when there is no file; False when it is there but can't be read."""
    try:
        with open(path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return False
    return raw if isinstance(raw, dict) else False


def load() -> dict:
    """The saved settings over the defaults; unknown keys in the file are ignored. A file that is there but can't be
    read turns every switch off (fail closed: a switch someone turned off must never come back on by itself), and so
    does a value that isn't true or false. problem() says why for the Keys dialog."""
    raw = _raw()
    if raw is None:
        return dict(DEFAULTS)
    if raw is False:
        return {k: False for k in DEFAULTS}
    return {k: (raw[k] if isinstance(raw[k], bool) else False) if k in raw else v for k, v in DEFAULTS.items()}


def problem() -> str | None:
    """Why the switches are all off although nobody turned them off (the settings file can't be read), or None."""
    return PROBLEM.format(file=os.path.basename(path())) if _raw() is False else None


def save(changes) -> dict:
    """Apply {setting: true|false} and write the file atomically. Raises ValueError for anything else,
    OSError when the projects folder can't be written."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send the settings to change as {\"values\": {\"setting\": true}}.")
    for k, v in changes.items():
        if k not in DEFAULTS:
            raise ValueError(f"unknown setting {k}")
        if not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false")
    from .services.durable import write_json
    with _LOCK:
        values = {**load(), **changes}
        write_json(path(), values, indent=2)  # synced: a power cut leaves the old settings or the new, never an empty file
    return values


def describe(values: dict | None = None) -> str:
    """One line for `morph doctor`: "network lookups on, Darwin sourcing on, Keewano events off, ..." (and why, when
    the settings file can't be read)."""
    values = values or load()
    line = ", ".join(f"{LABELS[k]} {'on' if values[k] else 'off'}" for k in DEFAULTS)
    why = problem()
    return f"{line} ({why})" if why else line


def offline() -> bool:
    return os.environ.get("MORPH_OFFLINE", "").strip().lower() in ("1", "true", "yes")


def network_allowed() -> bool:
    return not offline() and load()["network_enabled"]


def sourcing_enabled() -> bool:
    return network_allowed() and load()["sourcing_enabled"]


def telemetry_enabled() -> bool:
    return network_allowed() and (load()["telemetry_enabled"] or os.environ.get("MORPH_TELEMETRY_ENABLED") == "1")


def calibration_enabled() -> bool:
    return network_allowed() and load()["calibration_enabled"]


def agent_tools_enabled() -> bool:
    return network_allowed() and load()["agent_tools_enabled"]
