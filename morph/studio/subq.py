"""SubQ Snap (Subquadratic) decisions for Morph: yes/no, choice and score questions about a request, answered in
about a second. Standard library only; it never raises and never logs the key or what was asked.

    res = subq.decide({"request": text}, {"route": subq.choice("What does `request` ask for?", {...}),
                                          "harm": subq.noul("Is it meant to hurt people?")})
    if res is None: ...                       # no key, switched off, cooling down, or failed: use Morph's own rules
    subq.pick(res, "route")                   # the chosen option when its confidence is at least 0.6, else None
    subq.p_yes(res, "harm")                   # probability of yes

decide() returns {"answers", "usage", "ms", "source": "subq" | "cache"} or None. It retries 429, 503 and timeouts
once within its time budget; 401, 402 and 403 switch SubQ off until the key changes; a 503 or an unreachable
server pauses it for a minute. Answers (never the request, never images) are cached for 7 days in
~/.cache/morph/subq. Requests go to SubQ under the user's own key (SUBQ_API_KEY) and only with network lookups on.
Show results as "SubQ Snap (AI)".
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import random
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .security import open_keyed

BASE = os.environ.get("SUBQ_BASE_URL", "https://api.subq.ai/v1")
SNAP = "subq-snap-preview"
LABEL = "SubQ Snap (AI)"
CONFIDENCE = 0.6        # act on a choice or score at this confidence; below it, ask the person
YES = 0.5               # a noul at or above this is a yes
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "morph", "subq")
CACHE_TTL_S = 7 * 24 * 3600
COOL_S = 60
MAX_BODY = 12 << 20     # SubQ takes 13 MiB
MAX_IMAGES, MAX_IMAGE, MAX_IMAGES_TOTAL = 4, 4 << 20, 8 << 20
MEDIA = ("image/png", "image/jpeg", "image/webp")
RETRY = {429, 503}
OFF = {401: "key_rejected", 402: "no_credits", 403: "model_not_allowed"}
DETAIL = {401: "SubQ rejected this key: copy it again from console.subq.ai",
          402: "no credits left on this SubQ account: add credits at console.subq.ai (Morph uses its own rules meanwhile)",
          403: "this key can't use SubQ Snap (subq-snap-preview)",
          429: "SubQ is rate-limiting this key: try again in a minute",
          503: "SubQ is temporarily unavailable: try again shortly"}
B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")

_state = {"disabled": None, "key": None, "cool_until": 0.0, "last_error": None}
_lock = threading.Lock()


class _Failed(Exception):
    def __init__(self, status, kind, message, retry_after=None):
        super().__init__(f"{status} {kind}")
        self.status, self.kind, self.message, self.retry_after = status, kind, message, retry_after


def _key() -> str | None:
    return (os.environ.get("SUBQ_API_KEY") or "").strip() or None


def _fingerprint(key: str | None) -> str | None:
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else None


def _network() -> bool:
    try:
        from .. import settings
        return settings.network_allowed()
    except Exception:  # noqa: BLE001  (no settings, no network)
        return False


def _off() -> str | None:
    """Why SubQ is switched off for the current key (401/402/403), or None. A new key switches it back on."""
    with _lock:
        if _state["disabled"] and _state["key"] != _fingerprint(_key()):
            _state.update(disabled=None, key=None)
        return _state["disabled"]


def enabled() -> bool:
    """A key is set, network lookups are on, and SubQ hasn't refused the key."""
    return bool(_key()) and _network() and not _off()


def reset():
    """Forget errors, pauses and a refused key (the Keys dialog calls this when SUBQ_API_KEY changes)."""
    with _lock:
        _state.update(disabled=None, key=None, cool_until=0.0, last_error=None)


def status() -> dict:
    """For the Keys dialog and the header dot. Never contains the key or anything that was asked."""
    disabled = _off()
    with _lock:
        return {"configured": bool(_key()), "disabled": disabled, "last_error": _state["last_error"],
                "cooling": _state["cool_until"] > time.time()}


def _note(exc: _Failed, purpose: str):
    with _lock:
        _state["last_error"] = {"status": exc.status, "kind": exc.kind, "message": exc.message[:300],
                                "purpose": str(purpose)[:40], "at": time.strftime("%H:%M:%S")}


# ------------------------------------------------------------------ questions and answers

def noul(instructions: str, true: str | None = None, false: str | None = None) -> dict:
    """A yes/no question; `true`/`false` say what counts as each."""
    q = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {k: v for k, v in (("true", true), ("false", false)) if v}
    return q


def choice(instructions: str, options: dict) -> dict:
    """One of 2 to 255 named options ({name: description or None})."""
    return {"type": "choice", "instructions": instructions, "criteria": dict(options or {})}


def score(instructions: str, levels: list) -> dict:
    """A position on 2 to 10 ordered levels; the answer can fall between them."""
    return {"type": "score", "instructions": instructions, "criteria": list(levels or [])}


