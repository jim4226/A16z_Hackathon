"""Live check of Morph writes a tool (preview): never part of a normal or CI run. MORPH_LIVE=1 with ANTHROPIC_API_KEY set
runs it (the key is read when this file is collected, before the test fixtures clear keys); it spends some Claude usage
(one draft: up to four documentation pages read with web fetch and one structured answer).

Real Claude reads a real API's public documentation through web fetch, limited to that site, and answers with the draft
form; Morph builds and checks the spec. A small read-only API with a key in the query string should come out ready for
review: every page read from the documentation's own site, every host named in those pages, GET only, its key under
MORPH_TOOL_*, and approving it installs exactly the reviewed spec. Nothing calls the API itself."""

import json
import os
import urllib.parse

import pytest

_KEY = os.environ.get("ANTHROPIC_API_KEY") if os.environ.get("MORPH_LIVE") == "1" else None
pytestmark = [pytest.mark.live, pytest.mark.network,
              pytest.mark.skipif(not _KEY, reason="live check: set MORPH_LIVE=1 and ANTHROPIC_API_KEY (costs Claude usage)")]

DOCS = "https://openweathermap.org/current"  # a public read-only API: GET with the key in the appid query parameter
NEED = "The current weather for a city, to plan an outdoor robot test"


def test_a_real_draft_from_public_documentation_is_ready_and_installs_as_reviewed(tmp_path, monkeypatch):
    import anthropic

    from morph import keys
    from morph.studio.toolbox import catalog
    from toolbox_helpers import FIXTURES, toolbox

    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    client = lambda: anthropic.Anthropic(api_key=_KEY, timeout=600, max_retries=2)  # noqa: E731
    tb = toolbox(tmp_path / "home", client_factory=client, http_resolver=None)  # the real DNS check of the API's hosts
    view, _ = tb.drafts.start(need=NEED, docs_url=DOCS, project="egg-picker", by="live")
    d = tb.drafts.get(view["id"])
    print(json.dumps({k: d.get(k) for k in ("status", "note", "reason", "problems", "warnings", "notes", "pages", "usage")}, indent=1))
    assert d["status"] == "ready", d["problems"] or d.get("note") or d.get("reason")
    host = urllib.parse.urlsplit(DOCS).hostname
    assert d["pages"] and all(urllib.parse.urlsplit(p["url"]).hostname == host for p in d["pages"])  # its own site only
    spec, card = d["spec"], d["card"]
    assert all(h["named"] for h in card["hosts"]) and all(h["addresses"] == "public" for h in card["hosts"])
    assert all(t["request"]["method"] == "GET" and t["side_effect"] == "read" for t in spec["tools"])
    assert all(c["env"].startswith("MORPH_TOOL_") for c in spec.get("credentials") or [])
    out = tb.drafts.approve(d["id"], d["sha256"], by="live")
    assert out["status"] == "installed"
    path = os.path.join(catalog.user_dir(str(tmp_path / "home")), out["installed"]["file"])
    with open(path, encoding="utf-8") as fh:
        assert catalog.fingerprint(json.load(fh)) == d["sha256"]
    assert _KEY not in json.dumps(tb.drafts.list())
