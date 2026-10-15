"""The SubQ Snap client against a local stub server (never api.subq.ai): answers, the 7-day cache, retries within the
time budget, pausing after 503s, switching off on 401/402/403 until the key changes, no retry on 422, image checks,
and never a key or a request in logs, status or the cache. Plus the Keys entry, Test keys and `morph subq`."""

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from morph import cli, keys, settings
from morph.studio import server, subq

KEY = "sk-stub-only-not-a-real-key-0123456789"
SECRET_TEXT = "pick up grandma's private letters"
OK = (200, {"model": subq.SNAP, "answers": {"q": {"type": "noul", "noul": 0.91},
                                            "r": {"type": "choice", "choice": "b", "confidence": 0.82,
                                                  "probabilities": {"a": 0.18, "b": 0.82}},
                                            "s": {"type": "score", "score": 1.7, "legend": {"0": "low", "1": "mid", "2": "high"},
                                                  "confidence": 0.7}},
            "usage": {"input_tokens": 50, "output_tokens": 0}}, 0, {})


class Stub:
    def __init__(self):
        self.script, self.seen, self.paths = [], [], []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, code, body, delay, headers):
                time.sleep(delay)
                raw = json.dumps(body).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass

            def do_GET(self):
                stub.paths.append(self.path)
                self._answer(200, {"success": True, "data": {"status": "healthy"}}, 0, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                stub.seen.append({"auth": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(n))})
                stub.paths.append(self.path)
                code, body, delay, headers = stub.script.pop(0) if stub.script else (500, {"detail": {"error_type": "x",
                                                                                                    "message": "empty"}}, 0, {})
                self._answer(code, body, delay, headers)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


