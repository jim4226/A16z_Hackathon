"""The tool catalog (morph/studio/toolbox/catalog.py): strict morph.tool_manifest/1 validation, Morph's own descriptions
for Claude, fingerprints, which tools are on, the owner's own manifests, and a broken manifest never breaking the rest."""

import copy
import errno
import json
import os

import pytest

from morph.studio.toolbox import ManifestError, audit, catalog
from toolbox_helpers import FIXTURES, ScriptedClaude, toolbox

DOC = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))


def parse(mut=None, **kw):
    doc = copy.deepcopy(DOC)
    if mut:
        mut(doc)
    return catalog.parse_manifest(doc, **kw)


@pytest.fixture
def cat_home(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    return str(tmp_path / "home")


def test_the_fixture_manifest_loads_with_morphs_descriptions():
    info, tools, creds = parse(curated=True)
    assert info["id"] == "fixparts" and [t.name for t in tools] == ["fixparts_offers", "fixparts_categories", "fixparts_quote_request"]
    offers = tools[0]
    d = offers.definition()
    assert set(d) == {"name", "description", "input_schema"} and d["input_schema"] == DOC["tools"][0]["input_schema"]
    assert d["description"].startswith(DOC["tools"][0]["description"])
    assert "Provider: FixParts (test fixture) (api.fixparts-morph.com)." in d["description"]
    assert "Read-only" in d["description"] and "No fee per call." in d["description"] and "Morph asks the person for it" in d["description"]
    assert "before every call" in tools[2].claude_description()
    assert offers.card() == {"tool": "fixparts_offers", "title": "FixParts price and stock", "service": "FixParts (test fixture)",
                             "provider": "fixparts", "side_effect": "read", "sends": "the manufacturer part number",
                             "privacy": DOC["privacy"], "host": "api.fixparts-morph.com", "cost_usd": 0.0,
                             "cost_note": "Counts toward the free 1,000 lookups a month", "curated": True}
    assert len({t.fingerprint for t in tools}) == 3 and parse()[1][0].fingerprint == offers.fingerprint
    assert creds[0].valid("fxp_" + "a" * 24) and not creds[0].valid("fxp_" + "a" * 23) and not creds[0].valid("xx_" + "a" * 30)


@pytest.mark.parametrize("where", ["top", "credential", "tool", "cost", "request", "auth", "response", "limits", "verified"])
def test_unknown_fields_are_refused_everywhere(where):
    def mut(d):
        target = {"top": d, "credential": d["credentials"][0], "tool": d["tools"][0], "cost": d["tools"][0]["cost"],
                  "request": d["tools"][0]["request"], "auth": d["tools"][0]["auth"], "response": d["tools"][0]["response"],
                  "limits": d["tools"][0]["limits"]}.get(where)
        if where == "verified":
            d["verified"] = {"docs": "https://www.fixparts-morph.com/docs", "checked": "2026-10-13", "live": None, "extra": 1}
        else:
            target["sneaky"] = True
    with pytest.raises(ManifestError):
        parse(mut)


@pytest.mark.parametrize("mut,msg", [
    (lambda d: d.update(schema="morph.tool_manifest/2"), "schema"),
    (lambda d: d.update(id="morph"), "id"),
    (lambda d: d.update(id="darwin"), "id"),
    (lambda d: d.update(id="mcp"), "id"),
    (lambda d: d.update(id="Fix-Parts"), "id"),
    (lambda d: d.update(hosts=["127.0.0.1"]), "public DNS name"),
    (lambda d: d.update(hosts=["localhost"]), "public DNS name"),
    (lambda d: d.update(hosts=["api.fixparts-morph.local"]), "public DNS name"),
    (lambda d: d.update(hosts=["[::1]"]), "public DNS name"),
    (lambda d: d.update(homepage="http://www.fixparts-morph.com"), "https"),
    (lambda d: d.pop("privacy"), "privacy"),
    (lambda d: d["tools"][0].pop("side_effect"), "side_effect"),
    (lambda d: d["tools"][0].update(side_effect="delete"), "side_effect"),
    (lambda d: d["tools"][0].pop("cost"), "cost"),
    (lambda d: d["tools"][0].update(cost={"usd_per_call": -1}), "usd_per_call"),
    (lambda d: d["tools"][0].update(cost={"usd_per_call": True}), "usd_per_call"),
    (lambda d: d["tools"][0].pop("sends"), "sends"),
    (lambda d: d["tools"][0].update(name="otherco_offers"), "fixparts_"),
    (lambda d: d["tools"][1].update(name="fixparts_offers"), "unique"),
    (lambda d: d["tools"][0].update(description="short"), "description"),
    (lambda d: d["tools"][0].update(title="bad\nline"), "title"),
    (lambda d: d["credentials"][0].update(hosts=["api.elsewhere-morph.com"]), "manifest's hosts"),
    (lambda d: d["credentials"][0].update(pattern="^(a+)+$"), "pattern"),
    (lambda d: d["credentials"][0].update(pattern="^.*$"), "empty"),
    (lambda d: d["credentials"][0].update(pattern="fxp_[a-z]+"), "pattern"),
    (lambda d: d["credentials"][0].update(url="javascript:alert(1)"), "https"),
    (lambda d: d["credentials"][0].update(prefix="has space"), "prefix"),
    (lambda d: d["credentials"].append({**d["credentials"][0], "id": "spare", "env": "MORPH_TOOL_SPARE_KEY"}), "no tool uses"),
    (lambda d: d["tools"][0]["auth"].update(credential="nobody"), "no credential"),
    (lambda d: d["tools"][0]["auth"].update({"in": "cookie"}), "header or query"),
    (lambda d: d["tools"][0]["auth"].update(name="Host"), "can't carry a key"),
    (lambda d: d["tools"][0]["request"]["headers"].update({"Authorization": "x"}), "not allowed"),
    (lambda d: d["tools"][0]["request"].update(url="https://api.fixparts-morph.com/v1/../admin/{mpn}"), "plain segments"),
    (lambda d: d["tools"][0]["request"].update(url="https://api.fixparts-morph.com/v1/%2e%2e/{mpn}"), "plain segments"),
    (lambda d: d.update(tools=[]), "tools"),
])
def test_manifest_rules(mut, msg):
    with pytest.raises(ManifestError, match=msg):
        parse(mut)


def test_key_placement_in_the_query_string_is_allowed_with_a_warning():
    def mut(d):
        d["tools"][0]["auth"] = {"credential": "fixparts", "in": "query", "name": "apikey"}
    tools = parse(mut)[1]
    assert any("query string" in w for w in tools[0].warnings)


def test_a_paid_tool_must_say_it_spends():
    def mut(d):
        d["tools"][1].update(side_effect="spend", cost={"usd_per_call": 0.04, "note": "per lookup"})
    t = parse(mut)[1][1]
    assert t.paid and "aren't supported in this beta" in t.claude_description() and "$0.04" in t.claude_description()


def test_the_owners_manifests_start_off_and_never_shadow_morphs(cat_home):
    folder = catalog.user_dir(cat_home)
    os.makedirs(folder)
    own = copy.deepcopy(DOC)
    own.update(id="ownparts", service="OwnParts", hosts=["api.ownparts-morph.com"], credentials=[])
    own["tools"] = [dict(copy.deepcopy(DOC["tools"][1]), name="ownparts_search",
                         request={"method": "GET", "url": "https://api.ownparts-morph.com/v1/s", "query": {"q": "{query}"}})]
    with open(os.path.join(folder, "ownparts.json"), "w", encoding="utf-8") as fh:
        json.dump(own, fh)
    with open(os.path.join(folder, "copycat.json"), "w", encoding="utf-8") as fh:
        json.dump(DOC, fh)  # the same provider id as Morph's own
    clash = copy.deepcopy(own)
    clash.update(id="clashparts", hosts=["api.ownparts-morph.com"])
    clash["tools"][0]["name"] = "clashparts_search"
    clash["credentials"] = [dict(DOC["credentials"][0], hosts=["api.ownparts-morph.com"])]  # Morph's key name
    clash["tools"][0]["auth"] = {"credential": "fixparts", "in": "header", "name": "X-Key"}
    with open(os.path.join(folder, "clash.json"), "w", encoding="utf-8") as fh:
        json.dump(clash, fh)
    with open(os.path.join(folder, "broken.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    cat = catalog.load(cat_home)
    assert "ownparts_search" in cat.tools and not cat.tools["ownparts_search"].curated
    assert cat.credentials["MORPH_TOOL_FIXPARTS_KEY"].provider == "fixparts" and cat.credentials["MORPH_TOOL_FIXPARTS_KEY"].curated
    errors = {e["source"]: e["error"] for e in cat.errors}
    assert "already in the catalog (a built-in provider)" in errors["copycat.json"]
    assert "MORPH_TOOL_FIXPARTS_KEY" in errors["clash.json"] and "broken.json" in errors
    on = catalog.enabled_names(cat, cat_home)
    assert "ownparts_search" not in on and {"fixparts_offers", "fixparts_categories", "fixparts_quote_request"} <= on
    tb = toolbox(cat_home, ScriptedClaude())
    assert tb.set_enabled("ownparts_search", True, by="m_owner") == {"tool": "ownparts_search", "enabled": True, "changed": True}
    assert tb.set_enabled("ownparts_search", True)["changed"] is False
    assert "ownparts_search" in catalog.enabled_names(catalog.load(cat_home), cat_home)
    view = {t["name"]: t for t in tb.tools()["tools"]}
    assert view["ownparts_search"]["enabled"] is True and view["ownparts_search"]["curated"] is False
    assert view["fixparts_offers"]["key"] == {"env": "MORPH_TOOL_FIXPARTS_KEY", "label": "FixParts API key", "set": False,
                                             "url": "https://www.fixparts-morph.com/developers/keys",
                                             "where": "FixParts dashboard, Developers, API keys", "hosts": ["api.fixparts-morph.com"]}
    assert {e["source"] for e in tb.tools()["errors"]} == {"copycat.json", "clash.json", "broken.json"}
    with pytest.raises(Exception) as e:
        tb.set_enabled("nothing_here", True)
    assert getattr(e.value, "status", None) == 404
    assert [x["event"] for x in audit.entries(cat_home)] == ["tool_enabled"]


def test_the_catalog_is_cached_until_a_manifest_changes(cat_home, tmp_path):
    folder = tmp_path / "cat"
    folder.mkdir()
    (folder / "fixparts.json").write_text(json.dumps(DOC), encoding="utf-8")
    a = catalog.load(cat_home, curated_dirs=[str(folder)])
    assert catalog.load(cat_home, curated_dirs=[str(folder)]) is a
    doc = copy.deepcopy(DOC)
    doc["tools"][0]["description"] += " Updated."
    (folder / "fixparts.json").write_text(json.dumps(doc), encoding="utf-8")
    os.utime(folder / "fixparts.json", ns=(1, 1))
    b = catalog.load(cat_home, curated_dirs=[str(folder)])
    assert b is not a and b.tools["fixparts_offers"].fingerprint != a.tools["fixparts_offers"].fingerprint
    (folder / "big.json").write_text(" " * (catalog.MAX_MANIFEST_BYTES + 1), encoding="utf-8")
    assert any("larger than" in e["error"] for e in catalog.load(cat_home, curated_dirs=[str(folder)]).errors)


def test_a_manifest_windows_refuses_to_open_for_a_moment_loads_the_next_time(cat_home, tmp_path, monkeypatch):
    """Windows refuses to open a manifest for a moment after os.replace has renamed it (a virus scanner can do the
    same). A catalog loaded in that moment lists the refusal but is not remembered: the next load has the tools."""
    folder = tmp_path / "cat"
    folder.mkdir()
    path = str(folder / "fixparts.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DOC, fh)
    refused, real = [], open

    def busy(p, *a, **k):
        if os.fspath(p) == path and not refused:
            refused.append(p)
            raise PermissionError(errno.EACCES, "The process cannot access the file because it is being used by "
                                                "another process", p)
        return real(p, *a, **k)

    monkeypatch.setattr(catalog, "open", busy, raising=False)
    first = catalog.load(cat_home, curated_dirs=[str(folder)])
    assert refused and first.tools == {} and [e["source"] for e in first.errors] == ["fixparts.json"]
    again = catalog.load(cat_home, curated_dirs=[str(folder)])
    assert "fixparts_offers" in again.tools and again.errors == []           # read again, not remembered as missing
    assert catalog.load(cat_home, curated_dirs=[str(folder)]) is again        # then remembered
