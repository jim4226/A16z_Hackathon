"""Helpers for the Teams tests (no tests here): fake revisions that record provenance exactly like
pipeline2.RobotProject.run (dirty stages "computed", the rest "reused from <parent>"), and a seeded team."""

import json
import os

from morph.robot.pipeline2 import ORDER, RobotProject, dirty_nodes
from morph.spec import Design, diff
from morph.studio.teams import auth
from morph.studio.teams.store import TeamStore

ACTUATOR = {"smart_bus": "Feetech STS3215 serial bus servo 12 V", "dynamixel": "Dynamixel XL430-W250-T servo",
            "hobby_pwm": "MG996R metal gear servo"}


def bom_rows(design: Design) -> list:
    rows = [{"item": ACTUATOR.get(design.arm.actuator_family, "servo"), "category": "actuator", "qty": design.arm.dof + 1,
             "ext_price": 17.0 * (design.arm.dof + 1)},
            {"item": "ESP32-S3-DevKitC-1", "category": "electronics", "qty": 1, "ext_price": 15.0},
            {"item": "Base shell", "category": "fabricated", "qty": 1, "ext_price": 30.9}]
    if design.electronics.config == "custom_pcb":
        rows.append({"item": "Morph controller PCBA", "category": "pcb", "qty": 1, "ext_price": 31.8})
    return rows


def build(root, rev, parent=None, changes=None, by=None, proposed_by=None, label="", ts="2026-10-12T10:00:00Z", files=()):
    """Write <root>/<rev>/ (design.json, results.json) and a ledger entry, the way the pipeline does."""
    root = str(root)
    os.makedirs(os.path.join(root, rev), exist_ok=True)
    if not os.path.exists(os.path.join(root, "meta.json")):
        with open(os.path.join(root, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"kind": "robot"}, fh)
    if parent:
        with open(os.path.join(root, parent, "design.json"), encoding="utf-8") as fh:
            base = Design.from_dict(json.load(fh))
        with open(os.path.join(root, parent, "results.json"), encoding="utf-8") as fh:
            prev = json.load(fh)
    else:
        base, prev = Design(), {}
    design = base.with_changes(changes or {})
    ch = diff(base, design) if parent else {}
    todo = dirty_nodes(ch.keys()) if parent else list(ORDER)
    res = {"rev": rev, "parent": parent, "label": label, "kind": "robot", "changes": {k: list(v) for k, v in ch.items()},
           "provenance": {}, "timings_s": {}}
    for node in ORDER:
        for key in RobotProject._keys(node):
            res[key] = {"computed_in": rev, "node": node} if node in todo else prev.get(key)
        res["provenance"][node] = "computed" if node in todo else f"reused from {parent}"
    if "bom" in todo:
        res["bom"] = {"rows": bom_rows(design), "totals": {"total": 200.0}}
    res["summary"] = {"feasible": True, "issues": [], "unit_cost": 200.0, "pick_place_success": True}
    with open(os.path.join(root, rev, "design.json"), "w", encoding="utf-8") as fh:
        json.dump(design.to_dict(), fh)
    with open(os.path.join(root, rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    for rel in files:
        full = os.path.join(root, rev, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(f"{rev}:{rel}")
    with open(os.path.join(root, "ledger.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "type": "revision", "rev": rev, "parent": parent, "label": label, "by": by,
                             "proposed_by": proposed_by, "changes": res["changes"], "recomputed": todo,
                             "design": design.to_dict()}) + "\n")
    return res


TEAM = (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["electronics"]), ("Raj", "editor", ["manufacturing"]),
        ("Mo", "editor", ["mechanical"]), ("Sue", "editor", ["sourcing"]), ("Vic", "viewer", []))


def make_team(home, specs=TEAM):
    """Create a team in `home`: the first spec is the owner (init_team), the rest join through invites.
    Returns ({name: member id}, {name: session cookie value})."""
    home = str(home)
    first = specs[0]
    init = auth.init_team(first[0], "R Cubed", home=home)
    owner = init["member"]["id"]
    ids, cookies = {first[0]: owner}, {}
    if list(first[2]) != ["generalist"] or first[1] != "owner":
        auth.update_member(owner, owner, role=first[1] if first[1] != "owner" else None, tags=first[2], home=home)
    _, cookies[first[0]] = auth.redeem_link(init["code"], home=home)
    for name, role, tags in specs[1:]:
        inv = auth.create_invite(owner, role, tags, name, home=home)
        m, cookies[name] = auth.redeem_invite(inv["code"], name, home=home)
        ids[name] = m["id"]
    return ids, cookies


def members(home) -> dict:
    return TeamStore(str(home)).read().get("members") or {}


# ------------------------------------------------------------------ a minimal team-mode router for HTTP tests
# The real router is W0b's (morph/studio/routes.py); this one calls the same hooks in the same order.

import queue  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "morph", "studio", "static")


