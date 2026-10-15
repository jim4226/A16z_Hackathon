"""API keys for the integrations: what each one is for, whether it is set, and saving new ones.

Keys live in the git-ignored `.env` next to the launchers (an installed wheel uses the user's settings folder,
resources.config_dir(), never site-packages) and in the process environment. Nothing here ever returns a key's
value: the Studio only learns whether each key is set. Everything works without keys (Claude chat falls back to
the built-in rules, Keewano switches itself off, Darwin Search needs no key), so keys only add capabilities.

Besides the built-in services' keys (KEYS), the tools agents use (beta) bring their own: one entry per key a tool that
is on needs, generated from its manifest (morph/studio/toolbox/catalog.py), named MORPH_TOOL_<NAME>, checked against
the manifest's pattern when saved and sent only to that tool's own hosts.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from . import resources

ENV_PATH = os.path.join(resources.config_dir(), ".env")
TEMPLATE = resources.path("env.example")

KEYS = [
    {"name": "ANTHROPIC_API_KEY", "service": "Claude", "label": "Anthropic API key",
     "what": "One key for every Claude feature in this section, billed to your Anthropic account.",
     "where": "console.anthropic.com, Settings, API Keys", "url": "https://console.anthropic.com/settings/keys"},
    {"name": "KEEWANO_API_KEY", "service": "Keewano", "label": "Project key (type SDK)",
     "what": "Sends revisions, edits and bench measurements to your Keewano project, once Send build events to Keewano is on below.",
     "where": "Keewano, Settings, Project, API Keys (choose type SDK)", "url": "https://keewano.com"},
    {"name": "KEEWANO_PAT", "service": "Keewano", "label": "Personal access token (kwpat-...)",
     "what": "Reads the project's build history: for Test keys and the optional grip-force calibration.",
     "where": "Keewano, Settings, Personal, Security", "url": "https://keewano.com"},
    {"name": "KEEWANO_PROJECT_ID", "service": "Keewano", "label": "Project ID",
     "what": "Which Keewano project to read.", "where": "Keewano, Settings, Details, Project ID", "url": "https://keewano.com"},
    {"name": "SUBQ_API_KEY", "service": "SubQ", "label": "SubQ API key (sk-...)",
     "what": "Quick yes/no and multiple-choice decisions, such as sorting out what a one-line request asks for, shown as "
             "\"SubQ Snap (AI)\". Without it Morph uses its built-in rules.",
     "where": "console.subq.ai, API keys", "url": "https://console.subq.ai"},
]
NAMES = [k["name"] for k in KEYS]
TOOL_KEY = re.compile(r"MORPH_TOOL_[A-Z0-9_]{2,40}")
_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_LOCK = threading.Lock()  # two Save clicks must not interleave their writes


def read_text(path: str) -> str:
    """A .env as text, whatever editor wrote it: UTF-8 (with or without BOM), UTF-16 from
    PowerShell 5.1 `>`/Out-File, an "ANSI" (cp1252) file from Notepad, or a mix from `>>`."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    return text.replace("\x00", "")


def parse(text: str) -> list:
    """[(KEY, value)] from .env text: comments, blank lines, `export`, quotes and `  # comment` handled."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if v[:1] not in ('"', "'"):
            v = v.split(" #", 1)[0].strip()
        out.append((k, v.strip('"').strip("'")))
    return out


def load_env(path: str | None = None) -> list:
    """KEY=value lines from .env into the environment without overriding anything already set.
    Returns the names it set."""
    path = path or ENV_PATH
    if not os.path.isfile(path):
        return []
    loaded = []
    for k, v in parse(read_text(path)):
        if k and v and k not in os.environ:
            os.environ[k] = v
            loaded.append(k)
    return loaded


def _write_private(path: str, text: str):
    """Replace `path` with `text`, readable by this user only (0600): written to a fresh private temp file
    next to it, then renamed over it, so a crash never leaves half a file. Raises OSError (read-only folder)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)  # an installed wheel's settings folder may not exist yet
    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)  # mkstemp already made it 0600; this also covers a umask or an older file
    except OSError:
        pass


