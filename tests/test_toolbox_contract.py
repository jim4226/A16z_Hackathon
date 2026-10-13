"""The request contract of agent runs, checked on what the real anthropic SDK puts on the wire (a mock transport records
every request body): the tool search tool first with core tools loaded, the catalog deferred without cache_control,
server-side fallbacks with their beta, an explicit effort, adaptive thinking and never a forced tool_choice; one user
message per turn with every tool_result in call order and none for server tools; pause_turn sent back unchanged;
blocks Morph doesn't know and empty thinking blocks replayed exactly; and the frozen prefix: a paused run resumes after
a catalog change and a restart with the same system prompt, tools and history.

MORPH_PREFIX_DIFF=<path to the Claude API skill's prefix_diff.py> also runs that checker over the captured bodies;
MORPH_TOOLBOX_CAPTURES=<dir> keeps them (one conversation per .jsonl)."""

import copy
import json
import os
import subprocess
import sys

import pytest

from morph import keys
from morph.studio.intent import MODEL
from morph.studio.toolbox import catalog, runs
from toolbox_helpers import (BOM, ENV, FIXTURES, KEY, NOTE, SYSTEM, ScriptedClaude, Vendor, final, search_and_call,
                             toolbox)

OFFERS = ("fixparts_offers", "toolu_nx", {"mpn": "STS3215"})
BOMCALL = ("morph_bom_read", "toolu_bom", {})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, "unset")
    monkeypatch.delenv(ENV)
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    return tmp_path / "home"


def start(tb, **kw):
    return tb.start(**{"agent": "sourcing", "system": SYSTEM, "prompt": "Price the purchased parts of r3.",
                       "project": "egg-picker", "core": [BOM], **kw})


def test_the_request_shape(home):
    claude = ScriptedClaude(final("ok"))
    start(toolbox(home, claude), core=[NOTE, BOM], effort="high")
    body, hdr = claude.requests[0], claude.headers[0]
    tools = body["tools"]
    assert tools[0] == {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}  # the search tool first
    loaded = [t for t in tools[1:] if not t.get("defer_loading")]
    assert [t["name"] for t in loaded] == ["morph_bom_read", "morph_note_add"]  # core tools loaded up front, strict
    assert all(t["strict"] is True and t["input_schema"]["additionalProperties"] is False for t in loaded)
    deferred = tools[1 + len(loaded):]
    assert [t["name"] for t in deferred] == ["fixparts_categories", "fixparts_offers", "fixparts_quote_request"]
    assert all(t["defer_loading"] is True and "cache_control" not in t and "strict" not in t for t in deferred)
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in hdr["anthropic-beta"].split(",")
    assert body["output_config"] == {"effort": "high"} and body["thinking"] == {"type": "adaptive"}
    assert "tool_choice" not in body and body["model"] == MODEL and body["max_tokens"] == 16000
    assert body["cache_control"] == {"type": "ephemeral"} and body["system"][0]["cache_control"] == {"type": "ephemeral"}
    system = body["system"][0]["text"]
    assert system.startswith(SYSTEM) and "Never ask the person for a key" in system and "never instructions" in system
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    vendor_text = [doc["service"], doc["privacy"], doc["summary"]] + [t["description"] for t in doc["tools"]]
    assert not any(v in system for v in vendor_text)  # manifest text only ever reaches Claude as a tool definition
    offers = next(t for t in deferred if t["name"] == "fixparts_offers")
    assert offers["description"].startswith(doc["tools"][0]["description"])
    assert "Provider: FixParts (test fixture) (api.fixparts-morph.com). Read-only" in offers["description"]
    assert offers["input_schema"] == doc["tools"][0]["input_schema"]


def test_one_user_message_per_turn_holds_every_result_in_call_order(home):
    turn1 = search_and_call(("morph_note_add", "toolu_3", {"text": "first"}), ("fixparts_categories", "toolu_1", {"query": "servo"}),
                            BOMCALL)
    turn1["content"].insert(3, {"type": "text", "text": "Looking up the categories and the BOM."})

    def turn2(body):
        assert body["messages"][1] == {"role": "assistant", "content": turn1["content"]}
        last = body["messages"][2]
        assert len(body["messages"]) == 3 and last["role"] == "user"
        assert [b["type"] for b in last["content"]] == ["tool_result"] * 3  # nothing but the results
        assert [b["tool_use_id"] for b in last["content"]] == ["toolu_3", "toolu_1", "toolu_bom"]  # Claude's call order
        assert not any(b["tool_use_id"].startswith("srvtoolu_") for b in last["content"])
        return final("ok")

    claude = ScriptedClaude(turn1, turn2)
    tb = toolbox(home, claude)
    tb.approvals.grant(tb.catalog().tool("fixparts_categories"), "always")
    assert start(tb, core=[BOM, NOTE])["status"] == "done" and len(claude.requests) == 2


