"""The Studio's route table: every /api endpoint (and /files) is registered here and dispatched by server.py.

    from .routes import HTTPError, route

    @route("GET", "/api/world", role="viewer")
    def world(req):                                   # req.room is the ?project= room (default: the start project)
        return 200, {"world": ...}

    @route("PUT", "/api/world/upload", raw_body=True, max_body=300 << 20)
    def upload(req):
        size = req.save(path)                         # streamed to disk in 1 MiB chunks, never past max_body
        return 200, {"ok": True, "bytes": size}

A path is exact ("/api/brief") or a prefix ending in "/*" ("/api/import/*"); exact paths win, then the longest
prefix. Before a handler runs, the server has checked the Host/Origin, the access token, the X-Morph-Client header
on POST/PUT/DELETE, the declared body size against `max_body`, and the role. JSON bodies arrive parsed in
`req.body` (a dict; None for GET and raw_body routes). A handler returns (status, dict | list) for JSON,
(status, bytes, content_type) for anything else, or None when it wrote the response itself, and may raise
HTTPError(status, message) for a JSON error. Feature modules register their routes in
morph/studio/api_<feature>.py; server.py imports every such module at start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .security import MAX_BODY

ROLES = ("viewer", "editor", "owner")
METHODS = ("GET", "POST", "PUT", "DELETE")


class HTTPError(Exception):
    """A refusal a handler (or the role hook) raises; the client gets {"ok": false, "error": message}."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    fn: Callable
    role: str = "editor"
    raw_body: bool = False
    max_body: int = MAX_BODY


_EXACT: dict = {}   # (method, path) -> Route
_PREFIX: list = []  # Routes whose path ends in "/*", longest first


def route(method: str, path: str, *, role: str = "editor", raw_body: bool = False, max_body: int = MAX_BODY):
    """Register the decorated function for `method path`. A path registered twice is an error (two features
    claiming one route)."""
    method = method.upper()
    if method not in METHODS or role not in ROLES or not path.startswith("/") or max_body < 0:
        raise ValueError(f"bad route {method} {path} (role {role})")

    def register(fn):
        r = Route(method, path, fn, role, raw_body, int(max_body))
        if path.endswith("/*"):
            if any(x.method == method and x.path == path for x in _PREFIX):
                raise ValueError(f"{method} {path} is already registered")
            _PREFIX.append(r)
            _PREFIX.sort(key=lambda x: -len(x.path))
        else:
            if (method, path) in _EXACT:
                raise ValueError(f"{method} {path} is already registered")
            _EXACT[(method, path)] = r
        return fn

    return register


def find(method: str, path: str) -> Route | None:
    r = _EXACT.get((method, path))
    if r is not None:
        return r
    for r in _PREFIX:
        base = r.path[:-1]  # "/api/import/"
        if r.method == method and path.startswith(base) and len(path) > len(base):
            return r
    return None


def routes() -> list:
    """Every registered route, for tests and for Teams' role table."""
    return sorted([*_EXACT.values(), *_PREFIX], key=lambda r: (r.path, r.method))


_role_hook = None


def set_role_hook(fn):
    """Teams installs fn(req, role) -> member dict ({"id", "name", "role", "disciplines"}) or None, raising
    HTTPError(401 | 403) to refuse. Without a hook every request that passed the access checks may use every route:
    a local Studio has one user, and a remote one trusts whoever holds its access link."""
    global _role_hook
    _role_hook = fn


def authorize(req: "Request", role: str) -> dict | None:
    return _role_hook(req, role) if _role_hook else None


class Request:
    """What a handler gets. `room` is resolved on first use from ?project= (400 for a bad name, 404 for an
    unknown project), so routes that don't need a project never fail on one."""

    def __init__(self, handler, studio, method: str, path: str, query: dict, length: int = 0):
        self.handler, self.studio = handler, studio
        self.method, self.path, self.query = method, path, query
        self.headers = handler.headers
        self.length = length    # the declared Content-Length, already checked against the route's max_body
        self.body = None
        self.member = None
        self._room = None

    def arg(self, name: str, default=None):
        """The first value of a query parameter."""
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def room(self):
        if self._room is None:
            try:
                self._room = self.studio.room(self.arg("project"))
            except ValueError as exc:
                raise HTTPError(400, str(exc)) from None
            except LookupError as exc:
                raise HTTPError(404, str(exc)) from None
        return self._room

    def stream(self, chunk: int = 1 << 20):
        """A raw_body route's body in chunks: exactly Content-Length bytes (at most the route's max_body).
        HTTPError(400) if the client stops sending before the end."""
        left = self.length
        self.handler._read = True
        while left > 0:
            try:
                data = self.handler.rfile.read(min(chunk, left))
            except OSError:  # the client stopped sending for the handler's timeout, or went away
                data = b""
            if not data:
                raise HTTPError(400, "The upload was cut off.")
            left -= len(data)
            yield data

    def save(self, path: str, chunk: int = 1 << 20) -> int:
        """Stream the raw body into a new file (it must not exist yet); the file is removed again if the upload is
        cut off. Returns the number of bytes written."""
        n = 0
        fh = open(path, "xb")
        try:
            with fh:
                for data in self.stream(chunk):
                    fh.write(data)
                    n += len(data)
        except BaseException:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return n
