"""docs/TOOLS.md stays true to the code: its links resolve, its manifest example is one Morph loads without a warning
(the test fixture's own, cut to one tool), and the limits it states are the policy's."""

import json
import pathlib
import re

from morph.studio.toolbox import catalog, draft, policy, runs

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "docs" / "TOOLS.md").read_text(encoding="utf-8")


def test_its_links_resolve():
    links = re.findall(r"\]\(([^)\s]+)\)", TEXT)
    assert links
    for link in links:
        path = link.split("#", 1)[0]
        if path and not re.match(r"[a-z]+:", path):
            assert (REPO / "docs").joinpath(path).resolve().exists(), link


def test_its_manifest_example_loads_without_a_warning():
    doc = json.loads(re.search(r"```json\n(.*?)\n```", TEXT, re.S).group(1))
    info, tools, _ = catalog.parse_manifest(doc, source="docs/TOOLS.md", curated=False)
    assert info["id"] == "fixparts" and [t.name for t in tools] == ["fixparts_offers"] and not tools[0].warnings
    fixture = json.loads((REPO / "tests" / "fixtures" / "tools" / "fixparts.json").read_text(encoding="utf-8"))
    assert doc["credentials"] == fixture["credentials"] and doc["hosts"] == fixture["hosts"]


def test_the_limits_it_states_are_the_policys():
    text = " ".join(TEXT.split())
    assert policy.MAX_CALLS_PER_TOOL == 20 and policy.MAX_TURNS == 40 and policy.SPEND_CAP_USD == 0
    assert "at most 20 calls of one tool and 40 requests to Claude" in text and "the spending cap is $0" in text
    assert runs.TTL_S == draft.TTL_S == 7 * 86400 and "untouched for 7 days is gone" in text