class Req:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Hub:
    addressed = True  # delivers {"to": member} events to that member only

    def __init__(self):
        self.clients, self.lock, self.sent = [], threading.Lock(), []

    def subscribe(self, member_id=None):
        q = queue.Queue()
        with self.lock:
            self.clients.append((q, member_id))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients = [c for c in self.clients if c[0] is not q]

    def send(self, ev):
        with self.lock:
            self.sent.append(ev)
            for q, mid in self.clients:
                if ev.get("to") and ev["to"] != mid:
                    continue
                q.put(ev)


class P:
    def __init__(self, root):
        self.root = root


class Room:
    def __init__(self, root, hub):
        self.p, self.hub, self.name, self.jobs = P(str(root)), hub, os.path.basename(str(root)), []

    def enqueue(self, job):
        self.jobs.append(job)


class Studio:
    def __init__(self, home, root, hub):
        self.home, self.hub, self.p, self.team = str(home), hub, P(str(root)), True


def request(home, root, member=None, method="GET", path="/", body=None, query=None, hub=None):
    """A router Request for calling api_team handlers directly."""
    hub = hub or Hub()
    return Req(method=method, path=path, query={k: [v] for k, v in (query or {}).items()}, headers={}, body=body,
               studio=Studio(home, root, hub), room=Room(root, hub), member=member, handler=None)


def serve(home, root, extra=None):
    """Start a team-mode server on 127.0.0.1 (free port). Returns (httpd, base_url, room). `extra(handler, path, member)`
    may answer additional GET paths (the UI demo page) for any signed-in member, before the route table."""
    from morph.studio import api_team

    api_team.configure(home=str(home))
    hub = Hub()
    room = Room(root, hub)
    studio = Studio(home, root, hub)
    routes = {(m, p): (fn, opts) for m, p, fn, opts in api_team.ROUTES}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, status, obj):
            self._drain()
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _drain(self):
            # like the Studio's own handler: read a body nobody read before answering, or closing resets the
            # connection and Windows throws the answer away (ConnectionAbortedError on a refused POST)
            if self.command in ("POST", "PUT", "DELETE") and not getattr(self, "_read", False):
                self._read = True
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= 1 << 20:
                    self.rfile.read(n)

        def _go(self):
            self._read = False
            u = urlparse(self.path)
            if self.command in ("GET", "HEAD") and api_team.landing(self):
                return
            member = api_team.authenticate(self)
            if extra and member and self.command == "GET" and extra(self, u.path, member):
                return  # test-only pages (the UI demo) for any signed-in member
            if not api_team.authorize(member, self.command, u.path):
                if member is None and u.path == "/":
                    status, page, ctype, csp = api_team.signed_out_page()
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                    return
                return self._json(401 if member is None else 403, {"ok": False, "error": "not allowed"})
            if self.command == "GET" and u.path.startswith("/static/"):
                rel = u.path[len("/static/"):]
                full = os.path.realpath(os.path.join(STATIC, *rel.split("/")))
                if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
                    return self._json(404, {"ok": False})
                with open(full, "rb") as fh:
                    data = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript" if full.endswith(".js") else "text/css" if full.endswith(".css")
                                 else "font/woff2" if full.endswith(".woff2") else "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.command == "GET" and u.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                q = hub.subscribe(member["id"])
                api_team.stream(room.name, member, True, send=hub.send)
                try:
                    while True:
                        try:
                            ev = q.get(timeout=1)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except OSError:
                    pass
                finally:
                    hub.unsubscribe(q)
                    api_team.stream(room.name, member, False, send=hub.send)
                return
            hit = routes.get((self.command, u.path))
            if hit is None:
                return self._json(404, {"ok": False, "error": "Not found."})
            fn, opts = hit
            body = None
            if not opts.get("raw_body"):
                n = int(self.headers.get("Content-Length") or 0)
                self._read = True
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"ok": False, "error": "Send a JSON object."})
            req = Req(method=self.command, path=u.path, query=parse_qs(u.query), headers=self.headers, body=body,
                      studio=studio, room=room, member=member, handler=self)
            out = fn(req)
            if out is None:
                return
            self._json(out[0], out[1])

        do_GET = do_POST = do_HEAD = do_DELETE = do_PUT = _go

    class S(ThreadingHTTPServer):
        daemon_threads = True

    httpd = S(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", room


def call(base, method, path, body=None, cookie=None, token=None, headers=None, raw=None, ctype="application/json"):
    """(status, parsed JSON or text, headers) without following redirects."""
    import http.client

    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    if token:
        h["X-Morph-Token"] = token
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Type"] = ctype
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    text = r.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    conn.close()
    return r.status, parsed, dict(r.getheaders())
