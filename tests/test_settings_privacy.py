"""Non-secret settings and what they switch off: Keewano events are opt-in, calibration is opt-in and never from
synthetic or thin data, and network off means no Claude, Darwin, BoardRepo or Keewano call. Offline."""

import argparse
import json
import os
import re

import pytest

from morph import agent, boardrepo, cli, darwin, electronics, keewano, keys, settings
from morph.pipeline import Project
from morph.studio import server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "raw-egg")


def no_network(*a, **k):
    raise AssertionError("must not reach the network")


def test_defaults_and_round_trip():
    assert settings.load() == {"network_enabled": True, "sourcing_enabled": True,
                               "telemetry_enabled": False, "calibration_enabled": False}
    assert not os.path.exists(settings.path())
    out = settings.save({"telemetry_enabled": True, "calibration_enabled": True})
    assert out["telemetry_enabled"] and out["calibration_enabled"] and out["network_enabled"]
    with open(settings.path(), encoding="utf-8") as fh:
        assert json.load(fh) == out
    assert settings.path().startswith(os.environ["MORPH_HOME"])
    assert settings.describe() == "network lookups on, Darwin sourcing on, Keewano events on, Keewano calibration on"
    assert [f for f in os.listdir(settings.home()) if f.endswith(".tmp")] == []


@pytest.mark.parametrize("changes", [{}, None, [], {"network_enabled": "no"}, {"network_enabled": 1},
                                     {"ANTHROPIC_API_KEY": True}, {"telemetry_enabled": None}])
def test_only_known_switches_and_booleans(changes):
    with pytest.raises(ValueError):
        settings.save(changes)
    assert not os.path.exists(settings.path())


def test_a_damaged_file_falls_back_to_the_defaults():
    os.makedirs(settings.home(), exist_ok=True)
    for text in ("{not json", "[true]", json.dumps({"telemetry_enabled": "yes", "network_enabled": False, "x": 1})):
        with open(settings.path(), "w", encoding="utf-8") as fh:
            fh.write(text)
        got = settings.load()
        assert got["telemetry_enabled"] is False and set(got) == set(settings.DEFAULTS)
    assert settings.load()["network_enabled"] is False  # the valid value in the last file still counts


def test_offline_env_and_switch_turn_everything_off(monkeypatch):
    assert settings.network_allowed() and settings.sourcing_enabled() and not settings.telemetry_enabled()
    settings.save({"telemetry_enabled": True, "calibration_enabled": True})
    assert settings.telemetry_enabled() and settings.calibration_enabled()
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    assert not any((settings.network_allowed(), settings.sourcing_enabled(), settings.telemetry_enabled(),
                    settings.calibration_enabled()))
    monkeypatch.delenv("MORPH_OFFLINE")
    settings.save({"network_enabled": False})
    assert not any((settings.network_allowed(), settings.telemetry_enabled(), settings.calibration_enabled()))


def test_telemetry_env_opt_in(monkeypatch):
    assert not settings.telemetry_enabled()
    monkeypatch.setenv("MORPH_TELEMETRY_ENABLED", "1")
    assert settings.telemetry_enabled()
    settings.save({"network_enabled": False})
    assert not settings.telemetry_enabled()


# ------------------------------------------------------------------ Keewano

def test_keys_alone_never_send_events(monkeypatch):
    monkeypatch.setenv("KEEWANO_API_KEY", "test-key")
    monkeypatch.setattr(keewano, "_post", no_network)
    assert keewano.send("project", [("Revision", "r001")], key="explicit-key") is False
    assert keewano.emit("project", {"type": "human_time", "step": "test", "minutes": 5}) is False


def test_events_go_out_once_switched_on_and_stop_offline(monkeypatch):
    monkeypatch.setenv("KEEWANO_API_KEY", "test-key")
    monkeypatch.setattr(keewano, "_registered", True)
    calls = []
    monkeypatch.setattr(keewano, "_post", lambda path, body, key: calls.append(path))
    settings.save({"telemetry_enabled": True})
    assert keewano.send("project", [("Revision", "r001")]) and calls == ["in"]
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    assert keewano.send("project", [("Revision", "r002")]) is False and calls == ["in"]