def _answer(res, name) -> dict:
    answers = res.get("answers") if isinstance(res, dict) and isinstance(res.get("answers"), dict) else res
    a = answers.get(name) if isinstance(answers, dict) else None
    return a if isinstance(a, dict) else {}


def p_yes(res, name) -> float | None:
    """The probability of yes for a noul question, or None."""
    v = _answer(res, name).get("noul")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def pick(res, name, min_conf: float = CONFIDENCE) -> str | None:
    """The chosen option of a choice question when SubQ is at least `min_conf` sure, else None (ask instead)."""
    a = _answer(res, name)
    conf = a.get("confidence")
    if not isinstance(conf, (int, float)) or conf < min_conf or not isinstance(a.get("choice"), str):
        return None
    return a["choice"]


def level(res, name) -> tuple:
    """(score, the label of the nearest level) for a score question, or (None, None)."""
    a = _answer(res, name)
    s = a.get("score")
    if not isinstance(s, (int, float)) or isinstance(s, bool):
        return None, None
    legend = a.get("legend") if isinstance(a.get("legend"), dict) else {}
    return float(s), str(legend.get(str(round(s)), ""))


def _questions_ok(questions) -> bool:
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 200:
        return False
    for name, q in questions.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or not isinstance(q, dict):
            return False
        kind, crit = q.get("type"), q.get("criteria")
        if not isinstance(q.get("instructions"), str) or not q["instructions"].strip():
            return False
        if kind == "choice" and not (isinstance(crit, dict) and 2 <= len(crit) <= 255):
            return False
        if kind == "score" and not (isinstance(crit, list) and 2 <= len(crit) <= 10):
            return False
        if kind not in ("noul", "choice", "score"):
            return False
    return True


def _images_ok(images) -> bool:
    """At most 4 PNG/JPEG/WebP images, {"mediaType", "base64"} with plain base64 (no data: prefix), 4 MiB each and
    8 MiB together once decoded."""
    if not isinstance(images, list) or not 1 <= len(images) <= MAX_IMAGES:
        return False
    total = 0
    for im in images:
        if not isinstance(im, dict) or set(im) != {"mediaType", "base64"} or im["mediaType"] not in MEDIA:
            return False
        data = im["base64"]
        if not isinstance(data, str) or len(data) > MAX_IMAGE * 4 // 3 + 8 or not B64.fullmatch(data):
            return False
        try:
            size = len(base64.b64decode(data, validate=True))
        except (binascii.Error, ValueError):
            return False
        total += size
        if not size or size > MAX_IMAGE or total > MAX_IMAGES_TOTAL:
            return False
    return True


# ------------------------------------------------------------------ the request

