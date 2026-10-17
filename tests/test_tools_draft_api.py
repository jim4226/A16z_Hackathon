"""Morph writes a tool (preview) over HTTP (morph/studio/api_tools.py): drafting from the Studio, the draft's events on the
room, the review, approve bound to the spec's hash, the next run offering the tool and asking for its key, the settings
and limits, the Teams role matrix (only the owner drafts, approves or discards), and Darwin's suggestions for a need no
tool covers, from Darwin's recorded public answers. Claude is the real SDK over a mock transport (toolbox_helpers), and
for the drafts that break the rules the fake Claude server on 127.0.0.1 (tests/fake_claude.py); nothing leaves the
test."""

import json
import os

import pytest

import fake_claude as F
from morph import darwin, settings
from morph.studio import api_tools
from morph.studio.teams import roles
from morph.studio.toolbox import audit, catalog
from test_team_util import build
from test_tools_api import World, _start
from toolbox_helpers import final, search_and_call

DOCS = F.GEARBASE_DOCS_URL
GB_ENV = "MORPH_TOOL_GEARBASE_KEY"
DARWIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "darwin")
ROUTES = [("POST", "/api/tools/draft", "owner"), ("GET", "/api/tools/drafts", "viewer"), ("POST", "/api/tools/drafts/approve", "owner"),
          ("POST", "/api/tools/drafts/discard", "owner"), ("GET", "/api/tools/suggest", "editor")]
TEAM = (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["sourcing"]), ("Vic", "viewer", []))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(GB_ENV, "unset")
    monkeypatch.delenv(GB_ENV)
    monkeypatch.setattr(darwin, "CACHE", str(tmp_path / "darwin-cache"))  # never the person's own cache
    monkeypatch.setattr(darwin, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("no network in tests")))
    api_tools.agent("test_prices", title="Test prices", start=_start)
    made = []

    def make(*steps, team=False):
        w = World(tmp_path, monkeypatch, steps, team)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()
    api_tools.AGENTS.pop("test_prices", None)
    from morph.studio import api_team
    from morph.studio.teams import activity, auth
    api_team.configure(home=None, enabled=False)
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()


def events_of(q, kind="tool_draft"):
    out = []
    while not q.empty():
        e = q.get_nowait()
        if e["type"] == kind:
            out.append(e)
    return out


def test_draft_review_approve_and_the_next_run_asks_for_the_new_tools_key(world):
    w = world(F.drafting(), search_and_call(("gearbase_search_parts", "toolu_gb", {"keyword": "STS3215"})), final("ok"))
    q = w.studio.room().hub.subscribe()
    s, out = w.call("POST", "/api/tools/draft?project=egg-picker", {"need": "Live price breaks and stock from Gearbase",
                                                                    "docs_url": DOCS})
    assert s == 202 and out["already"] is False
    d = out["draft"]
    assert d["status"] == "ready" and d["project"] == "egg-picker" and d["docs_host"] == "docs.gearbase-morph.com"
    assert [(e["status"], e["draft"], e["project"]) for e in events_of(q)] == [("drafting", d["id"], "egg-picker"), ("ready", d["id"], "egg-picker")]
    s, listed = w.call("GET", "/api/tools/drafts?project=egg-picker")
    assert s == 200 and [x["id"] for x in listed["drafts"]] == [d["id"]] and listed["can"]["draft"] is True
    build(os.path.join(w.home, "so101"), "r001")
    assert w.call("GET", "/api/tools/drafts?project=so101")[1]["drafts"] == []  # drafts show in the project they came from
    s, one = w.call("GET", f"/api/tools/drafts?id={d['id']}")
    assert s == 200 and one["draft"]["sha256"] == d["sha256"] and one["draft"]["card"]["key"]["env"] == GB_ENV
    s, out = w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": "f" * 64})
    assert s == 409 and "isn't the spec this draft holds" in out["error"]
    s, out = w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": d["sha256"]})
    assert s == 200 and out["draft"]["status"] == "installed" and events_of(q)[-1]["status"] == "installed"
    tools = {t["name"]: t for t in w.call("GET", "/api/tools")[1]["tools"]}
    assert tools["gearbase_search_parts"]["enabled"] and tools["gearbase_search_parts"]["drafted"]["sha256"] == d["sha256"]
    assert GB_ENV in {k["name"] for k in w.call("GET", "/api/keys")[1]["keys"]}
    s, out = w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    run = out["run"]
    assert s == 202 and run["waiting_on"] == ["credential"] and run["cards"][0]["env"] == GB_ENV
    assert any(t["name"] == "gearbase_search_parts" and t.get("defer_loading") for t in w.claude.requests[1]["tools"])
    log = [e["event"] for e in audit.entries(w.home)]
    assert {"tool_draft_started", "tool_draft_ready", "tool_draft_approved"} <= set(log)


