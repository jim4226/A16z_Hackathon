"""Agent runs (morph/studio/toolbox/runs.py): pausing for a key, an approval or a restart and resuming by themselves,
the person's answers, the caps, and where keys never go. Claude is the real anthropic SDK over a mock transport
(toolbox_helpers.ScriptedClaude); the FixParts fixture API answers in-process; everything runs offline."""

import json
import os
import threading
import time

import pytest

from morph import keys
from morph.studio.toolbox import ToolboxError, audit, catalog, runs, store
from toolbox_helpers import (BOM, ENV, FIXTURES, KEY, KEY2, NOTE, SYSTEM, ScriptedClaude, Vendor, final, private,
                             search_and_call, secrets_absent, toolbox)

OFFERS = ("fixparts_offers", "toolu_nx", {"mpn": "STS3215"})
BOMCALL = ("morph_bom_read", "toolu_bom", {})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "unset")
    monkeypatch.delenv(ENV)  # recorded, so a key keys.save() sets here is gone after the test
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    return tmp_path / "home"


def start(tb, **kw):
    return tb.start(**{"agent": "sourcing", "system": SYSTEM, "prompt": "Price the purchased parts of r3.",
                       "project": "egg-picker", "core": [BOM], **kw})


def last_results(body):
    msg = body["messages"][-1]
    assert msg["role"] == "user" and all(b["type"] == "tool_result" for b in msg["content"])
    return msg["content"]


def text(block):
    return json.loads(block["content"][0]["text"])


# ------------------------------------------------------------------ keys
def test_a_missing_key_pauses_and_saving_it_resumes_the_run_once(home):
    ev = []

    def turn2(body):
        res = last_results(body)
        assert [r["tool_use_id"] for r in res] == ["toolu_nx", "toolu_bom"] and not any(r.get("is_error") for r in res)
        assert text(res[0])["data"]["offers"][0]["stock"] == 412
        return final("STS3215: 412 in stock at FixParts, $13.90.")

    claude, vendor = ScriptedClaude(search_and_call(OFFERS, BOMCALL), turn2), Vendor()
    run = start(toolbox(home, claude, vendor, ev))
    assert run["status"] == "paused" and run["waiting_on"] == ["credential"] and len(claude.requests) == 1
    assert not vendor.calls  # nothing in the batch ran, not even the core tool
    card = run["cards"][0]
    assert card["type"] == "credential_needed" and card["env"] == ENV and card["reason"] == "missing"
    assert card["service"] == "FixParts (test fixture)" and card["url"].startswith("https://") and card["cost"] and card["privacy"]
    assert card["hosts"] == ["api.fixparts-morph.com"] and card["project"] == "egg-picker"
    assert [e["type"] for e in ev] == ["run_started", "credential_needed", "run_paused"]

    ev2 = []
    tb2 = toolbox(home, claude, vendor, ev2)  # the Studio restarted while the run waited
    assert tb2.recover() == {"interrupted": [], "expired": []}
    with pytest.raises(ValueError):
        keys.save({ENV: "fxp_has spaces in it but long enough"})
    changed = keys.save({ENV: KEY})  # POST /api/keys ...
    assert tb2.on_credentials_saved(changed) == [run["id"]]  # ... then the toolbox resumes what waited on it
    assert tb2.on_credentials_saved(keys.save({ENV: KEY})) == []  # a second save: nothing waits any more
    done = tb2.get(run["id"])
    assert done["status"] == "done" and done["cards"] == [] and len(claude.requests) == 2 and len(vendor.calls) == 1
    assert tb2.answer(run["id"]).startswith("STS3215: 412")
    assert [e["type"] for e in ev2] == ["run_resumed", "tool_used", "tool_used", "run_done"]
    assert {e["tool"] for e in ev2 if e["type"] == "tool_used"} == {"fixparts_offers", "morph_bom_read"}
    appr = tb2.approvals.get("fixparts_offers")
    assert appr["mode"] == "key_saved"  # saving a curated read-only tool's key approved its first use
    assert private(keys.ENV_PATH)