@pytest.fixture
def stub(monkeypatch, tmp_path):
    s = Stub()
    monkeypatch.setattr(subq, "BASE", s.base)
    monkeypatch.setattr(subq, "CACHE", str(tmp_path / "subq-cache"))
    monkeypatch.setenv("SUBQ_API_KEY", KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    subq.reset()
    yield s
    subq.reset()
    s.httpd.shutdown()
    s.httpd.server_close()


QS = {"q": subq.noul("Is it fragile?"), "r": subq.choice("Which?", {"a": None, "b": "the second"}),
      "s": subq.score("How heavy?", ["low", "mid", "high"])}


def ask(state="x", **kw):
    return subq.decide({"request": state}, QS, **kw)


def test_an_answer_and_how_to_read_it(stub):
    stub.script = [OK]
    res = ask()
    assert res["source"] == "subq" and res["usage"]["input_tokens"] == 50 and res["ms"] >= 0
    assert stub.seen[0]["auth"] == f"Bearer {KEY}" and stub.paths == ["/v1/systemone"]
    assert stub.seen[0]["body"] == {"model": "subq-snap-preview", "state": {"request": "x"}, "questions": QS}
    assert subq.p_yes(res, "q") == 0.91 and subq.pick(res, "r") == "b" and subq.pick(res, "r", min_conf=0.9) is None
    assert subq.level(res, "s") == (1.7, "high") and subq.level(res, "q") == (None, None)
    assert subq.p_yes(res["answers"], "q") == 0.91 and subq.p_yes(res, "nope") is None and subq.pick(None, "r") is None
    assert subq.status() == {"configured": True, "disabled": None, "last_error": None, "cooling": False}


def test_answers_are_cached_for_a_week_without_the_request(stub):
    stub.script = [OK, OK]
    assert ask(SECRET_TEXT)["source"] == "subq"
    again = ask(SECRET_TEXT)
    assert again["source"] == "cache" and again["ms"] == 0 and again["answers"] == OK[1]["answers"]
    assert len(stub.seen) == 1                                       # the second answer came from the cache
    files = os.listdir(subq.CACHE)
    assert len(files) == 1 and len(files[0]) == 64 + 5
    text = open(os.path.join(subq.CACHE, files[0]), encoding="utf-8").read()
    assert SECRET_TEXT not in text and "request" not in text and set(json.loads(text)) == {"answers", "usage"}
    old = time.time() - subq.CACHE_TTL_S - 10
    os.utime(os.path.join(subq.CACHE, files[0]), (old, old))
    assert ask(SECRET_TEXT)["source"] == "subq"                      # a week later: asked again


def test_photos_are_checked_and_never_cached(stub):
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()
    good = [{"mediaType": "image/png", "base64": png}]
    stub.script = [OK, OK]
    assert ask(images=good)["source"] == "subq" and ask(images=good)["source"] == "subq"
    assert len(stub.seen) == 2 and stub.seen[0]["body"]["images"] == good
    big = base64.b64encode(b"0" * (subq.MAX_IMAGE + 1)).decode()
    three = base64.b64encode(b"0" * (3 << 20)).decode()
    for bad in ([dict(good[0])] * 5, [{"mediaType": "image/gif", "base64": png}], [{"mediaType": "image/png", "base64": "data:image/png;base64," + png}],
                [{"mediaType": "image/png", "base64": png + "\n"}], [{"mediaType": "image/png", "base64": "!!!"}],
                [{"mediaType": "image/png", "base64": big}], [{"mediaType": "image/png", "base64": three}] * 3,
                [{"mediaType": "image/png", "base64": png, "url": "x"}], "not a list", []):
        assert ask(images=bad) is None, str(bad)[:60]
    assert len(stub.seen) == 2                                       # none of those was sent
    assert subq.status()["last_error"]["kind"] == "invalid_request" and not subq.status()["cooling"]


def test_429_is_retried_once(stub):
    stub.script = [(429, {"error": {"message": "slow down", "type": "rate_limit_error", "code": "rate_limit_exceeded"}}, 0,
                    {"Retry-After": "0.2"}), OK]
    t0 = time.monotonic()
    assert ask(cache=False)["source"] == "subq"
    assert len(stub.seen) == 2 and 0.2 <= time.monotonic() - t0 < 3


def test_503s_pause_subq_for_a_minute(stub):
    busy = (503, {"detail": {"error_type": "service_unavailable", "message": "busy"}}, 0, {"Retry-After": "0"})
    stub.script = [busy, busy, OK]
    assert ask(cache=False) is None and len(stub.seen) == 2
    st = subq.status()
    assert st["cooling"] and st["last_error"]["status"] == 503 and st["last_error"]["kind"] == "service_unavailable"
    assert ask(cache=False) is None and len(stub.seen) == 2          # paused: not even tried
    assert 55 < subq._state["cool_until"] - time.time() <= 60
    subq._state["cool_until"] = time.time() - 1                      # a minute later
    assert ask(cache=False)["source"] == "subq"


@pytest.mark.parametrize("code,reason", [(401, "key_rejected"), (402, "no_credits"), (403, "model_not_allowed")])
def test_a_refused_key_switches_subq_off_until_the_key_changes(stub, monkeypatch, code, reason):
    stub.script = [(code, {"detail": {"error_type": "x", "message": "no"}}, 0, {}), OK]
    assert subq.enabled() and ask(cache=False) is None and len(stub.seen) == 1   # never retried
    assert not subq.enabled() and subq.status()["disabled"] == reason
    assert ask(cache=False) is None and len(stub.seen) == 1
    monkeypatch.setenv("SUBQ_API_KEY", KEY + "-new")                  # a new key: on again
    assert subq.enabled() and ask(cache=False)["source"] == "subq"


def test_422_is_our_mistake_and_never_retried(stub):
    stub.script = [(422, {"detail": {"error_type": "validation_error", "message": "questions/q: bad"}}, 0, {}), OK]
    assert ask(cache=False) is None and len(stub.seen) == 1
    assert subq.status()["last_error"]["message"] == "questions/q: bad" and not subq.status()["cooling"]


def test_the_timeout_covers_every_try(stub):
    slow = (200, OK[1], 2.5, {})
    stub.script = [slow, slow]
    t0 = time.monotonic()
    assert ask(cache=False, timeout=1.0) is None
    assert time.monotonic() - t0 < 2.2                               # not 2 x 2.5 s
    assert subq.status()["cooling"]


def test_unreachable_and_switched_off(stub, monkeypatch):
    monkeypatch.setattr(subq, "BASE", "http://127.0.0.1:9/v1")       # nothing listens there
    assert ask(cache=False, timeout=2) is None and subq.status()["last_error"]["kind"] == "unreachable"
    subq.reset()
    settings.save({"network_enabled": False})
    assert not subq.enabled() and ask(cache=False) is None and stub.seen == []
    settings.save({"network_enabled": True})
    monkeypatch.delenv("SUBQ_API_KEY")
    assert not subq.enabled() and ask() is None and subq.status()["configured"] is False


def test_a_redirect_is_never_followed_with_the_key(stub):
    """urllib resends every header, Authorization included, to wherever a 302 points: keyed requests refuse it."""
    stub.script = [(302, {}, 0, {"Location": stub.base + "/elsewhere"})] * 2
    assert ask(cache=False, retries=0) is None
    assert subq.check(timeout=5)["ok"] is False
    assert stub.paths == ["/v1/systemone", "/v1/systemone"]          # nothing was sent to /elsewhere


def test_bad_questions_never_raise(stub):
    for qs in ({}, {"q": subq.choice("one option", {"a": None})}, {"q": subq.score("x", ["one"])}, {"x" * 101: subq.noul("x")},
               {"q": {"type": "maybe", "instructions": "x"}}, {"q": subq.noul("")}, "q", None):
        assert subq.decide("state", qs) is None
    assert subq.decide(object(), {"q": subq.noul("x")}) is None      # not JSON
    assert subq.decide({"v": float("nan")}, {"q": subq.noul("x")}) is None
    assert stub.seen == []


def test_nothing_logs_the_key_or_the_request(stub, capsys):
    stub.script = [(401, {"detail": {"error_type": "authentication_error", "message": "Invalid API key"}}, 0, {})]
    assert ask(SECRET_TEXT, cache=False) is None
    out = capsys.readouterr()
    everything = out.out + out.err + json.dumps(subq.status())
    assert KEY not in everything and SECRET_TEXT not in everything


def test_check_health_and_lint(stub, monkeypatch):
    stub.script = [OK]
    assert subq.check() == {"ok": True, "detail": "key works: Morph can ask SubQ Snap"}
    stub.script = [(402, {"detail": {"error_type": "insufficient_credits", "message": "x"}}, 0, {})]
    out = subq.check()
    assert not out["ok"] and "no credits" in out["detail"] and subq.status()["disabled"] == "no_credits"
    assert subq.health() == {"ok": True, "detail": "reachable"} and stub.paths[-1] == "/health"
    stub.script = [(401, {"detail": {"error_type": "authentication_error", "message": "Missing API key"}}, 0, {}),
                   (422, {"detail": {"error_type": "validation_error", "message": "state: required"}}, 0, {})]
    assert subq.lint({"model": subq.SNAP, "state": "x", "questions": QS})[0] is True
    assert stub.seen[-1]["auth"] is None                             # lint never sends a key
    assert subq.lint({}) == (False, "422 validation_error: state: required")
    monkeypatch.delenv("SUBQ_API_KEY")
    assert subq.check() == {"ok": False, "detail": "no key set"}


@pytest.mark.parametrize("how", ["MORPH_OFFLINE", "settings"])
def test_lint_sends_nothing_while_network_lookups_are_off(stub, monkeypatch, capsys, how):
    """`morph subq --lint` sends the request text to SubQ (without a key): not while network lookups are off."""
    import argparse
    if how == "MORPH_OFFLINE":
        monkeypatch.setenv("MORPH_OFFLINE", "1")
    else:
        settings.save({"network_enabled": False})
    assert subq.lint({"model": subq.SNAP, "state": "x", "questions": QS}) == (False, "not sent: network lookups are off")
    rc = cli.cmd_subq(argparse.Namespace(text=SECRET_TEXT, lint=True, json=False, no_cache=False, timeout=2.0))
    out = capsys.readouterr().out
    assert rc == 1 and out.startswith("Not sent: network lookups are off") and stub.seen == [] and stub.paths == []
    assert ("MORPH_OFFLINE=1" in out) == (how == "MORPH_OFFLINE")


# ------------------------------------------------------------------ the rest of Morph

def test_keys_and_integrations_know_subq(stub, monkeypatch, tmp_path):
    entry = next(k for k in keys.KEYS if k["name"] == "SUBQ_API_KEY")
    assert entry["service"] == "SubQ" and entry["label"] == "SubQ API key (sk-...)" and "console.subq.ai" in entry["where"]
    monkeypatch.setattr(subq, "check", lambda: {"ok": True, "detail": "stub"})
    assert keys.check(["SubQ"]) == {"SubQ": {"ok": True, "detail": "stub"}}
    assert server.integrations()["subq"] == {"configured": True, "decide": True, "disabled": None}
    monkeypatch.delenv("SUBQ_API_KEY")
    assert "SubQ" not in keys.check() and server.integrations()["subq"]["configured"] is False


def test_saving_a_subq_key_gives_it_a_fresh_start(stub, monkeypatch, tmp_path):
    from test_keys_and_launch import _start

    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    stub.script = [(402, {"detail": {"error_type": "insufficient_credits", "message": "x"}}, 0, {})]
    assert ask(cache=False) is None and subq.status()["disabled"] == "no_credits"
    call, _, httpd = _start(tmp_path)
    try:
        code, body, _ = call("POST", "/api/keys", {"values": {"SUBQ_API_KEY": KEY}})   # the same key, saved again
        out = json.loads(body)
        assert code == 200 and out["changed"] == ["SUBQ_API_KEY"] and KEY.encode() not in body
        assert subq.status()["disabled"] is None and out["integrations"]["subq"]["decide"] is True
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_morph_subq_command(stub, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / "no.env"))  # never this computer's own .env
    stub.script = [(200, {"answers": {"route": {"choice": "new_robot", "confidence": 0.88}, "harm": {"noul": 0.01},
                                      "fragility": {"score": 1.9, "legend": {"0": "Robust", "1": "Needs care", "2": "Very fragile"}},
                                      "payload": {"score": 0.2, "legend": {"0": "Under 50 g"}}, "says_object": {"noul": 0.97}},
                          "usage": {"input_tokens": 900}}, 0, {})]
    assert cli.main(["subq", "a robot that picks strawberries"]) == 0
    out = capsys.readouterr().out
    assert "SubQ Snap (AI), subq" in out and "route: new_robot (confidence 0.88)" in out and "harm: p(yes) = 0.01" in out
    assert "fragility: 1.90 (Very fragile)" in out and KEY not in out
    body = stub.seen[0]["body"]
    assert body["state"]["request"] == "a robot that picks strawberries" and set(body["questions"]) == {
        "route", "harm", "fragility", "payload", "says_object"}
    stub.script = [(503, {"detail": {"error_type": "service_unavailable", "message": "busy"}}, 0, {})] * 2
    assert cli.main(["subq", "another request", "--no-cache"]) == 1
    assert "SubQ didn't answer" in capsys.readouterr().out


def test_morph_subq_needs_a_key(monkeypatch, capsys):
    monkeypatch.delenv("SUBQ_API_KEY", raising=False)
    monkeypatch.setattr(keys, "ENV_PATH", os.path.join(os.sep, "nonexistent", ".env"))
    assert cli.main(["subq", "anything"]) == 1
    assert "SUBQ_API_KEY is not set" in capsys.readouterr().out