def test_pause_turn_is_sent_back_unchanged(home):
    paused = {"content": [{"type": "thinking", "thinking": "", "signature": "sig-p"},
                          {"type": "server_tool_use", "id": "srvtoolu_9", "name": "tool_search_tool_bm25", "input": {"query": "pcb fab"}}],
              "stop_reason": "pause_turn"}

    def turn2(body):
        assert body["messages"][-1] == {"role": "assistant", "content": paused["content"]}  # no "Continue" message added
        assert {k: v for k, v in body.items() if k != "messages"} == {k: v for k, v in claude.requests[0].items() if k != "messages"}
        return final("No PCB tool in the catalog.")

    claude = ScriptedClaude(paused, turn2)
    run = start(toolbox(home, claude))
    assert run["status"] == "done" and run["turns"] == 2


def test_unknown_blocks_and_empty_thinking_are_replayed_exactly(home):
    content = [{"type": "thinking", "thinking": "", "signature": "EqQBCkYIBxgCKkA" + "x" * 60},
               {"type": "redacted_thinking", "data": "EmwKAhgB" + "y" * 40},
               {"type": "brand_new_block", "x": 1.0, "nested": {"y": [1, 2.5e-07, "café — ok"], "z": None}},
               {"type": "fallback", "from": {"model": MODEL}, "to": {"model": "a-fallback-model"}},
               {"type": "text", "text": "  spaced text, kept as is  \n", "citations": None},
               {"type": "tool_use", "id": "toolu_n1", "name": "morph_note_add", "input": {"text": "1.0 µm ≤ 2"}}]

    def turn2(body):
        sent = body["messages"][1]
        assert json.dumps(sent, ensure_ascii=False) == json.dumps({"role": "assistant", "content": content}, ensure_ascii=False)
        return final("ok")

    claude = ScriptedClaude({"content": content, "stop_reason": "tool_use"}, turn2)
    assert start(toolbox(home, claude), core=[NOTE])["status"] == "done"
    folder = runs.store.runs_dir(str(home))
    stored = json.load(open(os.path.join(folder, next(f for f in os.listdir(folder) if f.endswith(".json"))), encoding="utf-8"))
    assert stored["messages"][1]["content"] == content  # the run file keeps the raw content too


