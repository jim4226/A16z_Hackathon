"""Live checklist smoke test: never part of a normal or CI run. MORPH_LIVE=1 with ANTHROPIC_API_KEY set runs it (the key
is read when this file is collected, before the test fixtures clear keys); it spends a few cents of Claude usage.

A real Claude run over the fixture catalog (the FixParts API is answered in-process, so no vendor key is needed) should
find the price tool through tool search, call it, pause for the missing key, survive a Studio restart and resume when
the key is saved, with preserved thinking's prefix check set to "error" (beta thinking-binding-controls-2026-08-01): any
change to the system prompt, the tools or an earlier turn on resume would be a 400 and end the run as failed. When the
first request wrote the prompt cache, the resumed one must read it."""

import json
import os

import pytest

_KEY = os.environ.get("ANTHROPIC_API_KEY") if os.environ.get("MORPH_LIVE") == "1" else None
pytestmark = [pytest.mark.live, pytest.mark.network,
              pytest.mark.skipif(not _KEY, reason="live smoke test: set MORPH_LIVE=1 and ANTHROPIC_API_KEY (costs Claude usage)")]


def test_a_real_run_pauses_for_a_key_survives_a_restart_and_resumes(tmp_path, monkeypatch):
    import anthropic

    from morph import keys
    from morph.studio.toolbox import catalog, store
    from toolbox_helpers import BOM, ENV, FIXTURES, KEY, Vendor, toolbox

    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    client = lambda: anthropic.Anthropic(api_key=_KEY, timeout=300, max_retries=2)  # noqa: E731
    home, vendor, events = tmp_path / "home", Vendor(), []
    prompt = ("Find the live price and stock of the Feetech STS3215 servo (manufacturer part number STS3215) with the "
              "distributor tools in the tool catalog, then answer in one sentence with the stock count.")
    tb = toolbox(home, vendor=vendor, events=events, client_factory=client)
    run = tb.start(agent="smoke", system="You are Morph's sourcing agent for robot parts.", prompt=prompt, core=[BOM],
                   effort="low", binding="error")
    assert run["status"] == "paused", run
    assert any(c["type"] == "credential_needed" and c["env"] == ENV for c in run["cards"]), run["cards"]
    tb2 = toolbox(home, vendor=vendor, events=events, client_factory=client)  # the Studio restarts while it waits
    tb2.recover()
    assert tb2.on_credentials_saved(keys.save({ENV: KEY})) == [run["id"]]
    done = tb2.get(run["id"])
    assert done["status"] == "done", done  # a prefix 400 would have ended it as failed
    assert vendor.calls and "412" in (tb2.answer(run["id"]) or "")
    raw = json.load(open(os.path.join(store.runs_dir(str(home)), run["id"] + ".json"), encoding="utf-8"))
    first, resumed = raw["log"][0]["usage"], raw["log"][1]["usage"]
    print(json.dumps({"turns": [t["usage"] for t in raw["log"]], "models": [t["model"] for t in raw["log"]]}))
    if first.get("cache_creation_input_tokens"):
        assert resumed.get("cache_read_input_tokens"), raw["log"]
    blob = json.dumps([raw, events, tb2.runs()])
    assert KEY not in blob and _KEY not in blob