def test_two_saves_at_once_resume_the_run_exactly_once(home, monkeypatch):
    gate, entered = threading.Event(), threading.Event()

    def slow(body):
        entered.set()
        gate.wait(10)
        return final("ok")

    claude, vendor = ScriptedClaude(search_and_call(OFFERS), slow, final("never sent")), Vendor()
    tb = toolbox(home, claude, vendor)
    run = start(tb)
    monkeypatch.setenv(ENV, KEY)
    bg = toolbox(home, claude, vendor, spawn=lambda fn: threading.Thread(target=fn, daemon=True).start())
    assert bg.on_credentials_saved([ENV]) == [run["id"]]
    assert entered.wait(10)  # the first resume is talking to Claude right now
    assert bg.on_credentials_saved([ENV]) == []  # a second Save click, in this Studio ...
    assert toolbox(home, claude, vendor).on_credentials_saved([ENV]) == []  # ... or another process: the run is claimed
    with pytest.raises(ToolboxError) as e:
        tb.decide(run["id"], {"toolu_nx": "skip"})
    assert e.value.status == 409
    gate.set()
    assert bg.wait(run["id"], 10)["status"] == "done"
    assert len(claude.requests) == 2 and len(vendor.calls) == 1


def test_skip_answers_claude_with_a_needs_credential_result(home):
    seen = {}

    def turn2(body):
        seen["res"] = last_results(body)
        return final("I couldn't get live prices: the FixParts key wasn't added.")

    claude = ScriptedClaude(search_and_call(OFFERS, BOMCALL), turn2)
    tb = toolbox(home, claude)
    run = start(tb)
    out = tb.decide(run["id"], {"toolu_nx": "skip"}, role="editor")  # an editor may skip
    assert out["status"] == "done"
    nx, bom = seen["res"]
    assert nx["is_error"] is True and "is_error" not in bom and "STS3215" in bom["content"][0]["text"]
    body = text(nx)
    assert body["error"] == "needs_credential" and body["status"] == "skipped_by_user" and body["service"].startswith("FixParts")
    assert "Keys dialog" in body["message"] and "Do not ask the person to paste a key" in body["message"]
    assert "Carry on without this tool" in body["message"]