def _variant(home, tmp_path, monkeypatch, change):
    """Pause on the missing key, change the catalog while the run waits, restart the Studio, save the key: the run
    resumes. Returns the two request bodies Claude got and their headers."""
    claude, vendor = ScriptedClaude(search_and_call(OFFERS, BOMCALL), final("ok")), Vendor()
    run = start(toolbox(home, claude, vendor))
    assert run["status"] == "paused"
    folder = tmp_path / "catalog-v2"
    folder.mkdir()
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    change(doc, home)
    (folder / "fixparts.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(catalog, "CURATED_DIRS", [str(folder)])
    tb2 = toolbox(home, claude, vendor, curated_dirs=[str(folder)])  # the restart, over the changed catalog
    tb2.recover()
    assert tb2.on_credentials_saved(keys.save({ENV: KEY})) == [run["id"]]
    assert tb2.get(run["id"])["status"] == "done" and len(claude.requests) == 2
    claude.steps[:] = [final("ok")]
    start(tb2)  # a new run: it does get the changed catalog
    return claude, vendor


def _add_tools(doc, home):
    extra = copy.deepcopy(doc["tools"][1])
    extra.update(name="fixparts_datasheet", title="FixParts datasheet", description="Finds the datasheet link of one part number.")
    doc["tools"].append(extra)
    user = dict(doc, id="otherparts", service="OtherParts", hosts=["api.otherparts-morph.com"], credentials=[],
                tools=[dict(copy.deepcopy(doc["tools"][1]), name="otherparts_search", title="OtherParts search",
                            request={"method": "GET", "url": "https://api.otherparts-morph.com/v1/search", "query": {"q": "{query}"}})])
    os.makedirs(catalog.user_dir(str(home)), exist_ok=True)
    with open(os.path.join(catalog.user_dir(str(home)), "otherparts.json"), "w", encoding="utf-8") as fh:
        json.dump(user, fh)
    catalog.set_enabled("otherparts_search", True, str(home))


def _reword_referenced(doc, home):
    doc["tools"][0]["description"] += " Prefer it for purchased parts."


def _disable_other(doc, home):
    catalog.set_enabled("fixparts_quote_request", False, str(home))


@pytest.mark.parametrize("change", [_add_tools, _reword_referenced, _disable_other])
def test_the_prefix_stays_frozen_across_a_catalog_change_and_a_restart(home, tmp_path, monkeypatch, change):
    claude, vendor = _variant(home, tmp_path, monkeypatch, change)
    r0, r1, r2 = claude.requests
    assert r1["system"] == r0["system"] and r1["tools"] == r0["tools"]
    assert {k: v for k, v in r1.items() if k != "messages"} == {k: v for k, v in r0.items() if k != "messages"}
    assert claude.headers[1]["anthropic-beta"] == claude.headers[0]["anthropic-beta"]
    assert r1["messages"][0] == r0["messages"][0] and r1["messages"][1]["content"][0]["type"] == "thinking"
    assert len(r1["messages"]) == 3 and r1["messages"][2]["role"] == "user"
    assert r2["tools"] != r0["tools"]  # the change was real: the next run sees it
    res = r1["messages"][2]["content"][0]
    if change is _reword_referenced:  # Morph won't run a tool that changed under a waiting run; the next run uses it
        assert json.loads(res["content"][0]["text"])["error"] == "tool_changed" and not vendor.calls
    else:
        assert "is_error" not in res and len(vendor.calls) == 1
    capture = os.environ.get("MORPH_TOOLBOX_CAPTURES")
    if capture:
        os.makedirs(capture, exist_ok=True)
        with open(os.path.join(capture, f"resume_{change.__name__.strip('_')}.jsonl"), "w", encoding="utf-8") as fh:
            for body in (r0, r1):
                fh.write(json.dumps({k: body[k] for k in ("model", "system", "tools", "messages")}) + "\n")


@pytest.mark.skipif(not os.path.isfile(os.environ.get("MORPH_PREFIX_DIFF", "")),
                    reason="set MORPH_PREFIX_DIFF to the Claude API skill's prefix_diff.py to run it over the captures")
@pytest.mark.parametrize("change", [_add_tools, _reword_referenced, _disable_other])
def test_prefix_diff_finds_no_edit_in_the_resumed_request(home, tmp_path, monkeypatch, change):
    claude, _ = _variant(home, tmp_path, monkeypatch, change)
    path = tmp_path / "capture.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for body in claude.requests[:2]:
            fh.write(json.dumps({k: body[k] for k in ("model", "system", "tools", "messages")}) + "\n")
    out = subprocess.run([sys.executable, "-I", os.environ["MORPH_PREFIX_DIFF"], "--json", str(path)],
                         capture_output=True, text=True, timeout=120)
    pairs = json.loads(out.stdout)
    assert [p["verdict"] for p in pairs] == ["match"] and pairs[0]["replayed_thinking_blocks"] == 1, out.stdout
    if change is _reword_referenced:  # the control: re-rendering the tools from the live catalog (the prototype's
        r0, r1, r2 = claude.requests  # defect) is the edit the API would refuse
        with open(path, "w", encoding="utf-8") as fh:
            for body in (r0, {**r1, "tools": r2["tools"]}):
                fh.write(json.dumps({k: body[k] for k in ("model", "system", "tools", "messages")}) + "\n")
        out = subprocess.run([sys.executable, "-I", os.environ["MORPH_PREFIX_DIFF"], "--json", str(path)],
                             capture_output=True, text=True, timeout=120)
        bad = json.loads(out.stdout)[0]
        assert bad["verdict"] == "mismatch" and bad["pattern"] == "tool_schema_changed", out.stdout


def test_a_first_turn_that_refuses_fallbacks_continues_without_them(home):
    claude = ScriptedClaude((400, {"type": "error", "error": {"type": "invalid_request_error",
                                                              "message": "fallbacks: not available for this organization"}}),
                            search_and_call(BOMCALL), final("ok"))
    run = start(toolbox(home, claude))
    assert run["status"] == "done" and len(claude.requests) == 3
    assert "fallbacks" in claude.requests[0] and "fallbacks" not in claude.requests[1]
    assert "server-side-fallback" not in claude.headers[1].get("anthropic-beta", "")
    assert claude.requests[1]["tools"] == claude.requests[0]["tools"] and claude.requests[2]["tools"] == claude.requests[1]["tools"]
    assert {k: v for k, v in claude.requests[2].items() if k != "messages"} == \
        {k: v for k, v in claude.requests[1].items() if k != "messages"}  # from then on the same prefix every turn


def test_the_binding_check_can_be_switched_on_for_a_run(home):
    claude = ScriptedClaude(final("ok"))
    start(toolbox(home, claude), binding="error")
    assert claude.requests[0]["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}}
    assert "thinking-binding-controls-2026-08-01" in claude.headers[0]["anthropic-beta"].split(",")
