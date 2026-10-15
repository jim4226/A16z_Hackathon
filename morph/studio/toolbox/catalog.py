"""The tool catalog: morph.tool_manifest/1 manifests become the tools agents find through tool search, the keys those
tools need (the Keys dialog lists them) and the fingerprints approvals are bound to.

Manifests come from Morph (morph/studio/toolbox/catalog/*.json: curated, written from each vendor's official docs, on
by default) and from this Studio's owner (MORPH_HOME/.tools/catalog/*.json: off until the owner turns them on). One
manifest describes one provider:

    {"schema": "morph.tool_manifest/1", "id": "fixparts", "service": "FixParts", "homepage": "https://www.fixparts.com",
     "summary": "...", "privacy": "Part numbers from the parts list go to FixParts.", "hosts": ["api.fixparts.com"],
     "credentials": [{"id": "fixparts", "env": "MORPH_TOOL_FIXPARTS_KEY", "label": "FixParts API key", "what": "...",
                      "where": "FixParts dashboard, API keys", "url": "https://www.fixparts.com/keys",
                      "hosts": ["api.fixparts.com"], "pattern": "^fxp_[A-Za-z0-9]{24,64}$", "prefix": "fxp_",
                      "cost": "Free: 1,000 lookups a month"}],
     "tools": [{"name": "fixparts_offers", "title": "...", "description": "...", "side_effect": "read", "sends": "...",
                "request": {...}, "auth": {...}, "input_schema": {...}, "response": {...}, "limits": {...},
                "cost": {"usd_per_call": 0}}]}

Validation is strict: unknown fields are refused; a key's name must match MORPH_TOOL_[A-Z0-9_]{2,40} (a manifest can
never name ANTHROPIC_API_KEY, PATH or a proxy variable); hosts are public DNS names reached over HTTPS (no IP
literals, no localhost or local-network names); a key goes only to its own hosts; every tool declares its side effect
(read, write, upload, spend), its cost, what it sends and where its key goes (a header, or the query string with a
warning). Claude sees Morph's description of each tool (the manifest's text plus Morph's own line on provider, side
effect, cost and key), only ever as a tool definition: nothing from a manifest goes into a system prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from ...services import durable
from . import http_tool, store
from .http_tool import DEFAULT_POLICY, SpecError

SCHEMA = "morph.tool_manifest/1"
PACKAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog")
CURATED_DIRS = [PACKAGE_DIR]
MAX_MANIFEST_BYTES = 256 << 10
PROVIDER = re.compile(r"[a-z][a-z0-9]{1,23}")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{2,47}")
ENV_NAME = re.compile(r"MORPH_TOOL_[A-Z0-9_]{2,40}")
CRED_ID = re.compile(r"[a-z][a-z0-9_]{1,31}")
KEY_PREFIX = re.compile(r"[A-Za-z0-9_-]{2,16}")
RESERVED = ("morph", "darwin", "mcp", "tool", "web", "claude", "anthropic", "subq", "keewano", "code", "computer", "bash")
SIDE_EFFECTS = http_tool.SIDE_EFFECTS
MANIFEST_FIELDS = {"schema", "id", "service", "summary", "homepage", "privacy", "hosts", "credentials", "tools", "verified"}
CREDENTIAL_FIELDS = {"id", "env", "label", "what", "where", "url", "hosts", "pattern", "prefix", "cost", "privacy"}
TOOL_FIELDS = {"name", "title", "description", "side_effect", "sends", "request", "auth", "input_schema", "response",
               "limits", "cost"}
SIDE_TEXT = {"read": "Read-only: it only looks things up.",
             "write": "It changes something at the provider, so Morph asks the person before every call.",
             "upload": "It sends files or data to the provider, so Morph asks the person before every call.",
             "spend": "It spends money, so Morph asks the person before every call."}
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
_TOKEN = re.compile(r"[^\s\"'`<>()\[\]{},;|]+")


class ManifestError(ValueError):
    """A manifest Morph won't load; the message names the rule it breaks."""


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fingerprint(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


# ------------------------------------------------------------------------------------------------ the parts
@dataclass(frozen=True)
class Credential:
    id: str
    env: str
    provider: str
    service: str
    label: str
    what: str
    where: str
    url: str
    hosts: tuple
    pattern: str
    prefix: str = ""
    cost: str = ""
    privacy: str = ""
    curated: bool = False

    def spec(self) -> dict:
        """What the HTTP runner needs (never the value)."""
        return {"id": self.id, "env": self.env, "hosts": list(self.hosts), "service": self.service}

    def valid(self, value) -> bool:
        if not isinstance(value, str) or not 1 <= len(value) <= 4096 or re.search(r"[\s\"'\\\x00-\x1f\x7f]", value):
            return False
        return value.startswith(self.prefix) and re.fullmatch(self.pattern, value) is not None

    def entry(self) -> dict:
        """The Keys dialog's entry for this key: what it is for, who gets it, where it is sent. Never the value."""
        return {"name": self.env, "service": self.service, "label": self.label, "what": self.what, "where": self.where,
                "url": self.url, "cost": self.cost, "privacy": self.privacy, "hosts": list(self.hosts),
                "pattern": self.pattern, "prefix": self.prefix, "provider": self.provider, "curated": self.curated,
                "tool": True}


@dataclass(frozen=True)
class Tool:
    name: str
    provider: str
    service: str
    title: str
    description: str
    side_effect: str
    sends: str
    privacy: str
    host: str
    cost_usd: float
    cost_note: str
    spec: dict = field(compare=False, hash=False)
    credential: Credential | None = None
    curated: bool = False
    source: str = ""
    fingerprint: str = ""
    warnings: tuple = ()

    @property
    def paid(self) -> bool:
        return self.cost_usd > 0 or self.side_effect == "spend"

    def claude_description(self) -> str:
        parts = [self.description.strip(), f"Provider: {self.service} ({self.host}).", SIDE_TEXT[self.side_effect]]
        if self.paid:
            parts.append(f"It costs about ${self.cost_usd:g} a call; paid tools aren't supported in this beta, so Morph refuses calls."
                         if self.cost_usd else "Paid tools aren't supported in this beta, so Morph refuses calls.")
        else:
            parts.append("No fee per call.")
        if self.credential is not None:
            parts.append(f"It uses the person's {self.credential.label}, which Morph adds: when the tool would help, call it even "
                         "if the key isn't set yet, and Morph asks the person for it.")
        return " ".join(parts)[:2000]

    def definition(self) -> dict:
        """The Claude tool definition (without defer_loading; the run adds it)."""
        return {"name": self.name, "description": self.claude_description(), "input_schema": self.spec["input_schema"]}

    def card(self) -> dict:
        """What a person is shown about this tool before approving it: never a key, never Claude's text."""
        return {"tool": self.name, "title": self.title, "service": self.service, "provider": self.provider,
                "side_effect": self.side_effect, "sends": self.sends, "privacy": self.privacy, "host": self.host,
                "cost_usd": self.cost_usd, "cost_note": self.cost_note, "curated": self.curated}


@dataclass
class Catalog:
    tools: dict = field(default_factory=dict)          # name -> Tool
    credentials: dict = field(default_factory=dict)    # env -> Credential
    providers: dict = field(default_factory=dict)      # id -> {id, service, summary, homepage, privacy, hosts, curated, ...}
    errors: list = field(default_factory=list)         # [{"source", "error"}] manifests that were not loaded

    def tool(self, name: str) -> Tool | None:
        return self.tools.get(name)


# ------------------------------------------------------------------------------------------------ validation
def _text(v, name: str, lo: int, hi: int) -> str:
    if not isinstance(v, str) or re.search(r"[\x00-\x1f\x7f]", v) or not lo <= len(v.strip()) <= hi:
        raise ManifestError(f"{name}: text of {lo} to {hi} characters, on one line")
    return v.strip()


def _url(v, name: str) -> str:
    if not isinstance(v, str) or len(v) > 300 or not re.fullmatch(r"https://[^\s\"'<>\\]+", v):
        raise ManifestError(f"{name}: an https:// link")
    host = re.match(r"https://([^/:?#]+)", v).group(1)
    if not http_tool.public_host(host):
        raise ManifestError(f"{name}: an https:// link to a public host")
    return v


def _hosts(v, name: str, policy) -> tuple:
    if not isinstance(v, list) or not 1 <= len(v) <= 8 or len(set(v)) != len(v):
        raise ManifestError(f"{name}: a list of 1 to 8 host names")
    for h in v:
        if not (http_tool.public_host(h) or (policy.allow_loopback_http and h == "127.0.0.1")):
            raise ManifestError(f"{name}: {str(h)[:80]} is not a public DNS name (IP addresses and local names are refused)")
    return tuple(v)


def _pattern(v, name: str) -> str:
    """A key's pattern: ^...$, character classes, literals and quantifiers only (no groups, so no nested repetition,
    and the same meaning in the browser), never matching an empty value."""
    if not isinstance(v, str) or not 3 <= len(v) <= 200 or not v.startswith("^") or not v.endswith("$") or \
            re.search(r"[()|]|\\[1-9bBAZzGQEpP]|\(\?", v):
        raise ManifestError(f"{name}: a pattern like ^[A-Za-z0-9_-]{{20,64}}$ (no groups or alternation)")
    try:
        if re.fullmatch(v, ""):
            raise ManifestError(f"{name}: the pattern must not match an empty key")
    except re.error:
        raise ManifestError(f"{name}: not a valid pattern") from None
    return v


def parse_manifest(doc, *, source: str = "", curated: bool = False, policy=DEFAULT_POLICY):
    """(provider info, [Tool], [Credential]) from one manifest; ManifestError for anything Morph won't load."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ManifestError(f"schema must be {SCHEMA}")
    unknown = set(doc) - MANIFEST_FIELDS
    if unknown:
        raise ManifestError(f"unknown fields: {sorted(unknown)[:10]}")
    pid = doc.get("id")
    if not isinstance(pid, str) or not PROVIDER.fullmatch(pid) or pid in RESERVED:
        raise ManifestError("id: 2 to 24 lower-case letters and digits, not a name Morph uses itself")
    service = _text(doc.get("service"), "service", 2, 60)
    privacy = _text(doc.get("privacy"), "privacy", 10, 400)
    info = {"id": pid, "service": service, "privacy": privacy, "curated": curated, "source": source,
            "summary": _text(doc["summary"], "summary", 1, 300) if "summary" in doc else "",
            "homepage": _url(doc["homepage"], "homepage") if "homepage" in doc else "",
            "hosts": list(_hosts(doc.get("hosts"), "hosts", policy)), "verified": None}
    if "verified" in doc:
        ver = doc["verified"]
        if not isinstance(ver, dict) or set(ver) - {"docs", "checked", "live", "notes"} or \
                ("live" in ver and ver["live"] not in (None, True, False)) or \
                ("checked" in ver and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(ver["checked"]))):
            raise ManifestError('verified: {"docs": link, "checked": "YYYY-MM-DD", "live": null|true|false, "notes": text}')
        info["verified"] = {"docs": _url(ver["docs"], "verified.docs") if "docs" in ver else "", "checked": ver.get("checked", ""),
                            "live": ver.get("live"), "notes": _text(ver["notes"], "verified.notes", 1, 300) if "notes" in ver else ""}
    creds = {}
    raw_creds = doc.get("credentials", [])
    if not isinstance(raw_creds, list) or len(raw_creds) > 4:
        raise ManifestError("credentials: a list of at most 4 keys")
    for i, c in enumerate(raw_creds):
        where = f"credentials[{i}]"
        if not isinstance(c, dict) or set(c) - CREDENTIAL_FIELDS:
            raise ManifestError(f"{where}: unknown fields {sorted(set(c) - CREDENTIAL_FIELDS)[:10] if isinstance(c, dict) else ''}")
        cid, env = c.get("id"), c.get("env")
        if not isinstance(cid, str) or not CRED_ID.fullmatch(cid) or cid in creds:
            raise ManifestError(f"{where}.id: 2 to 32 lower-case letters, digits or _, unique in the manifest")
        if not isinstance(env, str) or not ENV_NAME.fullmatch(env):
            raise ManifestError(f"{where}.env must be MORPH_TOOL_<NAME> (A-Z, 0-9, _; 2 to 40 characters after the prefix)")
        hosts = _hosts(c.get("hosts"), f"{where}.hosts", policy)
        if set(hosts) - set(info["hosts"]):
            raise ManifestError(f"{where}.hosts: every host must also be one of the manifest's hosts")
        prefix = c.get("prefix", "")
        if prefix and (not isinstance(prefix, str) or not KEY_PREFIX.fullmatch(prefix)):
            raise ManifestError(f"{where}.prefix: 2 to 16 letters, digits, _ or -")
        creds[cid] = Credential(
            id=cid, env=env, provider=pid, service=service, label=_text(c.get("label"), f"{where}.label", 2, 80),
            what=_text(c.get("what"), f"{where}.what", 5, 300), where=_text(c.get("where"), f"{where}.where", 3, 200),
            url=_url(c.get("url"), f"{where}.url"), hosts=hosts, pattern=_pattern(c.get("pattern"), f"{where}.pattern"),
            prefix=prefix, cost=_text(c["cost"], f"{where}.cost", 1, 200) if "cost" in c else "",
            privacy=_text(c["privacy"], f"{where}.privacy", 10, 400) if "privacy" in c else privacy, curated=curated)
    if len({c.env for c in creds.values()}) != len(creds):
        raise ManifestError("two credentials of one manifest use the same env name")
    raw_tools = doc.get("tools")
    if not isinstance(raw_tools, list) or not 1 <= len(raw_tools) <= 32:
        raise ManifestError("tools: a list of 1 to 32 tools")
    tools, names, used = [], set(), set()
    cred_specs = {cid: {"id": cid, "hosts": list(c.hosts)} for cid, c in creds.items()}
    for i, t in enumerate(raw_tools):
        if not isinstance(t, dict):
            raise ManifestError(f"tools[{i}]: an object")
        name = t.get("name")
        where = f"tools[{i}] ({name})" if isinstance(name, str) else f"tools[{i}]"
        if set(t) - TOOL_FIELDS:
            raise ManifestError(f"{where}: unknown fields {sorted(set(t) - TOOL_FIELDS)[:10]}")
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name) or not name.startswith(pid + "_") or name in names:
            raise ManifestError(f"{where}.name: {pid}_<name> in lower-case letters, digits and _ (3 to 48), unique")
        names.add(name)
        side = t.get("side_effect")
        if side not in SIDE_EFFECTS:
            raise ManifestError(f"{where}.side_effect must be one of {', '.join(SIDE_EFFECTS)}")
        cost = t.get("cost")
        if not isinstance(cost, dict) or set(cost) - {"usd_per_call", "note"} or "usd_per_call" not in cost:
            raise ManifestError(f'{where}.cost: {{"usd_per_call": number, "note"?: text}}')
        usd = cost["usd_per_call"]
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not 0 <= usd <= 1000:
            raise ManifestError(f"{where}.cost.usd_per_call: a number of dollars, 0 or more")
        if usd > 0 and side != "spend":
            raise ManifestError(f"{where}: a tool that costs money per call must declare side_effect spend")
        title = _text(t.get("title"), f"{where}.title", 2, 60)
        desc = _text(t.get("description"), f"{where}.description", 20, 1024)
        sends = _text(t.get("sends"), f"{where}.sends", 3, 200)
        try:
            warnings = http_tool.check_tool(t, hosts=info["hosts"], credentials=cred_specs, policy=policy, curated=curated)
        except SpecError as exc:
            raise ManifestError(f"{where}: {exc}") from None
        if http_tool.INJECTION_HINTS.search(desc) or http_tool.INJECTION_HINTS.search(title):
            warnings.append("The description uses wording often found in prompt injection; read it before turning the tool on.")
        cred = creds[t["auth"]["credential"]] if t.get("auth") else None
        if cred:
            used.add(cred.id)
        fp = fingerprint({"provider": pid, "service": service, "privacy": privacy, "hosts": info["hosts"], "tool": t,
                          "credential": next((c for c in raw_creds if cred and c.get("id") == cred.id), None)})
        tools.append(Tool(name=name, provider=pid, service=service, title=title, description=desc, side_effect=side,
                          sends=sends, privacy=privacy, host=http_tool.split_url(t["request"]["url"], policy)[1], cost_usd=float(usd),
                          cost_note=_text(cost["note"], f"{where}.cost.note", 1, 200) if "note" in cost else "",
                          spec=json.loads(json.dumps(t)), credential=cred, curated=curated, source=source,
                          fingerprint=fp, warnings=tuple(warnings)))
    unused = set(creds) - used
    if unused:
        raise ManifestError(f"credentials no tool uses: {sorted(unused)}")
    return info, tools, list(creds.values())


# ------------------------------------------------------------------------------------------------ loading
_CACHE: OrderedDict = OrderedDict()
_CACHE_LOCK = threading.Lock()


def user_dir(home: str | None = None) -> str:
    return os.path.join(store.tools_dir(home), "catalog")


def _no_constant(name):
    raise ManifestError(f"{name} is not valid JSON")


def _files(home, curated_dirs):
    out = []
    for folder, curated in [(d, True) for d in curated_dirs] + [(user_dir(home), False)]:
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            p = os.path.join(folder, fn)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):  # the inode too: a file replaced within one clock tick at the same size is new
                out.append((p, curated, st.st_mtime_ns, st.st_size, st.st_ino))
    return tuple(out)


def _build(files, policy) -> Catalog:
    cat = Catalog()
    for p, curated, _, size, _ino in files:
        source = os.path.basename(p)
        try:
            if size > MAX_MANIFEST_BYTES:
                raise ManifestError(f"larger than {MAX_MANIFEST_BYTES // 1024} KiB")
            with open(p, encoding="utf-8-sig") as fh:  # a manifest saved by Notepad starts with a BOM
                doc = json.loads(fh.read(), parse_constant=_no_constant)
            info, tools, creds = parse_manifest(doc, source=source, curated=curated, policy=policy)
            info["sha256"] = fingerprint(doc)  # what a pin (an approved draft) is bound to
            if info["id"] in cat.providers:
                raise ManifestError(f"provider {info['id']} is already in the catalog"
                                    + (" (a built-in provider)" if cat.providers[info["id"]]["curated"] else ""))
            clash = [t.name for t in tools if t.name in cat.tools] + [c.env for c in creds if c.env in cat.credentials]
            if clash:
                raise ManifestError(f"already in the catalog: {clash[:5]}")
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:  # the Keys dialog lists it
            projects = os.path.dirname(os.path.dirname(os.path.dirname(p)))  # <projects>/.tools/catalog/<file>
            cat.errors.append({"source": source, "curated": curated, "error": durable.explain(exc, projects)[:300]})
            continue
        info["warnings"] = sorted({w for t in tools for w in t.warnings})
        cat.providers[info["id"]] = info
        cat.tools.update({t.name: t for t in tools})
        cat.credentials.update({c.env: c for c in creds})
    return cat


def load(home: str | None = None, *, curated_dirs=None, policy=DEFAULT_POLICY) -> Catalog:
    """Every manifest Morph ships plus the owner's own, validated; cached until a file changes. Never raises: a
    manifest that fails validation is left out and listed in `errors`."""
    files = _files(home, list(CURATED_DIRS if curated_dirs is None else curated_dirs))
    key = (files, policy)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    cat = _build(files, policy)
    with _CACHE_LOCK:
        _CACHE[key] = cat
        while len(_CACHE) > 16:
            _CACHE.popitem(last=False)
    return cat


# ------------------------------------------------------------------------------------------------ on and off
def _state_path(home: str | None = None) -> str:
    return os.path.join(store.tools_dir(home), "state.json")


STATE_PROBLEM = ("can't be read (damaged, or edited by hand), so every tool is off until you turn the ones you want on "
                 "again")


def _unreadable(path: str) -> bool:
    """The file is there but doesn't hold an object."""
    return os.path.exists(path) and not isinstance(store.read_json(path, None), dict)


def read_state(home: str | None = None) -> dict:
    """{"enabled": {tool: bool}} the owner set, plus "damaged": True when state.json is there but can't be read: then
    no tool counts as on (enabled_names), never Morph's defaults (they would turn tools the owner turned off on)."""
    path = _state_path(home)
    st = store.read_json(path, {}) or {}
    en = st.get("enabled") if isinstance(st, dict) else None
    out = {"enabled": {k: v for k, v in (en or {}).items() if isinstance(k, str) and isinstance(v, bool)}}
    if _unreadable(path) or (isinstance(st, dict) and "enabled" in st and not isinstance(en, dict)):
        out["damaged"] = True
    return out


def problems(home: str | None = None) -> list:
    """The tool files that can't be read, as catalog errors ({source, error}) for the Keys dialog."""
    out = []
    if read_state(home).get("damaged"):
        out.append({"source": ".tools/state.json", "error": STATE_PROBLEM})
    if _unreadable(_pins_path(home)):
        out.append({"source": ".tools/pins.json", "error": "can't be read (damaged, or edited by hand), so tools "
                                                           "installed from approved drafts are off until drafted again"})
    return out


def enabled_names(cat: Catalog, home: str | None = None) -> set:
    """Morph's own tools are on unless the owner turned them off; the owner's own manifests are off until turned on. A
    manifest installed from an approved draft (pinned) is on only while its content still has the approved sha256.
    Fail closed: while state.json can't be read nothing is on, and while pins.json can't be read none of the owner's own
    manifests is."""
    st, pins = read_state(home), read_pins(home)
    over, damaged, pins_bad = st["enabled"], st.get("damaged"), _unreadable(_pins_path(home))
    return {n for n, t in cat.tools.items() if over.get(n, t.curated and not damaged) and not (pins_bad and not t.curated)
            and pin_ok(cat, t, pins) is not False}


# ------------------------------------------------------------------------------------------------ pins (drafts)
def _pins_path(home: str | None = None) -> str:
    return os.path.join(store.tools_dir(home), "pins.json")


def read_pins(home: str | None = None) -> dict:
    """{provider id: {sha256, file, by, at, draft, docs}}: the owner's manifests installed from an approved draft."""
    data = store.read_json(_pins_path(home), {}) or {}
    pins = data.get("pins") if isinstance(data, dict) else None
    return {k: v for k, v in (pins or {}).items() if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("sha256"), str)}


