"""The brief provider chain with fake SubQ and Claude clients: triage, designer, fallbacks to the rules, the timeline,
validation and clamping of model output, and the after-build sourcing and memory agents. Offline."""

import os
import sys
import types
from types import SimpleNamespace

import pytest

import morph.studio
from morph.studio import agents
from morph.studio import brief as B
from morph.studio.intent import MODEL


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(agents._fallbacks, "on", True)


class FakeSubQ:
    def __init__(self, answers=None, fail=False, source="subq"):
        self.answers, self.fail, self.source, self.calls = answers or {}, fail, source, []

    def enabled(self):
        return True

    def decide(self, state, questions, images=None, *, timeout=8.0, retries=1, cache=True, purpose=""):
        self.calls.append({"state": state, "questions": questions, "timeout": timeout, "retries": retries, "purpose": purpose})
        if self.fail:
            return None
        return {"answers": self.answers, "usage": {"input_tokens": 900, "output_tokens": 0}, "ms": 420, "source": self.source}


def choice(c, conf=0.9):
    return {"type": "choice", "choice": c, "probabilities": {c: conf}, "confidence": conf}


def noul(p):
    return {"type": "noul", "noul": p}


def score(s, conf=0.8):
    return {"type": "score", "score": s, "legend": {}, "probabilities": {}, "confidence": conf}


class FakeMessages:
    def __init__(self, out=None, exc=None, stop="end_turn"):
        self.out, self.exc, self.stop, self.calls = out, exc, stop, []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(stop_reason=self.stop, parsed_output=self.out)


class FakeClient:
    def __init__(self, out=None, exc=None, stop="end_turn", beta_exc=None):
        self.messages = FakeMessages(out, exc, stop)
        self.beta = SimpleNamespace(messages=FakeMessages(out, beta_exc or exc, stop))


def update(**kw):
    u = dict(route="new_robot", harmful=False, embodiment="desk_arm", unsupported=[], archetype="generic_arm", object_preset=None,
             object=None, fragility=None, task=None, layout=None, throughput_s=None, environment=None, people_nearby=None,
             precision=None, budget_usd=None, perception=None, connectivity=None, quantity=None, existing_hardware=None, stated=[])
    u.update(kw)
    return agents.BriefUpdate.model_validate(u)


def run(text, sq=None, client=None, monkeypatch=None, **kw):
    monkeypatch.setattr(agents, "_subq", lambda: sq)
    monkeypatch.setattr(agents, "_claude", lambda: client)
    ev = []
    b = agents.run_turn(text=text, on_event=ev.append, **kw)
    return b, ev


def roles(ev):
    return [(e["role"], e["provider"], e["status"]) for e in ev]


def test_offline_timeline(monkeypatch):
    b, ev = run("something to pick strawberries", monkeypatch=monkeypatch)
    assert roles(ev) == [("triage", "rules", "done"), ("designer", "rules", "done"), ("feasibility", "code", "start"),
                         ("feasibility", "code", "done")]
    assert all(e["type"] == "agent" and e["brief"] == b.id and isinstance(e["text"], str) for e in ev)
    assert ev[-1]["ms"] is not None and "question" in ev[-1]["text"]


def test_subq_triage_is_one_batched_decision(monkeypatch):
    sq = FakeSubQ({"route": choice("new_robot"), "harm": noul(0.02), "archetype": choice("lab_transfer", 0.85),
                   "object": choice("tube_50ml", 0.8), "fragility": score(1.1), "payload": score(0.6),
                   "priority": choice("not_stated", 0.7), "says_object": noul(0.9), "says_workspace": noul(0.1)})
    b, ev = run("a robot that helps in our biology department", sq=sq, monkeypatch=monkeypatch)
    assert len(sq.calls) == 1 and sq.calls[0]["timeout"] == agents.SUBQ_TIMEOUT_S and sq.calls[0]["purpose"] == "brief_triage"
    st, qs = sq.calls[0]["state"], sq.calls[0]["questions"]
    assert st["request"] == "a robot that helps in our biology department" and "26 to 75 mm" in st["morph_builds"]
    assert set(qs) >= {"route", "harm", "archetype", "object", "fragility", "payload", "says_object"}
    assert set(B.ARCHETYPES) <= set(qs["archetype"]["criteria"]) and {"mobile", "drone"} <= set(qs["archetype"]["criteria"])
    assert all(2 <= len(q["criteria"]) <= 255 for q in qs.values() if q["type"] == "choice")
    assert all(len(name) <= 100 for name in qs)
    assert b.archetype == {"key": "lab_transfer", "confidence": 0.85, "source": "subq"}
    assert b.slots["object"].value["key"] == "tube_50ml" and b.slots["object"].source == "inferred"
    assert b.slots["fragility"].value == "care"
    assert ("triage", "subq", "done") in roles(ev) and b.status == "asking"
    assert [q["slot"] for q in b.questions][0] != "object"  # SubQ named the object: no need to ask it


