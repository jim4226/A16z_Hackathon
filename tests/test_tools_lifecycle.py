"""Agent runs and the project lifecycle (B2-M, contracts section 17). Deleting a project and Reset demo already stop the
project's robustness study; they also end the project's agent runs that still wait or work: a run that waits for a key
never continues on a revision that is gone, or on another project that takes the name later. Finished runs, and runs
of the revision Reset demo keeps, are left as they are. Claude is the real SDK over a mock transport and the FixParts
fixture API answers in-process (toolbox_helpers): nothing leaves the test."""

import json
import os

from morph.studio import server
from test_team_util import build
from test_tools_api import World
from toolbox_helpers import BOM, ENV, KEY, SYSTEM, final, search_and_call

OFFERS = ("fixparts_offers", "toolu_nx", {"mpn": "STS3215"})


def waiting(w, project, rev):
    """A run of `project` for `rev` that waits for the FixParts key, the way Live prices waits for a distributor's."""
    v = w.studio.toolbox.start(agent="test_prices", system=SYSTEM, prompt=f"Price the purchased parts of {rev}.",
                               project=project, core=[BOM], context={"rev": rev}, title=f"Test prices for {rev}")
    assert v["status"] == "paused" and v["waiting_on"] == ["credential"], v
    return v["id"]


def test_deleting_a_project_ends_its_waiting_run_and_a_new_project_of_that_name_starts_clean(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, (search_and_call(OFFERS), final("Not reached: its project was deleted.")))
    try:
        build(os.path.join(w.home, "brick-sorter"), "r001")
        run = waiting(w, "brick-sorter", "r001")
        q = w.studio.room("brick-sorter").hub.subscribe()
        s, out = w.call("DELETE", "/api/projects?project=brick-sorter")
        assert s == 200, out
        view = w.studio.toolbox.get(run)
        assert view["status"] == "cancelled" and view["waiting_on"] == [] and view["cards"] == []
        assert view["note"] == "Stopped: brick-sorter was deleted."
        sent = []
        while not q.empty():
            sent.append(q.get_nowait())
        assert [e["type"] for e in sent if e.get("run") == run] == ["run_done"]  # the deleted project's tabs see it end
        # a new robot that takes the name: nothing of the old run waits in it, and saving the key resumes nothing
        build(os.path.join(w.home, "brick-sorter"), "r001")
        s, listed = w.call("GET", "/api/runs?project=brick-sorter")
        assert s == 200 and [r["status"] for r in listed["runs"]] == ["cancelled"]
        s, out = w.call("POST", "/api/keys", {"values": {ENV: KEY}})
        assert s == 200 and out["resumed"] == []
        assert len(w.claude.requests) == 1 and not w.vendor.calls  # Claude never asked again; FixParts never called
    finally:
        w.close()


def test_reset_demo_ends_the_waiting_runs_of_the_revisions_it_removes(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch, (search_and_call(OFFERS), search_and_call(OFFERS), final("STS3215: 412 at FixParts.")))
    try:
        p = w.studio.room().p
        with open(os.path.join(p.root, "meta.json"), "w", encoding="utf-8") as fh:  # a demo project, as the launcher's
            json.dump({"kind": "robot", "demo": True}, fh)
        with open(os.path.join(p.root, "r001", "results.json"), encoding="utf-8") as fh:
            res = json.load(fh)
        res["pipeline_version"] = server.pipeline_version(p.load_design("r001").arm.source)  # current: no rebuild
        with open(os.path.join(p.root, "r001", "results.json"), "w", encoding="utf-8") as fh:
            json.dump(res, fh)
        build(p.root, "r002", parent="r001", changes={"arm.dof": 4})
        gone, kept = waiting(w, "egg-picker", "r002"), waiting(w, "egg-picker", "r001")
        assert w.studio.reset() is True and p.revs() == ["r001"]
        assert w.studio.toolbox.get(gone)["status"] == "cancelled"
        assert w.studio.toolbox.get(gone)["note"] == "Stopped: Reset demo removed the revision it was for."
        assert w.studio.toolbox.get(kept)["status"] == "paused"  # the first build is kept, so its run still waits
        s, out = w.call("POST", "/api/keys", {"values": {ENV: KEY}})
        assert s == 200 and out["resumed"] == [kept]
        assert w.studio.toolbox.get(kept)["status"] == "done"
    finally:
        w.close()