def pin(provider: str, sha256: str, home: str | None = None, **info) -> dict:
    """Bind an owner manifest to the sha256 the owner approved (draft.py); any later change turns its tools off."""
    with store.locked(store.tools_dir(home)):
        pins = read_pins(home)
        pins[provider] = {**info, "sha256": sha256, "at": store.utc()}
        store.write_json(_pins_path(home), {"pins": pins})
    return pins[provider]


def pin_ok(cat: Catalog, tool: Tool, pins: dict) -> bool | None:
    """None: the tool's manifest isn't pinned; True: it still has the approved content; False: it changed since."""
    p = pins.get(tool.provider) if not tool.curated else None
    return None if p is None else (cat.providers.get(tool.provider) or {}).get("sha256") == p["sha256"]


def set_enabled(name: str, on: bool, home: str | None = None, names=()) -> bool:
    """Turn one tool on or off; True when that changed anything. While state.json can't be read, the tools in `names`
    (the catalog's) are written as off, as the owner sees them, and the damaged file is replaced: no tool comes back on
    by itself."""
    folder = store.tools_dir(home)
    with store.locked(folder):
        st = read_state(home)
        if st.pop("damaged", False):
            st["enabled"] = {**{n: False for n in names}, **st["enabled"]}
        elif st["enabled"].get(name) is on:
            return False
        st["enabled"][name] = bool(on)
        store.write_json(_state_path(home), st)
    return True