def ensure_env_file(path: str | None = None) -> bool:
    """Create .env from env.example on first run, so there is one obvious place to put keys. An existing .env that
    other users of the computer can read (Morph before 0.2 wrote it so) is made private (0600)."""
    path = path or ENV_PATH
    if os.path.exists(path):
        try:
            if os.name != "nt" and os.stat(path).st_mode & 0o077:
                os.chmod(path, 0o600)
        except OSError:
            pass
        return False
    if not os.path.isfile(TEMPLATE):
        return False
    with open(TEMPLATE, encoding="utf-8") as src:
        _write_private(path, src.read())
    return True


def tool_keys(home: str | None = None) -> list:
    """The keys of the tools that are on, from their manifests: service, label, what it unlocks, where to get it, cost,
    privacy, the hosts it is sent to and the pattern it must fit (`tool: true`). Never a value. A tool key still saved
    while no tool that is on uses it (its tools turned off, or its manifest gone) is listed too, `unused: true`, so it
    can be removed."""
    from .studio.toolbox import catalog, store
    out = catalog.key_entries(home)
    listed, specs = {e["name"] for e in out}, catalog.key_specs(home)
    for name in sorted(n for n, v in store.env_items(os.environ) if v and TOOL_KEY.fullmatch(n) and n not in listed):
        c = specs.get(name)
        e = c.entry() if c else {"name": name, "service": "Keys no tool uses", "label": name, "where": "", "url": "", "cost": "",
                                 "privacy": "", "hosts": [], "pattern": "", "prefix": "", "provider": None, "curated": False,
                                 "tool": True}
        e.update(unused=True, tools=[], used_by=[], what="Its tools are turned off, so Morph sends it nowhere. Remove it, or "
                 "turn one of its tools back on." if c else "No tool in Morph's catalog uses this key any more, so Morph sends "
                 "it nowhere. Remove it.")
        out.append(e)
    return out


def env_file() -> dict:
    """Where keys are saved, for the Keys dialog: the file's name, its folder in words (a checkout: the Morph folder,
    where both launchers are; an installed wheel: the settings folder's path) and whether git ignores it."""
    folder = os.path.dirname(os.path.abspath(ENV_PATH))
    root = resources.checkout()
    same = bool(root) and os.path.normcase(os.path.realpath(folder)) == os.path.normcase(os.path.realpath(root))
    return {"file": os.path.basename(ENV_PATH), "folder": "the Morph folder" if same else folder, "git_ignored": same}


def status(home: str | None = None) -> list:
    """Each key's description and whether it is set: the built-in services, then the tools' keys. Never the value."""
    return [{**k, "set": bool(os.environ.get(k["name"]))} for k in [*KEYS, *tool_keys(home)]]