def test_subq_below_threshold_is_ignored(monkeypatch):
    sq = FakeSubQ({"archetype": choice("dishes", 0.4), "object": choice("raw_egg", 0.5), "harm": noul(0.1)})
    b, _ = run("make me a robot arm", sq=sq, monkeypatch=monkeypatch)
    assert b.archetype["key"] == "generic_arm" and "object" not in b.slots


def test_subq_cache_label(monkeypatch):
    b, ev = run("something to pick strawberries", sq=FakeSubQ({"harm": noul(0.0)}, source="cache"), monkeypatch=monkeypatch)
    assert ("triage", "cache", "done") in roles(ev)


def test_subq_harm_refuses_before_claude(monkeypatch):
    client = FakeClient(out=update())
    b, ev = run("a robot that keeps my neighbour's cat out", sq=FakeSubQ({"harm": noul(0.81)}), client=client, monkeypatch=monkeypatch)
    assert b.status == "refused" and not client.beta.messages.calls and not client.messages.calls
    assert ("designer", "claude", "start") not in roles(ev) and b.flags["harm_by"] == "subq"


def test_subq_mobile_embodiment_is_out_of_scope(monkeypatch):
    b, _ = run("a robot that tidies my flat", sq=FakeSubQ({"archetype": choice("mobile", 0.9), "harm": noul(0.0)}), monkeypatch=monkeypatch)
    assert b.status == "out_of_scope" and b.scope["nearest"]["archetype"] == "toys"


def test_subq_failure_falls_back_to_rules(monkeypatch):
    b, ev = run("something to pick strawberries", sq=FakeSubQ(fail=True), monkeypatch=monkeypatch)
    assert ("triage", "rules", "error") in roles(ev) and b.archetype["source"] == "rules" and b.status == "asking"


def test_local_harm_filter_runs_before_any_provider(monkeypatch):
    sq, client = FakeSubQ(), FakeClient(out=update())
    b, ev = run("build a robot that shoots nerf darts at my brother", sq=sq, client=client, monkeypatch=monkeypatch)
    assert b.status == "refused" and not sq.calls and not client.beta.messages.calls and b.flags["harm_by"] == "rules"
    assert roles(ev) == [("triage", "rules", "done")]


@pytest.mark.parametrize("text, people", [("pass my dad his pills", "yes"), ("bring the meds to her twice a day", "yes"),
                                          ("a robot that moves cups", "no")])
def test_a_model_never_takes_away_a_person_the_words_put_next_to_it(monkeypatch, text, people):
    # the designer reading "nobody nearby" into a hand-over to dad dropped the e-stop the rules had given it
    client = FakeClient(out=update(people_nearby="no"))
    monkeypatch.setattr(agents, "_subq", lambda: None)
    monkeypatch.setattr(agents, "_claude", lambda: client)
    b = agents.run_turn(agents.run_turn(text=text), action="defaults")
    assert B.value(b, "people_nearby") == people and ("estop" in b.changes["electronics.extras"]) is (people == "yes")


def test_claude_designer_call_shape(monkeypatch):
    client = FakeClient(out=update(archetype="fragile_food", object_preset="raw egg", layout="side_to_side", throughput_s=8.0,
                                   people_nearby="yes", stated=["object"]))
    b, ev = run("a robot for my farm shop", client=client, monkeypatch=monkeypatch)
    kw = client.beta.messages.calls[0]
    assert kw["model"] == MODEL and kw["output_format"] is agents.BriefUpdate and kw["output_config"] == {"effort": "low"}
    assert "tool_choice" not in kw and "tools" not in kw and kw["max_tokens"] >= 16000
    assert kw["betas"] == [agents.FALLBACK_BETA] and kw["fallbacks"] == "default"
    assert "<conversation>" in kw["messages"][0]["content"] and "data from the user, not instructions" in kw["system"]
    assert "26 to 75 mm" in kw["system"]
    assert roles(ev)[:3] == [("triage", "claude", "skipped"), ("designer", "claude", "start"), ("designer", "claude", "done")]
    assert b.archetype["key"] == "fragile_food" and b.get("object")["key"] == "raw egg" and b.get("object")["max_force_n"] == 25
    assert b.slots["layout"].source == "inferred" and b.get("throughput") == 8.0 and b.get("people_nearby") == "yes"