def test_rehearsal_measurements_never_carry_a_force_ratio():
    entry = {"type": "measurement", "rev": "r002", "error_vs_prediction": {"min_hold_force_n": 0.4}}
    real = dict(keewano.events_for({**entry, "actual": {"min_hold_force_n": 1.2}}))
    fake = dict(keewano.events_for({**entry, "actual": {"min_hold_force_n": 1.2, "simulated": True}}))
    assert real["HoldForceRatioPct"] == 150 and "HoldForceRatioPct" not in fake
    assert "HoldForceRatioPct" not in dict(keewano.events_for({**entry, "synthetic": True, "actual": {"min_hold_force_n": 1.2}}))


@pytest.fixture
def keewano_query(monkeypatch):
    monkeypatch.setenv("KEEWANO_PAT", "kwpat-test")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "p1")
    monkeypatch.setattr(keewano, "_ratio_cache", {})
    seen = []

    def answer(rows):
        monkeypatch.setattr(keewano, "query", lambda sql, max_rows=100: (seen.append(sql), rows)[1])
        keewano._ratio_cache.clear()

    return seen, answer


def test_calibration_is_off_by_default(keewano_query):
    seen, answer = keewano_query
    answer([[150, 9]])
    assert keewano.hold_ratio("contour", "PETG") is None and seen == []


def test_calibration_needs_three_non_synthetic_builds(keewano_query):
    seen, answer = keewano_query
    settings.save({"calibration_enabled": True})
    answer([[150, 2]])
    assert keewano.hold_ratio("contour", "PETG") is None
    assert "\"Project\" NOT LIKE 'synthetic-%'" in seen[-1] and "'contour'" in seen[-1] and "'PETG'" in seen[-1]
    answer([[None, 0]])
    assert keewano.hold_ratio("contour", "PETG") is None
    answer([[112, 8]])
    assert keewano.hold_ratio("contour", "PETG") == {"ratio": 1.12, "n": 8, "source": "keewano", "min_builds": 3}
    answer([[900, 30]])
    assert keewano.hold_ratio("flat", "PLA")["ratio"] == 3.0  # still clamped
    settings.save({"network_enabled": False})
    assert keewano.hold_ratio("v", "PLA") is None


def test_queries_stay_home_with_the_network_off(monkeypatch):
    monkeypatch.setenv("KEEWANO_PAT", "kwpat-test")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "p1")
    monkeypatch.setattr(keewano, "open_keyed", no_network)
    settings.save({"network_enabled": False})
    assert keewano.query("SELECT 1") is None