def save(updates: dict, path: str | None = None, home: str | None = None) -> list:
    """Write keys into .env (other lines and comments kept; the file readable by this user only) and apply them
    to this process at once. An empty value removes the key. Returns the names changed. Raises ValueError for
    anything that is not a key, OSError when .env can't be written. A tool's key (MORPH_TOOL_*) must be declared by a
    loaded manifest and fit its pattern; the error never repeats the value."""
    path = path or ENV_PATH
    clean, specs = {}, None
    if not isinstance(updates or {}, dict):
        raise ValueError("send the keys as {NAME: value}")
    for k, v in (updates or {}).items():
        if k not in NAMES and not (isinstance(k, str) and TOOL_KEY.fullmatch(k)):
            raise ValueError(f"unknown key {str(k)[:60]}")
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{k}: that does not look like a key")
        v = (v or "").strip()
        if k not in NAMES:
            if specs is None:
                from .studio.toolbox import catalog
                specs = catalog.key_specs(home)
            spec = specs.get(k)
            if spec is None and v:  # removing one is always allowed: its manifest may be gone
                raise ValueError(f"unknown key {k}: no tool in the catalog uses it")
            if v and not spec.valid(v):
                raise ValueError(f"{spec.label}: that does not look like a {spec.service} key")
            clean[k] = v
            continue
        if len(v) > 500 or any(c in v for c in "\r\n\x00\"'"):
            raise ValueError(f"{k}: that does not look like a key")
        clean[k] = v
    if not clean:
        return []
    with _LOCK:
        if os.path.isfile(path):
            lines = read_text(path).splitlines()
        elif os.path.isfile(TEMPLATE):
            with open(TEMPLATE, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        else:
            lines = []
        out, done = [], set()
        for line in lines:
            m = _LINE.match(line)
            if m and m.group(1) in clean:
                if m.group(1) not in done:
                    out.append(f"{m.group(1)}={clean[m.group(1)]}")
                    done.add(m.group(1))
                continue  # drop duplicate lines for the same key
            out.append(line)
        out += [f"{k}={v}" for k, v in clean.items() if k not in done]
        _write_private(path, "\n".join(out).rstrip("\n") + "\n")
        for k, v in clean.items():
            if v:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
    return sorted(clean)


def _claude_error(exc: Exception) -> str:
    name, status = exc.__class__.__name__, getattr(exc, "status_code", None)
    if name == "AuthenticationError" or status == 401:
        return "Anthropic rejected this key: copy it again from console.anthropic.com"
    if name == "PermissionDeniedError" or status == 403:
        return "this key is not allowed to use the API"
    if name in ("APIConnectionError", "APITimeoutError"):
        return "couldn't reach api.anthropic.com (offline, or blocked by a firewall)"
    return f"{name}: {str(exc)[:160]}"


def _check_claude() -> dict:
    try:
        import anthropic

        anthropic.Anthropic(timeout=15, max_retries=0).models.list(limit=1)
        return {"ok": True, "detail": "key works: free-form chat is on"}
    except Exception as exc:  # noqa: BLE001  (any failure is reported, never raised)
        return {"ok": False, "detail": _claude_error(exc)}


def _check_keewano() -> dict:
    from . import keewano

    rows = keewano.query("SELECT count(*) FROM events")
    if rows is None:
        return {"ok": False, "detail": "the query failed: check the personal access token and the project ID"}
    n = rows[0][0] if rows and rows[0] else 0
    return {"ok": True, "detail": f"connected: {n} events in the project"
            + ("" if os.environ.get("KEEWANO_API_KEY") else " (add the project key to send events too)")}


def _check_subq() -> dict:
    from .studio import subq

    return subq.check()


def check(services=None) -> dict:
    """Try each configured service once with the current keys, so a wrong key shows up at once instead
    of as a silent fallback: {"Claude": {"ok", "detail"}, "Keewano": {...}, "SubQ": {...}}. Services without keys
    are skipped. Nothing is sent to Keewano; SubQ answers one tiny question (about 50 input tokens)."""
    env = os.environ.get
    jobs = {}
    if env("ANTHROPIC_API_KEY") and (not services or "Claude" in services):
        jobs["Claude"] = _check_claude
    if env("KEEWANO_PAT") and env("KEEWANO_PROJECT_ID") and (not services or "Keewano" in services):
        jobs["Keewano"] = _check_keewano
    if env("SUBQ_API_KEY") and (not services or "SubQ" in services):
        jobs["SubQ"] = _check_subq
    if not jobs:
        return {}
    from . import settings
    if not settings.network_allowed():
        return {name: {"ok": False, "detail": "not checked: network lookups are off (Keys, Privacy and network)"} for name in jobs}
    with ThreadPoolExecutor(len(jobs)) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        return {name: f.result() for name, f in futures.items()}