def test_a_vendor_401_pauses_as_rejected_and_a_new_key_runs_the_call_again(home, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    vendor = Vendor()
    vendor.reject = True

    def turn2(body):
        assert text(last_results(body)[0])["data"]["offers"][0]["stock"] == 412
        return final("ok")

    claude = ScriptedClaude(search_and_call(OFFERS), turn2)
    tb = toolbox(home, claude, vendor)
    tb.approvals.grant(tb.catalog().tool("fixparts_offers"), "always")
    run = start(tb)
    card = run["cards"][0]
    assert run["status"] == "paused" and card["type"] == "credential_needed" and card["reason"] == "rejected"
    assert len(claude.requests) == 1 and len(vendor.calls) == 1  # Claude was told nothing yet
    vendor.reject = False
    monkeypatch.setenv(ENV, KEY2)
    assert tb.on_credentials_saved([ENV]) == [run["id"]]
    assert tb.get(run["id"])["status"] == "done" and len(vendor.calls) == 2
    assert vendor.calls[1][2]["Authorization"] == f"Bearer {KEY2}"


# ------------------------------------------------------------------ approvals
def test_a_keyless_tool_asks_before_its_first_use_and_always_is_remembered_until_it_changes(home, tmp_path, monkeypatch):
    cats = ("fixparts_categories", "toolu_c1", {"query": "servo"})
    claude = ScriptedClaude(search_and_call(cats), final("Servo motors."), search_and_call(cats), final("again"))
    tb = toolbox(home, claude)
    run = start(tb)
    card = run["cards"][0]
    assert card["type"] == "tool_request" and card["reason"] == "first_use" and card["side_effect"] == "read"
    assert card["host"] == "api.fixparts-morph.com" and card["sends"] == "the search words" and '"servo"' in card["input"]
    with pytest.raises(ToolboxError) as e:
        tb.decide(run["id"], {"toolu_c1": "allow_always"}, role="editor")
    assert e.value.status == 403
    assert tb.decide(run["id"], {"toolu_c1": "allow_always"}, by="m_owner")["status"] == "done"
    assert start(tb)["status"] == "done"  # remembered: the next run doesn't ask
    # the manifest changes (an update reworded the tool): its approval no longer counts
    changed = tmp_path / "changed"
    changed.mkdir()
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    doc["tools"][1]["sends"] = "the search words and your account id"
    (changed / "fixparts.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(catalog, "CURATED_DIRS", [str(changed)])
    claude.steps[:] = [search_and_call(cats)]
    run3 = start(toolbox(home, claude, curated_dirs=[str(changed)]))
    assert run3["cards"][0]["reason"] == "changed"


def test_write_tools_ask_before_every_call(home, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    quote = ("fixparts_quote_request", "toolu_q1", {"mpn": "STS3215", "quantity": 6})
    quote2 = ("fixparts_quote_request", "toolu_q2", {"mpn": "STS3215", "quantity": 12})

    def turn2(body):
        assert text(last_results(body)[0])["data"]["quote"]["qty"] == 6
        return search_and_call(quote2)

    claude, vendor = ScriptedClaude(search_and_call(quote), turn2, final("Two quotes requested.")), Vendor()
    tb = toolbox(home, claude, vendor)
    run = start(tb)
    assert run["cards"][0]["reason"] == "per_call" and run["cards"][0]["side_effect"] == "write"
    run = tb.decide(run["id"], {"toolu_q1": "allow_always"})  # "Always" counts once for a tool that writes
    assert run["status"] == "paused" and run["cards"][0]["tool_use_id"] == "toolu_q2" and run["cards"][0]["reason"] == "per_call"
    assert tb.approvals.get("fixparts_quote_request") is None
    assert tb.decide(run["id"], {"toolu_q2": "allow_once"})["status"] == "done"
    assert [c[0] for c in vendor.calls] == ["POST", "POST"] and json.loads(vendor.calls[1][3]) == {"part": "STS3215", "qty": 12}


def test_deny_and_unknown_decisions(home):
    claude = ScriptedClaude(search_and_call(("fixparts_categories", "toolu_c1", {"query": "x"})), final("ok"))
    tb = toolbox(home, claude)
    run = start(tb)
    with pytest.raises(ToolboxError):
        tb.decide(run["id"], {"toolu_c1": "maybe"})
    with pytest.raises(ToolboxError) as e:
        tb.decide(run["id"], {"toolu_other": "skip"})
    assert e.value.status == 404
    tb.decide(run["id"], {"toolu_c1": "deny"})
    body = text(last_results(claude.requests[-1])[0])
    assert body["status"] == "denied_by_user" and "Don't call it again" in body["message"]
    assert tb.decide(run["id"], {"toolu_c1": "skip"})["status"] == "done"  # a late click changes nothing


# ------------------------------------------------------------------ refusals by policy
def _write_manifest(folder, doc):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{doc['id']}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_paid_tools_are_refused_with_an_honest_result(home, tmp_path, monkeypatch):
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    paid = json.loads(json.dumps(doc["tools"][1]))
    paid.update(name="fixparts_live_quote", title="FixParts live quote", side_effect="spend", cost={"usd_per_call": 0.04})
    doc["tools"].append(paid)
    _write_manifest(tmp_path / "cat", doc)
    monkeypatch.setattr(catalog, "CURATED_DIRS", [str(tmp_path / "cat")])
    claude, vendor = ScriptedClaude(search_and_call(("fixparts_live_quote", "toolu_p", {"query": "x"})), final("ok")), Vendor()
    tb = toolbox(home, claude, vendor, curated_dirs=[str(tmp_path / "cat")])
    assert "paid tools aren't supported in this beta" in tb.catalog().tool("fixparts_live_quote").claude_description()
    assert start(tb)["status"] == "done" and not vendor.calls
    body = text(last_results(claude.requests[-1])[0])
    assert body["error"] == "paid_tool_not_supported" and "$0.04" in body["message"]
    assert any(t["paid"] for t in tb.tools()["tools"])


def test_per_tool_call_limit_turn_cap_refusal_and_max_tokens(home, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    calls = [("fixparts_offers", f"toolu_{i}", {"mpn": "STS3215"}) for i in range(3)]
    claude = ScriptedClaude(search_and_call(*calls), final("ok"))
    tb = toolbox(home, claude, max_calls=2)
    tb.approvals.grant(tb.catalog().tool("fixparts_offers"), "always")
    start(tb)
    res = last_results(claude.requests[-1])
    assert [r.get("is_error", False) for r in res] == [False, False, True] and text(res[2])["error"] == "call_limit"
    loop = ScriptedClaude(*[search_and_call(("morph_bom_read", f"toolu_b{i}", {})) for i in range(5)])
    run = start(toolbox(home, loop, max_turns=3))
    assert run["status"] == "failed" and "3 turns" in run["note"] and len(loop.requests) == 3
    refusal = ScriptedClaude({"content": [], "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber"}})
    run = start(toolbox(home, refusal))
    assert run["status"] == "refused" and run["note"] == "Claude declined this request (safety category: cyber)."
    cut = ScriptedClaude({"content": [{"type": "tool_use", "id": "toolu_x", "name": "morph_bom_read", "input": {}}],
                          "stop_reason": "max_tokens"})
    run = start(toolbox(home, cut))
    assert run["status"] == "failed" and "cut off" in run["note"]


def test_tools_turned_off_changed_or_unknown_while_a_run_waits(home, tmp_path, monkeypatch):
    calls = [("fixparts_offers", "toolu_a", {"mpn": "STS3215"}), ("fixparts_categories", "toolu_b", {"query": "x"}),
             ("fixparts_nothing", "toolu_c", {})]
    claude = ScriptedClaude(search_and_call(*calls), final("ok"))
    tb = toolbox(home, claude)
    run = start(tb)
    assert run["status"] == "paused"
    tb.set_enabled("fixparts_categories", False, by="m_owner")  # the owner turned one off while the run waited
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    doc["tools"][0]["description"] += " Prefer it for purchased parts."  # and an update reworded another
    _write_manifest(tmp_path / "new", doc)
    monkeypatch.setattr(catalog, "CURATED_DIRS", [str(tmp_path / "new")])
    tb2 = toolbox(home, claude, curated_dirs=[str(tmp_path / "new")])
    monkeypatch.setenv(ENV, KEY)
    assert tb2.on_credentials_saved([ENV]) == [run["id"]]
    codes = [text(r)["error"] for r in last_results(claude.requests[-1])]
    assert codes == ["tool_changed", "tool_unavailable", "unknown_tool"]
    entries = [e for e in audit.entries(str(home)) if e["event"] in ("tool_disabled", "tool_refused")]
    assert {e.get("reason") for e in entries} >= {"tool_changed", "tool_unavailable", "unknown_tool"}


def test_bad_arguments_and_offline_get_a_result_not_a_card(home):
    claude = ScriptedClaude(search_and_call(("fixparts_categories", "toolu_a", {"query": "x" * 300})), final("ok"),
                            search_and_call(("fixparts_categories", "toolu_b", {"query": "servo"})), final("ok"))
    tb = toolbox(home, claude)
    assert start(tb)["status"] == "done" and text(last_results(claude.requests[1])[0])["error"] == "invalid_arguments"
    # network lookups off: nothing goes out, not even to Claude; the run waits until they are on again (B2-T4: it used
    # to send the call an "offline" result and keep talking to Claude)
    off = start(toolbox(home, claude, network=lambda: False))
    assert off["status"] == "paused" and off["cards"][0]["reason"] == "offline" and len(claude.requests) == 2
    from morph.studio.toolbox import policy
    tool = toolbox(home, claude).catalog().tool("fixparts_categories")
    chk = policy.check({"calls": {}, "pending": {}}, {"id": "toolu_b", "name": tool.name, "input": {"query": "servo"}}, tool=tool,
                       frozen={"fingerprint": tool.fingerprint}, enabled=True, approvals=toolbox(home, claude).approvals, env={},
                       network=False)
    assert chk.action == "result" and chk.code == "offline"  # the policy still answers a call that slipped past


# ------------------------------------------------------------------ restarts
class Crash(BaseException):
    """The Studio process dying in the middle of something."""


def test_a_restart_turns_a_running_run_into_an_interrupted_pause(home):
    def die(body):
        raise Crash()

    claude = ScriptedClaude(search_and_call(BOMCALL), die)
    tb = toolbox(home, claude)
    with pytest.raises(Crash):
        start(tb)
    rid = tb.runs()[0]["id"]
    assert tb.runs()[0]["status"] == "running"
    ev = []
    tb2 = toolbox(home, claude, events=ev)
    assert tb2.recover() == {"interrupted": [rid], "expired": []}
    view = tb2.get(rid)
    assert view["status"] == "paused" and view["waiting_on"] == ["interrupted"] and view["cards"][0]["type"] == "run_interrupted"
    assert ev[-1]["type"] == "run_paused" and ev[-1]["reason"] == "restart"
    claude.steps[:] = [final("ok")]
    assert tb2.resume(rid)["status"] == "done"
    assert claude.requests[-1]["messages"] == claude.requests[-2]["messages"]  # the same request, nothing lost or added


def test_an_interrupted_run_never_repeats_a_side_effect(home, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    quote = ("fixparts_quote_request", "toolu_q", {"mpn": "STS3215", "quantity": 6})
    claude, vendor = ScriptedClaude(search_and_call(quote, ("fixparts_categories", "toolu_c", {"query": "servo"}))), Vendor()
    tb = toolbox(home, claude, vendor)
    tb.approvals.grant(tb.catalog().tool("fixparts_categories"), "always")
    run = start(tb)
    vendor.crash = Crash()
    with pytest.raises(Crash):
        tb.decide(run["id"], {"toolu_q": "allow_once"})  # the Studio dies while the quote request is out
    assert len(vendor.calls) == 1
    raw = json.load(open(os.path.join(store.runs_dir(str(home)), run["id"] + ".json"), encoding="utf-8"))
    assert raw["status"] == "running" and raw["pending"]["started"] == ["toolu_q"]  # the intent was on disk first
    vendor.crash = None
    claude.steps[:] = [final("I asked for a quote; Morph says the request may or may not have gone through.")]
    tb2 = toolbox(home, claude, vendor)
    assert tb2.recover()["interrupted"] == [run["id"]]
    assert tb2.resume(run["id"])["status"] == "done"
    assert [c[0] for c in vendor.calls] == ["POST", "GET"]  # the quote was never sent twice; the read ran
    q, c = last_results(claude.requests[-1])
    assert text(q)["error"] == "interrupted" and "did not repeat it" in text(q)["message"] and "is_error" not in c


def test_an_interrupted_read_runs_again(home, monkeypatch):
    monkeypatch.setenv(ENV, KEY)
    claude, vendor = ScriptedClaude(search_and_call(OFFERS)), Vendor()
    tb = toolbox(home, claude, vendor)
    tb.approvals.grant(tb.catalog().tool("fixparts_offers"), "always")
    vendor.crash = Crash()
    with pytest.raises(Crash):
        start(tb)
    vendor.crash = None
    claude.steps[:] = [final("ok")]
    tb2 = toolbox(home, claude, vendor)
    rid = tb2.recover()["interrupted"][0]
    assert tb2.resume(rid)["status"] == "done" and len(vendor.calls) == 2
    assert "is_error" not in last_results(claude.requests[-1])[0]


def test_claude_unreachable_or_refusing_the_key_interrupts_and_resumes(home):
    claude = ScriptedClaude((529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    tb = toolbox(home, claude)
    run = start(tb)
    assert run["status"] == "paused" and run["cards"][0]["reason"] == "claude_unavailable"
    claude.steps[:] = [(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})]
    run = tb.resume(run["id"])
    assert run["status"] == "paused" and run["cards"][0]["reason"] == "claude_key"
    claude.steps[:] = [(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "messages.0: bad"}})]
    run = tb.resume(run["id"])
    assert run["status"] == "failed" and "messages.0: bad" in run["note"]
    assert len(claude.requests) == 3 and claude.requests[0] == claude.requests[2]


# ------------------------------------------------------------------ the run files
def test_run_files_are_private_ids_are_checked_and_runs_expire(home):
    claude = ScriptedClaude(final("ok"))
    tb = toolbox(home, claude)
    run = start(tb)
    p = os.path.join(store.runs_dir(str(home)), run["id"] + ".json")
    assert private(p) and private(store.runs_dir(str(home)), 0o700)
    for bad in ("../x", "run_../../etc", "run_123", run["id"] + "/..", "RUN_0123456789abcdef"):
        with pytest.raises(ToolboxError) as e:
            tb.get(bad)
        assert e.value.status == 404
    with pytest.raises(ToolboxError):
        tb.decide("../../secret", {"x": "skip"})
    raw = json.load(open(p, encoding="utf-8"))
    raw["updated"] = time.time() - 8 * 86400
    store.write_json(p, raw)
    assert tb.runs() == []  # past 7 days: hidden at once, deleted at the next start
    assert tb.recover()["expired"] == [run["id"]] and not os.path.exists(p)
    assert audit.entries(str(home), event="run_expired")[0]["run"] == run["id"]


def test_cancel_a_paused_run_and_a_running_one(home):
    claude = ScriptedClaude(search_and_call(OFFERS))
    tb = toolbox(home, claude)
    run = start(tb)
    assert tb.cancel(run["id"], by="m_owner")["status"] == "cancelled"
    assert tb.cancel(run["id"])["status"] == "cancelled"
    gate, entered = threading.Event(), threading.Event()

    def slow(body):
        entered.set()
        gate.wait(10)
        return search_and_call(BOMCALL)

    claude2 = ScriptedClaude(slow, final("never"))
    bg = toolbox(home, claude2, spawn=lambda fn: threading.Thread(target=fn, daemon=True).start())
    run2 = start(bg)
    assert entered.wait(10)
    assert bg.cancel(run2["id"])["cancelling"] is True
    gate.set()
    assert bg.wait(run2["id"], 10)["status"] == "cancelled" and len(claude2.requests) == 1


def test_start_checks_its_arguments_and_needs_claude(home):
    tb = toolbox(home, ScriptedClaude(final("ok")))
    for bad in ({"agent": "Sourcing!"}, {"project": "../evil"}, {"effort": "extreme"}, {"max_tokens": 64000},
                {"binding": "loose"}, {"prompt": ""}, {"core": ["morph_unknown"]}):
        with pytest.raises(ToolboxError):
            start(tb, **bad)
    with pytest.raises(ToolboxError) as e:
        start(toolbox(home))  # no Claude: no key, or network lookups off
    assert e.value.status == 503
    with pytest.raises(ValueError):
        runs.check_builtin(runs.Builtin("morph_x_tool", "Does something useful for the agent here.",
                                        {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}},
                                         "required": ["n"], "additionalProperties": False}, lambda i, c: {}))
    with pytest.raises(ValueError):
        runs.check_builtin(runs.Builtin("morph_x_tool", "Does something useful for the agent here.",
                                        {"type": "object", "properties": {"n": {"type": "integer"}}, "required": [],
                                         "additionalProperties": False}, lambda i, c: {}))
    assert runs.check_builtin(NOTE) is NOTE


def test_a_core_tool_that_breaks_is_an_error_result(home):
    boom = runs.Builtin("morph_boom", "A core tool that fails, to check the run carries on.",
                        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                        lambda i, c: 1 / 0)
    claude = ScriptedClaude(search_and_call(("morph_boom", "toolu_b", {})), final("ok"))
    run = start(toolbox(home, claude, builtins={"morph_boom": boom}), core=[boom])
    assert run["status"] == "done"
    assert text(last_results(claude.requests[-1])[0]) == {"error": "tool_failed", "detail": "ZeroDivisionError"}


# ------------------------------------------------------------------ where keys never go
def test_keys_never_reach_claude_events_the_audit_log_run_files_or_answers(home):
    ev = []
    claude, vendor = ScriptedClaude(search_and_call(OFFERS, BOMCALL), final("ok")), Vendor()
    tb = toolbox(home, claude, vendor, ev)
    run = start(tb)
    keys.save({ENV: KEY})
    tb.on_credentials_saved([ENV], by="m_owner")
    assert tb.get(run["id"])["status"] == "done"
    assert vendor.calls[0][2]["Authorization"] == f"Bearer {KEY}"  # the key went where it belongs ...
    assert vendor.ips == ["93.184.216.34"]                          # ... to the address Morph checked
    assert "[redacted]" in json.dumps(claude.requests[-1])          # FixParts echoed it back: Claude got [redacted]
    assert secrets_absent(claude.requests, KEY) and secrets_absent(claude.headers, KEY)
    assert secrets_absent(ev, KEY)
    assert secrets_absent(open(audit.path(str(home)), encoding="utf-8").read(), KEY)
    for fn in os.listdir(store.runs_dir(str(home))):
        assert secrets_absent(open(os.path.join(store.runs_dir(str(home)), fn), encoding="utf-8", errors="replace").read(), KEY)
    assert secrets_absent([tb.runs(), tb.tools(), tb.get(run["id"]), keys.status()], KEY)
    assert private(keys.ENV_PATH)


def test_views_carry_no_message_text(home):
    claude = ScriptedClaude(search_and_call(("fixparts_categories", "toolu_c", {"query": "servo"})), final("The secret plan: X."))
    tb = toolbox(home, claude)
    run = start(tb, prompt="Please price my confidential gripper redesign.")
    view = json.dumps(tb.runs())
    assert "confidential gripper" not in view and "Servo motors" not in view
    tb.decide(run["id"], {"toolu_c": "allow_once"})
    view = json.dumps([tb.runs(), tb.get(run["id"])])
    assert "secret plan" not in view and "confidential" not in view and tb.answer(run["id"]) == "The secret plan: X."


def test_the_audit_log_is_hash_chained(home):
    claude = ScriptedClaude(search_and_call(("fixparts_categories", "toolu_c", {"query": "servo"})), final("ok"))
    tb = toolbox(home, claude)
    run = start(tb)
    tb.decide(run["id"], {"toolu_c": "allow_always"}, by="m_owner")
    tb.set_enabled("fixparts_quote_request", False, by="m_owner")
    ok = audit.verify(str(home))
    assert ok["ok"] and ok["lines"] >= 7
    events = [e["event"] for e in reversed(audit.entries(str(home)))]
    assert events[:4] == ["run_started", "run_paused", "tool_approved", "run_resumed"] and "tool_call" in events
    call = audit.entries(str(home), event="tool_call")[0]
    assert {"tool", "host", "ok", "ms", "cost_usd"} <= set(call) and "input" not in call
    p = audit.path(str(home))
    lines = open(p, encoding="utf-8").read().splitlines()
    lines[1] = lines[1].replace('"run_paused"', '"run_xpaused"')
    open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    bad = audit.verify(str(home))
    assert not bad["ok"] and bad["line"] == 3


def test_a_run_is_claimed_across_processes_and_freed_when_its_process_dies(tmp_path):
    import subprocess
    import sys
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = str(tmp_path / "run_0123456789abcdef.lock")
    probe = f"from morph.studio.toolbox import store; print(store.Claim({path!r}).acquire())"
    hold = f"import sys, time; from morph.studio.toolbox import store; c = store.Claim({path!r}); print(c.acquire(), flush=True); time.sleep(60)"

    def other():
        return subprocess.run([sys.executable, "-c", probe], cwd=repo, capture_output=True, text=True, timeout=60).stdout.strip()

    mine = store.Claim(path)
    assert mine.acquire() and not store.Claim(path).acquire()  # another thread of this process can't take it ...
    assert other() == "False"                                   # ... nor another process
    mine.release()
    assert other() == "True"
    holder = subprocess.Popen([sys.executable, "-c", hold], cwd=repo, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "True"
        assert not store.Claim(path).acquire()
        holder.kill()  # the process dies while it drives the run: the system frees the claim
        holder.wait(30)
        again = store.Claim(path)
        assert again.acquire()
        again.release()
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.stdout.close()


def test_a_resume_while_claude_is_unavailable_runs_no_tool(home, monkeypatch):
    claude, vendor = ScriptedClaude(search_and_call(OFFERS), final("ok")), Vendor()
    run = start(toolbox(home, claude, vendor))
    monkeypatch.setenv(ENV, KEY)
    offline = toolbox(home, claude, vendor, client_factory=lambda: None)  # say the Anthropic key was removed meanwhile
    assert offline.on_credentials_saved([ENV]) == [run["id"]]
    view = offline.get(run["id"])
    assert view["waiting_on"] == ["interrupted"] and view["cards"][0]["reason"] == "claude_unavailable" and not vendor.calls
    assert toolbox(home, claude, vendor).resume(run["id"])["status"] == "done" and len(vendor.calls) == 1


def test_saving_the_key_of_the_owners_own_tool_does_not_approve_it(home):
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    os.makedirs(catalog.user_dir(str(home)), exist_ok=True)
    with open(os.path.join(catalog.user_dir(str(home)), "fixparts.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)  # the same manifest, but the owner's own: not curated
    claude = ScriptedClaude(search_and_call(OFFERS), final("ok"))
    tb = toolbox(home, claude, curated_dirs=[])
    tb.set_enabled("fixparts_offers", True, by="m_owner")
    run = start(tb)
    assert [c["type"] for c in run["cards"]] == ["tool_request", "credential_needed"]  # both: first use and the key
    assert tb.on_credentials_saved(keys.save({ENV: KEY})) == [run["id"]]
    run = tb.get(run["id"])
    assert run["status"] == "paused" and [c["type"] for c in run["cards"]] == ["tool_request"]
    assert tb.approvals.get("fixparts_offers") is None
    assert tb.decide(run["id"], {"toolu_nx": "allow_once"})["status"] == "done"


def test_a_key_saved_while_the_run_reaches_its_pause_still_resumes_it(home, monkeypatch):
    """The save lands between the run's check (no key) and its pause: the save can't claim the run, so the run itself
    notices the key once it lets go. A key the service refused never loops this way."""
    claude, vendor = ScriptedClaude(search_and_call(OFFERS), final("ok")), Vendor()
    tb = toolbox(home, claude, vendor)
    real_pause = tb._pause

    def pause_then_save(run, blocked):
        out = real_pause(run, blocked)
        monkeypatch.setenv(ENV, KEY)
        assert tb.on_credentials_saved([ENV]) == []  # the run is still claimed by the thread pausing it
        return out

    tb._pause = pause_then_save
    run = start(tb)
    tb._pause = real_pause
    assert tb.get(run["id"])["status"] == "done" and len(vendor.calls) == 1 and len(claude.requests) == 2
    vendor.reject = True
    claude.steps[:] = [search_and_call(OFFERS)]
    run2 = start(tb)  # the key is set but refused: the run waits for a new key, without calling FixParts again
    assert run2["status"] == "paused" and run2["cards"][0]["reason"] == "rejected" and len(vendor.calls) == 2


def test_listing_runs_reads_only_the_files_that_changed(home):
    claude = ScriptedClaude(*[step for _ in range(3) for step in (search_and_call(("fixparts_categories", "toolu_c", {"query": "x"})),)],
                            final("ok"))
    tb = toolbox(home, claude)
    ids = [start(tb)["id"] for _ in range(3)]
    loads = []
    real = tb._load
    tb._load = lambda rid: loads.append(rid) or real(rid)
    assert [r["id"] for r in tb.runs()] == ids[::-1] and len(loads) == 3
    assert len(tb.runs()) == 3 and len(loads) == 3  # nothing changed: nothing read again
    tb._load = real
    tb.decide(ids[0], {"toolu_c": "allow_once"})
    tb._load = lambda rid: loads.append(rid) or real(rid)
    assert {r["id"]: r["status"] for r in tb.runs()}[ids[0]] == "done" and loads[3:] == [ids[0]]
    assert tb.on_credentials_saved([ENV]) == [] and loads[4:] == []  # no run waits for that key: none was read


def test_keys_changing_in_another_thread_never_break_a_run(home):
    stop = threading.Event()

    def churn():
        while not stop.is_set():
            os.environ["MORPH_TOOL_CHURN_KEY"] = "churn-value-0123456789"
            os.environ.pop("MORPH_TOOL_CHURN_KEY", None)

    tb = toolbox(home, ScriptedClaude())
    t = threading.Thread(target=churn, daemon=True)
    t.start()
    n, t0 = 0, time.monotonic()
    try:  # reading os.environ.items() here raised KeyError about 15 times a second (a key removed mid-read)
        while time.monotonic() - t0 < 1.0:
            store.env_items(os.environ)
            tb._known_secrets()
            n += 1
        assert not catalog.looks_like_tool_key("nothing secret here")
    finally:
        stop.set()
        t.join(5)
        os.environ.pop("MORPH_TOOL_CHURN_KEY", None)
    assert n > 100


def test_a_full_disk_at_a_restart_leaves_what_it_cant_write_for_the_next_start(home, monkeypatch):
    """A Studio starting on a full disk: the first run whose pause couldn't be written stopped the recovery of every
    run after it and of the drafts. Each run and the drafts are tried now; what the disk refused stays as it was for
    the next start, and recover() then raises the disk-full error (api_tools.attach says it in one line)."""
    import errno
    from morph.studio.toolbox import draft as draft_mod

    def die(body):
        raise Crash()

    claude = ScriptedClaude(search_and_call(BOMCALL), die, search_and_call(BOMCALL), die)
    tb = toolbox(home, claude)
    for _ in range(2):
        with pytest.raises(Crash):
            start(tb)
    first, second = sorted(r["id"] for r in tb.runs())
    full = OSError(errno.ENOSPC, "No space left on device")
    real_save, real_locked, real_ids, drafts_tried = runs.Toolbox._save, draft_mod.store.locked, runs.Toolbox._ids, []
    drafts = os.path.join(draft_mod.store.tools_dir(str(home)), "drafts")

    def save(self, run):
        if run["id"] == first:
            raise full
        real_save(self, run)

    def locked(folder):  # only the drafts' folder is refused (the audit log's lock still works)
        if os.path.realpath(folder) == os.path.realpath(drafts):
            drafts_tried.append(folder)
            raise full
        return real_locked(folder)

    monkeypatch.setattr(runs.Toolbox, "_ids", lambda self, newest_first=False: [first, second])  # the refused one first
    monkeypatch.setattr(runs.Toolbox, "_save", save)
    monkeypatch.setattr(draft_mod.store, "locked", locked)
    assert sorted(real_ids(tb)) == [first, second]
    tb2 = toolbox(home, claude)
    with pytest.raises(OSError) as raised:
        tb2.recover()
    assert raised.value is full and drafts_tried                            # the drafts were tried too
    assert {r["id"]: r["status"] for r in tb2.runs()} == {first: "running", second: "paused"}
    monkeypatch.undo()
    assert toolbox(home, claude).recover()["interrupted"] == [first]       # space again: the next start does it