def _post(path: str, body: bytes, timeout: float, key: str | None) -> dict:
    headers = {"Content-Type": "application/json", "User-Agent": "morph-studio"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with open_keyed(Request(f"{BASE}{path}", data=body, method="POST", headers=headers), timeout=max(0.05, timeout)) as resp:
            raw = resp.read(MAX_BODY)
    except HTTPError as exc:
        raise _error(exc.code, exc.read(64 << 10), exc.headers.get("Retry-After")) from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise _Failed(None, "unreachable", exc.__class__.__name__) from None
    try:
        out = json.loads(raw)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        raise _Failed(None, "bad_response", "the answer was not a JSON object")
    return out


def _error(code: int, raw: bytes, retry_after=None) -> _Failed:
    """Both error shapes: Decisions {"detail": {error_type, message}}, OpenAI-style {"error": {type, code, message}}."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    wait = None
    try:
        wait = min(5.0, max(0.0, float(retry_after))) if retry_after else None
    except ValueError:
        pass
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        d = body["detail"]
        return _Failed(code, str(d.get("error_type") or "error")[:60], str(d.get("message") or ""), wait)
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        return _Failed(code, str(e.get("code") or e.get("type") or "error")[:60], str(e.get("message") or ""), wait)
    return _Failed(code, "http_error", f"HTTP {code}", wait)


def _cache_path(body: dict) -> str:
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return os.path.join(CACHE, digest + ".json")


def _cache_get(path: str) -> dict | None:
    try:
        if time.time() - os.path.getmtime(path) >= CACHE_TTL_S:
            return None
        with open(path, encoding="utf-8") as fh:
            hit = json.load(fh)
    except (OSError, ValueError):
        return None
    return hit if isinstance(hit, dict) and isinstance(hit.get("answers"), dict) else None


def _cache_put(path: str, res: dict):
    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"answers": res["answers"], "usage": res.get("usage") or {}}, fh)  # answers only, never the request
        os.replace(tmp, path)
    except OSError:
        pass


def decide(state, questions: dict, images: list | None = None, *, timeout: float = 8.0, retries: int = 1,
           cache: bool = True, purpose: str = "") -> dict | None:
    """Ask SubQ Snap `questions` about `state` (text, an object or a list). Returns {"answers", "usage", "ms",
    "source": "subq" | "cache"}, or None when the caller should use its fallback: no key, network lookups off, SubQ
    switched off or pausing, an invalid request, or no answer within `timeout` seconds (all tries together)."""
    key = _key()
    if not key or not _network() or _off():
        return None
    with _lock:
        if time.time() < _state["cool_until"]:
            return None
    started = time.monotonic()
    invalid = None
    if not _questions_ok(questions):
        invalid = "the questions are not a valid SubQ request"
    elif images is not None and not _images_ok(images):
        invalid = "images must be at most 4 PNG, JPEG or WebP files, 4 MiB each and 8 MiB together, as plain base64"
    body = {"model": SNAP, "state": state, "questions": questions}
    if images:
        body["images"] = images
    try:
        raw = json.dumps(body, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raw, invalid = b"", invalid or "the state can't be sent as JSON"
    if not invalid and len(raw) > MAX_BODY:
        invalid = "the request is larger than SubQ accepts"
    if invalid:
        _note(_Failed(None, "invalid_request", invalid), purpose)
        return None
    path = _cache_path(body) if cache and not images else None  # photos are never cached
    hit = _cache_get(path) if path else None
    if hit:
        return {"answers": hit["answers"], "usage": hit.get("usage") or {}, "ms": 0, "source": "cache"}
    for attempt in range(max(0, int(retries)) + 1):
        left = timeout - (time.monotonic() - started)
        if left <= 0.05:
            break
        t0 = time.monotonic()
        try:
            out = _post("/systemone", raw, left, key)
            answers = out.get("answers")
            if not isinstance(answers, dict):
                raise _Failed(None, "bad_response", "the answer had no answers")
            res = {"answers": answers, "usage": out.get("usage") if isinstance(out.get("usage"), dict) else {},
                   "ms": round((time.monotonic() - t0) * 1000), "source": "subq"}
            if path:
                _cache_put(path, res)
            with _lock:
                _state["last_error"] = None
            return res
        except _Failed as exc:
            _note(exc, purpose)
            if exc.status in OFF:  # until the key changes
                with _lock:
                    _state.update(disabled=OFF[exc.status], key=_fingerprint(key))
                return None
            if exc.status == 422:  # our request is wrong: sending it again won't help
                return None
            if attempt < retries and (exc.status in RETRY or exc.status is None):
                wait = exc.retry_after if exc.retry_after is not None else 0.5 * (2 ** attempt) + random.random() * 0.25
                if time.monotonic() - started + wait < timeout - 0.1:
                    time.sleep(wait)
                    continue
            break
    with _lock:  # overloaded, rate-limited or unreachable: stop asking for a minute
        _state["cool_until"] = time.time() + COOL_S
        if _state["last_error"] is None:
            _state["last_error"] = {"status": None, "kind": "timeout", "message": f"no answer within {timeout:g} s",
                                    "purpose": str(purpose)[:40], "at": time.strftime("%H:%M:%S")}
    return None


# ------------------------------------------------------------------ checks

def check(timeout: float = 15) -> dict:
    """Test keys: one tiny decision (about 50 input tokens) with the current key: {"ok", "detail"}."""
    key = _key()
    if not key:
        return {"ok": False, "detail": "no key set"}
    if not _network():
        return {"ok": False, "detail": "not checked: network lookups are off (Keys, Privacy and network)"}
    body = json.dumps({"model": SNAP, "state": "Morph key check",
                       "questions": {"ok": noul("Is this a key check?")}}).encode()
    try:
        _post("/systemone", body, timeout, key)
    except _Failed as exc:
        if exc.status in OFF:
            with _lock:
                _state.update(disabled=OFF[exc.status], key=_fingerprint(key))
        if exc.status is None:
            return {"ok": False, "detail": "couldn't reach api.subq.ai (offline, or blocked by a firewall)"}
        return {"ok": False, "detail": DETAIL.get(exc.status) or f"{exc.status} {exc.kind}: {exc.message[:120]}"}
    reset()
    return {"ok": True, "detail": "key works: Morph can ask SubQ Snap"}


def health(timeout: float = 5) -> dict:
    """Is api.subq.ai reachable (no key needed)? For `morph doctor`: {"ok", "detail"}."""
    url = BASE.rsplit("/v1", 1)[0] + "/health"
    try:
        with urlopen(Request(url, headers={"User-Agent": "morph-studio"}), timeout=timeout) as resp:
            return {"ok": resp.status == 200, "detail": "reachable" if resp.status == 200 else f"HTTP {resp.status}"}
    except HTTPError as exc:
        return {"ok": False, "detail": f"HTTP {exc.code}"}
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "detail": f"not reachable ({exc.__class__.__name__})"}


def lint(body: dict, timeout: float = 15) -> tuple:
    """Dev and CI only, no key: SubQ checks a request's shape before the key, so 401 means the body is valid and 422
    returns what is wrong. (ok, message)."""
    try:
        _post("/systemone", json.dumps(body).encode(), timeout, None)
        return True, "unexpected 200 without a key"
    except _Failed as exc:
        return exc.status == 401, f"{exc.status} {exc.kind}: {exc.message[:300]}"
    except (TypeError, ValueError) as exc:
        return False, f"not JSON: {exc}"