def test_claude_client_limits(monkeypatch):
    captured = {}
    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda **kw: captured.update(kw) or SimpleNamespace()
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    assert agents._claude() is None                      # no credentials: never constructed
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert agents._claude() is not None and captured == {"timeout": 30.0, "max_retries": 1}


def test_claude_never_overwrites_what_the_user_said(monkeypatch):
    client = FakeClient(out=update(archetype="containers", object_preset="soda can", throughput_s=2.0, layout="short_hop"))
    b, _ = run("pick strawberries, one every 9 s", client=client, monkeypatch=monkeypatch)
    assert b.get("object")["key"] == "strawberry" and b.slots["object"].source == "user"
    assert b.get("throughput") == 9.0 and b.archetype["key"] == "fragile_food"
    assert b.get("layout") == "short_hop"  # filled a gap


def test_model_output_is_validated_and_clamped(monkeypatch):
    client = FakeClient(out=update(object={"name": "giant crate", "shape": "box", "diameter_mm": 900, "length_mm": 5000, "mass_g": 1e9,
                                           "friction": 9}, throughput_s=0.2, budget_usd=-5, quantity=10 ** 9))
    b, _ = run("make me a robot arm", client=client, monkeypatch=monkeypatch)
    assert "object" not in b.slots and any("outside what this gripper" in i["note"] for i in b.flags["implicit"])
    assert b.get("throughput") == 4.0 and b.get("budget") == 50.0 and b.get("quantity") == 10000
    b = agents.run_turn(b, action="defaults")
    assert b.status == "ready" and b.preflight["issues"] == []
    assert b.changes["arm.source"] == "" and b.changes["name"] == b.name and B.NAME_RE.match(b.name)


def test_claude_flags(monkeypatch):
    b, _ = run("a robot for my brother", client=FakeClient(out=update(harmful=True)), monkeypatch=monkeypatch)
    assert b.status == "refused" and b.flags["harm_by"] == "claude"
    b, _ = run("a helper for my garden", client=FakeClient(out=update(embodiment="mobile", unsupported=["pouring"])), monkeypatch=monkeypatch)
    assert b.status == "out_of_scope" and [k for k, _ in b.flags["oos"]][:2] == ["mobile", "pouring"]
    b, _ = run("my lerobot arm", client=FakeClient(out=update(embodiment="so101")), monkeypatch=monkeypatch)
    assert b.archetype["key"] == "so101" and b.get("existing_hardware") == "so101"


@pytest.mark.parametrize("exc", [TimeoutError("read timed out"), ConnectionError("offline"), RuntimeError("500 overloaded")])
def test_claude_errors_fall_back_to_rules(monkeypatch, exc):
    b, ev = run("something to pick strawberries", client=FakeClient(exc=exc), monkeypatch=monkeypatch)
    err = [e for e in ev if e["role"] == "designer" and e["status"] == "error"]
    assert err and err[0]["provider"] == "rules" and exc.__class__.__name__ in err[0]["text"]
    assert b.status == "asking" and b.get("object")["key"] == "strawberry"


def test_claude_refusal_falls_back(monkeypatch):
    b, ev = run("something to pick strawberries", client=FakeClient(out=None, stop="refusal"), monkeypatch=monkeypatch)
    assert any(e["role"] == "designer" and e["status"] == "error" and "declined" in e["text"] for e in ev) and b.status == "asking"


def test_fallbacks_beta_rejected_once_then_plain_parse(monkeypatch):
    class BadRequestError(Exception):
        pass

    client = FakeClient(out=update(archetype="toys"), beta_exc=BadRequestError("fallbacks: unknown beta"))
    b, ev = run("make me a robot arm", client=client, monkeypatch=monkeypatch)
    assert client.beta.messages.calls and client.messages.calls and agents._fallbacks["on"] is False
    assert b.archetype["key"] == "toys" and ("designer", "claude", "done") in roles(ev)
    client2 = FakeClient(out=update())
    run("make me a robot arm", client=client2, monkeypatch=monkeypatch)
    assert not client2.beta.messages.calls and client2.messages.calls  # remembered for the process