def test_discard_bad_input_limits_settings_and_no_claude(world):
    w = world(F.drafting())
    for body, words in [({"need": "ok tool", "docs_url": "http://docs.gearbase-morph.com"}, "https://"),
                        ({"need": "ok tool", "docs_url": "https://192.168.1.10/docs"}, "public website"),
                        ({"need": "x", "docs_url": DOCS}, "3 to 300"), ({"docs_url": DOCS}, "3 to 300")]:
        s, out = w.call("POST", "/api/tools/draft", body)
        assert s == 400 and words in out["error"], body
    s, out = w.call("POST", "/api/tools/draft", {"need": "Gearbase prices", "docs_url": DOCS})
    d = out["draft"]
    assert w.call("POST", "/api/tools/drafts/discard", {"draft": "nope"})[0] == 400
    assert w.call("POST", "/api/tools/drafts/discard", {"draft": "draft_" + "0" * 16})[0] == 404
    s, out = w.call("POST", "/api/tools/drafts/discard", {"draft": d["id"]})
    assert s == 200 and out["draft"]["status"] == "discarded"
    assert w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": d["sha256"]})[0] == 409
    assert w.call("GET", "/api/tools/drafts?status=bogus")[0] == 400
    assert [x["status"] for x in w.call("GET", "/api/tools/drafts?status=discarded")[1]["drafts"]] == ["discarded"]
    settings.save({"agent_tools_enabled": False})
    try:
        s, out = w.call("POST", "/api/tools/draft", {"need": "Gearbase prices", "docs_url": DOCS})
        assert s == 409 and out["error"].endswith("Turn them on to draft a tool.")
    finally:
        settings.save({"agent_tools_enabled": True})
    w.studio.toolbox.client_factory = lambda: None
    s, out = w.call("POST", "/api/tools/draft", {"need": "Gearbase prices", "docs_url": DOCS})
    assert s == 503 and "Drafting a tool needs Claude" in out["error"]


def _form_at(url):
    form = F.gearbase_form()
    form["tools"][0]["url"] = url
    return form


# what the preview promises to refuse, drafted by the fake Claude server over HTTP (the Studio's own SDK client, as in
# the end-to-end tests): (case, documentation pages, the form Claude answers with, the problem code)
SERVED = [("documentation that injects instructions", {DOCS: F.GEARBASE_INJECTED},
           _form_at("https://collect.gearbase-mirror-morph.com/v2/parts/search"), "host_not_named"),
          ("a private IP address", None, _form_at("https://10.20.30.40/v2/parts/search"), "ip_address"),
          ("a host without HTTPS", None, _form_at("http://api.gearbase-morph.com/v2/parts/search"), "not_https"),
          ("a host the documentation never names", None, _form_at("https://api.gearbase-cdn-morph.com/v2/parts/search"),
           "host_not_named"),
          ("a key name outside MORPH_TOOL_", None, F.gearbase_form(key_env="GEARBASE_API_KEY"), "env_name")]


def test_drafts_from_the_fake_claude_server_that_break_the_rules_are_refused(world):
    import anthropic
    w = world()
    for i, (case, pages, form, code) in enumerate(SERVED):
        with F.FakeClaude(F.drafting(pages=pages, form=form)) as fake:
            w.studio.toolbox.client_factory = lambda: anthropic.Anthropic(api_key="test-not-a-real-key", base_url=fake.url,
                                                                           max_retries=0)
            s, out = w.call("POST", "/api/tools/draft", {"need": f"Gearbase prices, case {i}", "docs_url": DOCS})
            d = out["draft"]
            assert s == 202 and d["status"] == "refused" and d["sha256"] is None, (case, d)
            assert code in {p["code"] for p in d["problems"]}, (case, d["problems"])
            assert len(fake.requests) == 1 and fake.requests[0]["tools"][0]["allowed_domains"] == ["docs.gearbase-morph.com"]
            s, out = w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": catalog.fingerprint(d["spec"])})
            assert s == 409, case
    assert not os.path.isdir(catalog.user_dir(w.home)) or os.listdir(catalog.user_dir(w.home)) == []
    assert [e["event"] for e in audit.entries(w.home, event="tool_draft_refused")] == ["tool_draft_refused"] * len(SERVED)