def test_keewano_keys_never_follow_a_redirect(monkeypatch):
    """urllib resends the k-token header to wherever a 302 points; keyed requests refuse the redirect instead."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append((self.command, self.path, self.headers.get("k-token")))
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(keewano, "API", base)
    monkeypatch.setattr(keewano, "MCP", base + "/mcp")
    monkeypatch.setenv("KEEWANO_PAT", "kwpat-test-only")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "p1")
    try:
        with pytest.raises(OSError):  # urllib's HTTPError: 302 refused
            keewano._post("events", {"a": 1}, "test-only-key")
        assert keewano.query("SELECT 1") is None
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert [path for _, path, _ in seen] == ["/events", "/mcp"]  # nothing went to /stolen


def test_calibrated_target_says_logged_not_measured(monkeypatch):
    monkeypatch.setattr(keewano, "hold_ratio", lambda pad, mat: {"ratio": 1.4, "n": 9, "source": "keewano"})
    p = Project(EXAMPLE)
    r = p.load_results("r001")
    plan = electronics.plan(p.load_design("r001"), r["analysis"], r["sim"])
    note = next(w for w in plan["warnings"] if "Keewano" in w)
    assert "logged" in note and "measured" not in note and "seeded demo data" in note


# ------------------------------------------------------------------ network off: Darwin, BoardRepo, Claude

def test_darwin_uses_saved_answers_when_lookups_are_off(tmp_path, monkeypatch):
    q = "buy Feetech STS3215 serial bus servo, price and shipping"
    shipped = tmp_path / "shipped"
    shipped.mkdir()
    (shipped / os.path.basename(darwin._cache_path(q))).write_text(json.dumps({"query": q, "ok": True, "agents": [{"name": "x"}]}))
    monkeypatch.setattr(darwin, "CACHE", str(tmp_path / "empty"))
    monkeypatch.setattr(darwin, "SHIPPED", str(shipped))
    monkeypatch.setattr(darwin, "urlopen", no_network)
    for switch in ({"network_enabled": False}, {"network_enabled": True, "sourcing_enabled": False}):
        settings.save(switch)
        out = darwin.search(q)
        assert out["cached"] is True and out["agents"][0]["name"] == "x" and "off" in out["error"]
    assert darwin.search("never searched before")["ok"] is False


def test_boardrepo_and_claude_stay_home_with_the_network_off(monkeypatch):
    monkeypatch.setattr(boardrepo, "urlopen", no_network)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-not-real")
    settings.save({"network_enabled": False})
    assert boardrepo.fetch("evroc/robotic-gripper")["ok"] is False
    assert agent._client() is None and "network lookups" in agent.research("anything")
    monkeypatch.setenv("KEEWANO_PAT", "kwpat-test")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "p1")
    checks = keys.check()
    assert set(checks) == {"Claude", "Keewano"} and not any(c["ok"] for c in checks.values())
    assert all("network lookups are off" in c["detail"] for c in checks.values())


def test_integrations_say_what_will_actually_be_called(monkeypatch):
    i = server.integrations()
    assert i == {"claude": {"configured": False, "chat": False},
                 "keewano": {"configured": False, "send": False, "query": False, "calibration": False},
                 "darwin": {"search": True}, "subq": {"configured": False, "decide": False, "disabled": None},
                 "network": True}
    for k, v in (("ANTHROPIC_API_KEY", "k"), ("KEEWANO_API_KEY", "k"), ("KEEWANO_PAT", "k"), ("KEEWANO_PROJECT_ID", "p"),
                 ("SUBQ_API_KEY", "k")):
        monkeypatch.setenv(k, v)
    i = server.integrations()
    assert i["claude"]["chat"] and i["keewano"]["configured"] and i["keewano"]["query"] and i["subq"]["decide"]
    assert not i["keewano"]["send"] and not i["keewano"]["calibration"]  # keys alone: no events, no calibration
    settings.save({"telemetry_enabled": True, "calibration_enabled": True})
    i = server.integrations()
    assert i["keewano"]["send"] and i["keewano"]["calibration"]
    settings.save({"network_enabled": False})
    i = server.integrations()
    assert i["claude"] == {"configured": True, "chat": False} and i["network"] is False and not i["darwin"]["search"]
    assert i["subq"] == {"configured": True, "decide": False, "disabled": None}
    assert not any((i["keewano"]["send"], i["keewano"]["query"], i["keewano"]["calibration"]))


def test_doctor_prints_the_settings_and_skips_network_checks_when_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(keys, "TEMPLATE", str(tmp_path / "no-template"))
    monkeypatch.setattr(darwin, "search", no_network)
    settings.save({"network_enabled": False})
    cli.cmd_doctor(argparse.Namespace(offline=False))
    out = capsys.readouterr().out
    assert "network lookups off, Darwin sourcing on, Keewano events off, Keewano calibration off" in out
    assert "network checks skipped" in out


def test_no_measured_or_real_build_claims_for_seeded_data():
    claims = re.compile(r"calibrat\w* (the grip force )?from real builds|measured results tune|measured builds for"
                        r"|from \$\{cal\.n\} measured|no measured \$|grip force calibrated from real")
    for rel in ("morph/studio/static/app.js", "morph/studio/static/keys.js", "morph/studio/static/present.js",
                "README.md", "docs/SPONSORS.md", "morph/electronics.py"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            assert not claims.search(fh.read()), rel
