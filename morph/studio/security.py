"""Local-only Studio boundary. These protections are not hosted authentication."""

from __future__ import annotations

import hmac
import re
import secrets
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_BODY = 64 * 1024
MAX_MESSAGE = 8000
COOKIE = "rcubed_session"
PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def project_name(value: str) -> str:
    if not isinstance(value, str) or not PROJECT_NAME.fullmatch(value) or value.lower() in WINDOWS_RESERVED:
        raise ValueError("Use 1–64 letters, numbers, hyphens or underscores for the project name.")
    return value


def contained_path(root: str, relative: str) -> str | None:
    """Reject traversal, drive paths and symlink/junction escapes, including sibling prefixes."""
    relative = unquote(relative)
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        return None
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    try:
        base = Path(root).resolve()
        path = (base / relative).resolve()
        path.relative_to(base)
        return str(path) if path != base else None
    except (OSError, ValueError, RuntimeError):
        return None


class LocalSession:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)

    def cookie(self) -> str:
        # Secure cannot be set for an HTTP loopback listener. Never use this on public HTTP.
        return f"{COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Strict"

    def authenticated(self, headers) -> bool:
        try:
            jar = SimpleCookie()
            jar.load(headers.get("Cookie", ""))
            value = jar[COOKIE].value if COOKIE in jar else ""
            return hmac.compare_digest(value, self.token)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def valid_request(headers, port: int) -> bool:
        # An attacker-controlled DNS name resolving to loopback must never be accepted.
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        if port == 80:
            hosts |= {"127.0.0.1", "localhost", "[::1]"}
        host = headers.get("Host", "").lower()
        if host not in hosts or len(headers.get_all("Host", [])) != 1:
            return False
        origin = headers.get("Origin")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                return False
            if parsed.scheme != "http" or parsed.netloc.lower() != host or parsed.path or parsed.query or parsed.fragment:
                return False
        if headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none"):
            return False
        return True