# ------------------------------------------------------------------------------------------------ keys
def key_specs(home: str | None = None) -> dict:
    """{env name: Credential} for every key a loaded manifest declares (on or off)."""
    return dict(load(home).credentials)


def key_entries(home: str | None = None) -> list:
    """The Keys dialog's entries for the keys of tools that are on, grouped by service: never a value."""
    cat = load(home)
    on = enabled_names(cat, home)
    users: dict = {}
    for t in sorted(cat.tools.values(), key=lambda t: t.name):
        if t.name in on and t.credential is not None:
            users.setdefault(t.credential.env, []).append(t)
    out = []
    for env, tools in users.items():
        e = cat.credentials[env].entry()
        e["tools"], e["used_by"] = [t.name for t in tools], [t.title for t in tools]
        out.append(e)
    return sorted(out, key=lambda e: (e["service"].lower(), e["label"].lower()))


def looks_like_tool_key(text: str, home: str | None = None, env=None) -> bool:
    """True when `text` holds a tool key: the value of any MORPH_TOOL_* key that is set, a token that starts with a
    manifest's key prefix and fits its pattern, or a bearer token (JWT). Never raises."""
    text = text or ""
    if not text:
        return False
    if JWT.search(text):
        return True
    for k, v in store.env_items(os.environ if env is None else env):
        if k.startswith("MORPH_TOOL_") and len(v) >= 8 and v in text:
            return True
    try:
        creds = [c for c in load(home).credentials.values() if c.prefix and c.prefix in text]
    except Exception:  # noqa: BLE001  a chat filter must never break chat
        return False
    for c in creds:
        for tok in _TOKEN.findall(text):
            t = tok.strip(".:!?")
            if t.startswith(c.prefix) and c.valid(t):
                return True
    return False