def test_every_draft_route_has_its_minimum_role_in_the_team_table():
    for method, path, role in ROUTES:
        assert roles.required(method, path) == (role, None), (method, path)


def test_only_the_owner_drafts_approves_or_discards_on_a_team_studio(world):
    w = world(F.drafting(), team=True)
    for who in ("Ana", "Vic"):
        s, out = w.call("POST", "/api/tools/draft", {"need": "Gearbase prices", "docs_url": DOCS}, who=who)
        assert s == 403, who
    s, out = w.call("POST", "/api/tools/draft", {"need": "Gearbase prices", "docs_url": DOCS}, who="Jim")
    assert s == 202 and out["draft"]["by"] == w.ids["Jim"]
    d = out["draft"]
    for who in ("Ana", "Vic"):
        s, listed = w.call("GET", "/api/tools/drafts", who=who)
        assert s == 200 and [x["id"] for x in listed["drafts"]] == [d["id"]] and listed["can"]["draft"] is False
        for path, body in (("/api/tools/drafts/approve", {"draft": d["id"], "sha256": d["sha256"]}),
                           ("/api/tools/drafts/discard", {"draft": d["id"]})):
            assert w.call("POST", path, body, who=who)[0] == 403, (who, path)
    assert w.call("GET", "/api/tools/suggest?need=part+prices", who="Vic")[0] == 403
    assert w.call("GET", "/api/tools/suggest?need=part+prices", who="Ana")[0] == 200
    assert w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": d["sha256"]}, who="Jim")[0] == 200
    entries = w.call("GET", "/api/audit", who="Jim")[1]["entries"]
    approved = next(e for e in entries if e["event"] == "tool_draft_approved")
    assert approved["sha256"] == d["sha256"] and approved["by"] == w.ids["Jim"] and approved["hosts"] == ["api.gearbase-morph.com"]
    assert any(e["event"] == "tool_draft_started" and e["docs_host"] == "docs.gearbase-morph.com" for e in entries)


class Resp:
    def __init__(self, path):
        self.data = open(os.path.join(DARWIN, path), "rb").read()

    def read(self, *a):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_darwins_suggestions_for_a_need_come_with_websites_and_never_connect(world, monkeypatch):
    w = world()
    asked = []

    def urlopen(req, timeout=None):  # Darwin's recorded public answers: the search, and one agent's details
        asked.append(req.full_url)
        if req.full_url == darwin.API:
            return Resp("search_needs_input.json")
        if req.full_url == darwin.CAPABILITY.format(cap="cmud0ag7s002n09gmbhttfdpo"):
            return Resp("capability_part_lookup.json")
        raise OSError("not recorded")
    monkeypatch.setattr(darwin, "urlopen", urlopen)
    need = "live distributor stock and price for a manufacturer part number"
    s, out = w.call("GET", "/api/tools/suggest?need=" + need.replace(" ", "+"))
    assert s == 200 and out["need"] == need and out["status"] == "needs_input"
    assert out["question"] == "Do you have a specific manufacturer part number you would like to look up?"
    first, second = out["suggestions"]
    assert first == {"name": "part-lookup", "owner": "Toolcall", "website": "https://toolcall.click/",
                     "darwin_url": "https://index.darwin.so/agent/cmud0999x004b09gml2r8q5zf?capability=cmud0ag7s002n09gmbhttfdpo",
                     "ready": True, "sign_in": False, "paid": True, "summary": first["summary"]}
    assert first["summary"].startswith("Everything about an electronic component") and second["owner"] is None and second["website"] is None and second["ready"] is False
    assert asked[0] == darwin.API and len(asked) == 3  # one search, the details of each agent (one not recorded)
    sent = len(asked)
    s, again = w.call("GET", "/api/tools/suggest?need=" + need.replace(" ", "+"))
    assert again["suggestions"][0]["website"] == "https://toolcall.click/" and len(asked) == sent + 2  # details cached
    settings.save({"network_enabled": False})
    try:
        before = len(asked)
        s, off = w.call("GET", "/api/tools/suggest?need=" + need.replace(" ", "+"))
        assert s == 200 and off["cached"] is True and "off" in off["error"] and len(asked) == before  # nothing sent
        assert off["suggestions"][0]["website"] == "https://toolcall.click/"
    finally:
        settings.save({"network_enabled": True})
    s, out = w.call("GET", "/api/tools/suggest?need=x")
    assert s == 400
    s, out = w.call("GET", "/api/tools/suggest?need=" + "prices+with+sk-ant-api03-" + "z" * 30)
    assert s == 400 and "key" in out["error"]
    assert json.dumps(out).count("sk-ant") == 0
