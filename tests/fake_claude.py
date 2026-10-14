"""A local stand-in for Claude's Messages API, for tests and live checks that run a real Studio: start it, then start the
Studio with ANTHROPIC_BASE_URL=<its url> and any ANTHROPIC_API_KEY. The SDK honors ANTHROPIC_BASE_URL, so the Studio runs
its own code, unchanged, against this server: nothing in Morph has a test hook for it.

    with FakeClaude(price_check) as claude:       # claude.url, claude.requests (bodies), claude.headers
        ...

Each POST /v1/messages is answered by script(body): a message dict ({"content", "stop_reason"}), or (status, error
body) for an API error. `price_check` plays Live prices from the conversation so far, so it answers the same way after
a pause or a restart: read the parts list; look the first purchased part up at Mouser (the Studio pauses for the
Mouser key; a skip or a failed call is answered with a summary that says so); record what Mouser said; sum up.

`delay` (seconds) holds each answer back the way the real API takes a moment, so a page shows its in-between states.

Run as a script for a live check: python tests/fake_claude.py --port 8841 [--delay 1.2] (it prints its url and serves
until Ctrl+C).
"""

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIG = "sig-" + "F" * 40


def _thinking():
    return {"type": "thinking", "thinking": "", "signature": SIG}


def search(query, *names):
    """A server-side tool search that found `names` (the blocks Claude's answer carries before it calls them)."""
    n = len(query)
    return [{"type": "server_tool_use", "id": f"srvtoolu_{n:02d}{len(names)}", "name": "tool_search_tool_bm25", "input": {"query": query}},
            {"type": "tool_search_tool_result", "tool_use_id": f"srvtoolu_{n:02d}{len(names)}",
             "content": {"type": "tool_search_tool_search_result", "tool_references": [{"type": "tool_reference", "tool_name": x} for x in names]}}]


def call(name, tid, inp, *, query=None, found=()):
    content = [_thinking(), *(search(query, *found) if query else []), {"type": "tool_use", "id": tid, "name": name, "input": inp}]
    return {"content": content, "stop_reason": "tool_use"}


def answer(text):
    return {"content": [_thinking(), {"type": "text", "text": text}], "stop_reason": "end_turn"}


def _results(body, last_only=True):
    """[(tool name, parsed result or text, is_error)] of the tool results in the last user message (or in all of them)."""
    names, out = {}, []
    msgs = body.get("messages") or []
    for m in msgs:
        if m.get("role") == "assistant":
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b["id"]] = b["name"]
    for m in msgs[-1:] if last_only else msgs:
        for b in m.get("content") or [] if m.get("role") == "user" and isinstance(m.get("content"), list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                text = "".join(c.get("text", "") for c in b.get("content") or [] if isinstance(c, dict))
                try:
                    doc = json.loads(text)
                except ValueError:
                    doc = text
                out.append((names.get(b["tool_use_id"]), doc, bool(b.get("is_error"))))
    return out


def _keyword(item: str) -> str:
    m = re.search(r"\b([A-Z]{2,}[0-9][A-Z0-9-]*|[0-9]{2}-[0-9]{4,})\b", item)
    return m.group(1) if m else " ".join(item.split()[:3])


def price_check(body):
    """Live prices, decided from the conversation so far (see the module's docstring)."""
    res = _results(body)
    if not any(m.get("role") == "assistant" for m in body.get("messages") or []):
        return call("morph_bom_read", "toolu_bom", {})
    for name, doc, err in res:
        if name == "morph_bom_read" and isinstance(doc, dict):
            lines = doc.get("lines") or []
            if not lines:
                return answer("This revision has no purchased parts to price.")
            return call("mouser_search_keyword", "toolu_mouser", {"keyword": _keyword(lines[0]["item"])},
                        query="distributor price and stock", found=("mouser_search_keyword", "element14_search_keyword"))
        if name == "mouser_search_keyword":
            if err:
                why = doc.get("error") if isinstance(doc, dict) else "error"
                reason = {"needs_credential": "you chose to continue without the Mouser key",
                          "network": "Mouser couldn't be reached from this computer"}.get(why, f"the Mouser lookup failed ({why})")
                return answer(f"I couldn't check live prices at Mouser: {reason}. Nothing was priced and nothing was "
                              "ordered; Morph's own estimates in the BOM tab still stand.")
            parts = ((doc.get("data") or {}).get("parts") or []) if isinstance(doc, dict) else []
            if not parts:
                return answer("Mouser found no match for the first part. Nothing was ordered.")
            p = parts[0]
            br = (p.get("PriceBreaks") or [{}])[0]
            num = re.sub(r"[^0-9.]", "", str(br.get("Price") or "")) or None
            bom = next((d for n, d, _ in _results(body, last_only=False) if n == "morph_bom_read" and isinstance(d, dict)), {})
            item = (bom.get("lines") or [{"item": ""}])[0]["item"]
            line = {"item": item, "distributor": "Mouser", "part_number": p.get("MouserPartNumber") or "", "unit_price": float(num) if num else None,
                    "currency": br.get("Currency"), "quantity": br.get("Quantity"), "stock": int(p["AvailabilityInStock"]) if str(p.get("AvailabilityInStock", "")).isdigit() else None,
                    "tool": "mouser_search_keyword"}
            return call("morph_prices_record", "toolu_record", {"lines": [line]})
        if name == "morph_prices_record":
            return answer("Priced the first purchased line at Mouser. Prices change: check them before you buy. Nothing was ordered.")
    return answer("Done.")


class FakeClaude:
    """The server, on 127.0.0.1 (a free port unless given); a context manager. `requests` keeps every body as sent."""

    def __init__(self, script=price_check, port: int = 0, delay: float = 0.0):
        self.script, self.requests, self.headers, self.delay = script, [], [], delay
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, doc):
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("request-id", f"req_fake_{len(me.requests):04d}")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/v1/models"):
                    return self._send(200, {"data": [{"id": "claude-opus-5-5", "type": "model"}], "has_more": False})
                return self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": "not here"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not self.path.startswith("/v1/messages"):
                    return self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": "not here"}})
                me.requests.append(body)
                me.headers.append({k.lower(): v for k, v in self.headers.items()})
                if me.delay:
                    time.sleep(me.delay)
                try:
                    step = me.script(body)
                except Exception as exc:  # noqa: BLE001  (a script bug answers 500, the Studio pauses as unavailable)
                    return self._send(500, {"type": "error", "error": {"type": "api_error", "message": f"script: {exc!r}"}})
                if isinstance(step, tuple):
                    return self._send(step[0], step[1])
                n = len(me.requests)
                return self._send(200, {"id": f"msg_fake_{n:04d}", "type": "message", "role": "assistant", "model": body.get("model"),
                                        "stop_sequence": None, "usage": {"input_tokens": 1500, "output_tokens": 120,
                                                                         "cache_read_input_tokens": 1400 if n > 1 else 0,
                                                                         "cache_creation_input_tokens": 0 if n > 1 else 1450}, **step})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A local stand-in for Claude's Messages API (Live prices script)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.0, help="seconds before each answer")
    a = ap.parse_args()
    fake = FakeClaude(price_check, a.port, delay=a.delay)
    print(f"fake Claude (Live prices script) at {fake.url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.close()
