"""Fakes shared by the toolbox tests (a module rather than a conftest): Claude through the real anthropic SDK over a
mock transport, recording every request body exactly as the SDK sent it; the FixParts fixture API answered in-process
(nothing leaves the test); a resolver that maps every host to one public address; and two core tools."""

import json
import os
import socket

import anthropic
import httpx2

from morph.studio.toolbox import Builtin, Toolbox

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "tools")
KEY = "fxp_" + "Q7w8E9r0T1y2U3i4O5p6A7s8"
KEY2 = "fxp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8n7"
ENV = "MORPH_TOOL_FIXPARTS_KEY"
PUBLIC_IP = "93.184.216.34"
SYSTEM = "You are Morph's sourcing agent. Price the purchased parts of the robot the person names."


class ScriptedClaude:
    """Each step answers one request: a dict (the message's content and stop_reason), fn(body) -> dict, or
    (status, error body) for an API error. Requests are kept as the JSON bodies and headers that went out."""

    def __init__(self, *steps):
        self.steps, self.requests, self.headers = list(steps), [], []

    def _handle(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.headers.append({k.lower(): v for k, v in request.headers.items()})
        if not self.steps:
            return httpx2.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "no step left"}})
        step = self.steps.pop(0)
        if callable(step):
            step = step(body)
        if isinstance(step, tuple):
            return httpx2.Response(step[0], json=step[1])
        n = len(self.requests)
        msg = {"id": f"msg_{n:03d}", "type": "message", "role": "assistant", "model": body["model"], "stop_sequence": None,
               "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 1000 if n > 1 else 0,
                         "cache_creation_input_tokens": 0 if n > 1 else 1100}, **step}
        return httpx2.Response(200, json=msg, headers={"request-id": f"req_{n:03d}"})

    def client(self):
        return anthropic.Anthropic(api_key="test-not-a-real-key", max_retries=0,
                                   http_client=httpx2.Client(transport=httpx2.MockTransport(self._handle)))


def search_and_call(*calls, query="distributor price stock", thinking=True):
    """An assistant turn: (empty) thinking, a server-side tool search that found the tools, then client tool calls,
    each (name, tool_use_id, input)."""
    names = sorted({c[0] for c in calls})
    content = [{"type": "thinking", "thinking": "", "signature": "sig-" + "A" * 40}] if thinking else []
    content += [{"type": "server_tool_use", "id": "srvtoolu_01", "name": "tool_search_tool_bm25", "input": {"query": query}},
                {"type": "tool_search_tool_result", "tool_use_id": "srvtoolu_01",
                 "content": {"type": "tool_search_tool_search_result",
                             "tool_references": [{"type": "tool_reference", "tool_name": n} for n in names]}}]
    content += [{"type": "tool_use", "id": tid, "name": n, "input": inp} for n, tid, inp in calls]
    return {"content": content, "stop_reason": "tool_use"}


def final(text="Done."):
    return {"content": [{"type": "thinking", "thinking": "", "signature": "sig-final"}, {"type": "text", "text": text}],
            "stop_reason": "end_turn"}


class Vendor:
    """The FixParts fixture API, answered in-process (Toolbox(http_transport=vendor.transport)). `calls` records
    (method, path, headers, body); `reject` answers 401; `crash` raises it from inside a call (a Studio dying mid-call)."""

    def __init__(self):
        self.calls, self.reject, self.crash, self.ips = [], False, None, []

    def resolver(self, host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    def transport(self, r, ip, timeout, max_bytes):
        self.ips.append(ip)
        self.calls.append((r["method"], r["path"] + ("?" + "&".join(f"{k}={v}" for k, v in r["query"]) if r["query"] else ""),
                           dict(r["headers"]), r["body"]))
        if self.crash is not None:
            raise self.crash
        hdr = {"content-type": "application/json"}
        authed = r["headers"].get("Authorization") in (f"Bearer {KEY}", f"Bearer {KEY2}")
        if r["path"].startswith("/v1/categories"):
            return 200, hdr, json.dumps({"categories": [{"id": "servo", "name": "Servo motors"}]}).encode()
        if not authed or self.reject:
            return 401, hdr, b'{"error": "bad key"}'
        if r["path"].startswith("/v1/parts/"):
            doc = {"data": {"offers": [{"distributor": "FixParts", "stock": 412, "price_usd": 13.9,
                                        "echo": r["headers"].get("Authorization")}]}, "meta": {"total": 1}}
            return 200, hdr, json.dumps(doc).encode()
        if r["path"] == "/v1/quotes":
            body = json.loads(r["body"])
            return 200, hdr, json.dumps({"quote": {"id": "q_1", "part": body["part"], "qty": body["qty"], "unit_usd": 12.5}}).encode()
        return 404, hdr, b"{}"


def bom_read(inp, ctx):
    return {"project": ctx["project"], "lines": [{"item": "Feetech STS3215 servo", "mpn": "STS3215", "qty": 6}]}


BOM = Builtin("morph_bom_read", "Read the purchased parts (BOM lines) of the project's newest revision.",
              {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, bom_read, "Parts list")
NOTE = Builtin("morph_note_add", "Add one line to the project's sourcing notes (a local file; nothing leaves Morph).",
               {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
               lambda inp, ctx: {"ok": True, "saved": len(inp["text"])}, "Sourcing notes")


def toolbox(home, claude=None, vendor=None, events=None, **kw):
    """A Toolbox over the fixture catalog that runs everything in the calling thread."""
    vendor = vendor or Vendor()
    opts = dict(emit=(events.append if events is not None else None),
                client_factory=(lambda: claude.client()) if claude is not None else (lambda: None),
                spawn=lambda fn: fn(), curated_dirs=[FIXTURES], http_transport=vendor.transport,
                http_resolver=vendor.resolver, builtins={BOM.name: BOM, NOTE.name: NOTE}, network=lambda: True)
    opts.update(kw)
    return Toolbox(str(home), **opts)


def private(path, mode: int = 0o600) -> bool:
    """POSIX file modes; Windows has none (os.chmod only sets read-only), so a file there is as private as its folder."""
    return os.name == "nt" or (os.stat(path).st_mode & 0o777) == mode


def secrets_absent(blob, *values) -> bool:
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    return all(v not in text for v in values)
