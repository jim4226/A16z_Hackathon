"""Live checks of Morph's distributor tools (morph/studio/toolbox/catalog/) and of Live prices with real keys: never part
of a normal or CI run, and not run by the engineer who wrote them (no keys were available). MORPH_LIVE=1 with the keys
named below runs the matching test; the keys are read when this file is collected, before the test fixtures clear them.
Each distributor test makes one or two free lookups; the Live prices test also spends a few cents of Claude usage.

They check what the manifests were written from documentation for: the request is accepted, the answer has the fields
that `extract` and `keep` expect, nothing of the key comes back, and a wrong key pauses the run as "rejected" (the
Replace key card), which assumes the service answers 401 or 403 to it. When one passes, set that manifest's
`verified.live` to the date."""

import json
import os

import pytest

_LIVE = os.environ.get("MORPH_LIVE") == "1"
_MOUSER = os.environ.get("MORPH_TOOL_MOUSER_KEY") if _LIVE else None
_E14 = os.environ.get("MORPH_TOOL_ELEMENT14_KEY") if _LIVE else None
_CLAUDE = os.environ.get("ANTHROPIC_API_KEY") if _LIVE else None
pytestmark = [pytest.mark.live, pytest.mark.network]


def _run(name, args, secret):
    from morph.studio.toolbox import catalog, http_tool
    tool = catalog.load().tool(name)
    out = http_tool.run(tool.spec, args, credential=tool.credential.spec(), secret=secret)
    return out, (json.loads(out["content"]) if out["content"] else {})


@pytest.mark.skipif(not _MOUSER, reason="live: set MORPH_LIVE=1 and MORPH_TOOL_MOUSER_KEY")
def test_mouser_keyword_and_part_number_search_with_a_real_key():
    out, doc = _run("mouser_search_keyword", {"keyword": "STS3215", "options": "None"}, _MOUSER)
    assert out["kind"] == "ok" and out["audit"]["status"] == 200, out
    data = doc["data"]
    assert not data.get("errors"), data["errors"]
    assert isinstance(data["total"], int) and data["parts"], data
    part = data["parts"][0]
    assert set(part) <= {"MouserPartNumber", "ManufacturerPartNumber", "Manufacturer", "Description", "Availability",
                         "AvailabilityInStock", "FactoryStock", "LeadTime", "LifecycleStatus", "IsDiscontinued", "Min",
                         "Mult", "PriceBreaks", "ProductDetailUrl", "SuggestedReplacement", "RestrictionMessage"}
    assert part["MouserPartNumber"] and isinstance(part.get("PriceBreaks", []), list)
    out2, doc2 = _run("mouser_search_part_number", {"part_numbers": part["MouserPartNumber"], "match": "Exact"}, _MOUSER)
    assert out2["kind"] == "ok" and doc2["data"]["parts"], out2
    assert _MOUSER not in out["content"] + out2["content"]
    wrong, _ = _run("mouser_search_keyword", {"keyword": "STS3215", "options": "None"}, "0" * 8 + _MOUSER[8:][::-1])
    assert wrong["kind"] == "needs_credential" and wrong["need"]["reason"] == "rejected", wrong


@pytest.mark.skipif(not _E14, reason="live: set MORPH_LIVE=1 and MORPH_TOOL_ELEMENT14_KEY")
def test_element14_keyword_search_with_a_real_key():
    for store in ("www.newark.com", "uk.farnell.com"):
        out, doc = _run("element14_search_keyword", {"term": "any:fuse", "store": store}, _E14)
        assert out["kind"] == "ok" and out["audit"]["status"] == 200, out
        data = doc["data"]
        assert isinstance(data["total"], int) and data["products"], data
        assert set(data["products"][0]) <= {"sku", "displayName", "brandName", "translatedManufacturerPartNumber",
                                            "translatedMinimumOrderQuality", "packSize", "unitOfMeasure", "productStatus",
                                            "rohsStatusCode", "prices", "stock"}
        assert _E14 not in out["content"]
    wrong, _ = _run("element14_search_keyword", {"term": "any:fuse", "store": "www.newark.com"}, "a" * len(_E14))
    assert wrong["kind"] == "needs_credential" and wrong["need"]["reason"] == "rejected", wrong


@pytest.mark.skipif(not (_CLAUDE and _MOUSER), reason="live: set MORPH_LIVE=1, ANTHROPIC_API_KEY and MORPH_TOOL_MOUSER_KEY")
def test_live_prices_with_real_claude_and_a_real_mouser_key(tmp_path, monkeypatch):
    """The demo-like parts list of a fixture project: the price check reads it, looks parts up at Mouser and records
    prices it can match to Mouser's answers."""
    import anthropic

    from morph import keys
    from morph.robot.pipeline2 import RobotProject
    from morph.studio import agents, server
    from test_toolbox_sourcing import make

    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    make(home / "egg-picker", "r001")
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    tb = st.toolbox
    tb.client_factory = lambda: anthropic.Anthropic(api_key=_CLAUDE, timeout=300, max_retries=2)
    tb.spawn = lambda fn: fn()
    keys.save({"MORPH_TOOL_MOUSER_KEY": _MOUSER}, home=str(home))
    tb.on_credentials_saved(["MORPH_TOOL_MOUSER_KEY"])
    view, _ = agents.price_check(st, st.room(), {})
    done = tb.get(view["id"])
    assert done["status"] == "done", done
    lines = (done["result"] or {}).get("lines") or []
    print(json.dumps({"lines": lines, "dropped": (done["result"] or {}).get("dropped"), "calls": done["calls"],
                      "answer": tb.answer(view["id"])}, indent=1))
    assert any(ln["matched"] and ln["service"] == "Mouser Electronics" for ln in lines), lines
    assert _MOUSER not in json.dumps([done, tb.runs()])