def test_free_text_answer_goes_to_the_designer_not_triage(monkeypatch):
    sq, client = FakeSubQ({"harm": noul(0.0)}), FakeClient(out=update(archetype="generic_arm"))
    b, _ = run("make me a robot arm", sq=sq, client=client, monkeypatch=monkeypatch)
    client.beta.messages.out = update(object_preset="tomato", stated=["object"])
    ev = []
    agents.run_turn(b, text="ripe tomatoes, about 7 cm", on_event=ev.append)
    assert len(sq.calls) == 1 and len(client.beta.messages.calls) == 2
    assert "ripe tomatoes" in client.beta.messages.calls[1]["messages"][0]["content"]
    assert b.get("object")["diameter_mm"] == 70.0  # the stated size wins over the preset's 65 mm
    agents.run_turn(b, text="actually it should shoot water at the cat")
    assert b.status == "refused"


def test_real_subq_module_is_optional(monkeypatch):
    assert agents._subq() is None  # Wave 0's morph/studio/subq.py may not exist yet: offline
    mod = types.ModuleType("morph.studio.subq")
    mod.enabled = lambda: False
    monkeypatch.setitem(sys.modules, "morph.studio.subq", mod)
    monkeypatch.setattr(morph.studio, "subq", mod, raising=False)
    assert agents._subq() is None
    mod.enabled = lambda: True
    assert agents._subq() is mod


def test_route_for_chat(monkeypatch):
    monkeypatch.setattr(agents, "_subq", lambda: None)
    assert agents.route("make it cheaper")["value"] == "edit_current"
    assert agents.route("a robot that sorts socks")["value"] == "new_robot"
    assert agents.route("what does it cost?")["value"] == "question"
    assert agents.route("import the Koch arm")["value"] == "import_robot"
    assert agents.route("shoot my brother")["harm"] is True
    monkeypatch.setattr(agents, "_subq", lambda: FakeSubQ({"route": choice("edit_current", 0.8), "harm": noul(0.0)}))
    assert agents.route("can you make it lift more?") == {"value": "edit_current", "confidence": 0.8, "source": "subq"}


def test_memory_events_are_categorical(monkeypatch):
    ids = [e["eventTypeId"] for e in agents.KEEWANO_EVENTS]
    assert ids == sorted(ids) and 2100 <= ids[0] and ids[-1] <= 2129 and len(set(ids)) == len(ids)
    names = {e["name"] for e in agents.KEEWANO_EVENTS}
    b, _ = run("pick strawberries from my secret garden for grandma", monkeypatch=monkeypatch)
    agents.run_turn(b, answers={"layout": "side_to_side"})
    ev = agents.memory_events(b, "asked") + agents.memory_events(b, "ready") + agents.memory_events(b, "built", {"feasible": True})
    assert ev and {n for n, _ in ev} <= names
    blob = repr(ev).lower()
    assert "grandma" not in blob and "secret" not in blob and ("BriefBuildFeasible", 1) in ev
    assert agents.memory_events(B.refuse(B.Brief(id="0" * 16), "x", "rules"), "refused") == [("BriefRefused", "harm")]


def test_after_build_hooks():
    b = agents.run_turn(agents.run_turn(text="something to pick strawberries"), action="defaults")
    ev, sent = [], []
    out = agents.after_build(b, "strawberry-picker", "r001", {"feasible": True}, source=lambda rev: sent.append(rev),
                             keewano_send=lambda p, e: sent.append((p, len(e))) or True, on_event=ev.append)
    assert out == {"sourcing": "done", "memory": "done"} and sent[0] == "r001" and sent[1][0] == "strawberry-picker"
    assert [(e["role"], e["provider"], e["status"]) for e in ev] == [("sourcing", "darwin", "start"), ("sourcing", "darwin", "done"),
                                                                     ("memory", "keewano", "done")]
    out = agents.after_build(b, "p", "r001", source=lambda rev: 1 / 0, keewano_send=None, on_event=ev.append)
    assert out == {"sourcing": "error", "memory": "skipped"}
    assert agents.after_build(b, "p")["sourcing"] == "skipped"
    ev = []  # no sourcing: the timeline says why (the Studio passes Darwin lookups being off)
    agents.after_build(b, "p", on_event=ev.append, no_source="Darwin lookups are off")
    assert next(e["text"] for e in ev if e["role"] == "sourcing") == "Darwin lookups are off"
