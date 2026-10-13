"""Keewano: ledger entries become typed events; one optional query calibrates the grip force.

Send side (REST, stdlib only): Project.log() calls emit(entry). Each project is one Keewano
entity (UUID v5 of its name), so its revisions, measurements, human minutes and Darwin
sourcing calls land in one ordered timeline. Design context (servo, pad, ...) is sent before
a measurement, so every measured value carries the design it was measured on, no join needed.
Nothing is sent unless the person turns on "Send build events to Keewano" (settings.py).

Query side (SQL through Keewano's MCP run_sql_query tool): hold_ratio(pad, material) returns how
much more (or less) holding force logged builds needed than the simulation predicted, and
electronics.plan() scales the simulated holding force by it. Off unless "Use Keewano calibration"
is on, and never from synthetic-* projects or fewer than MIN_BUILDS measurements. Logged builds are
whatever the Keewano project holds: the R Cubed demo history is seeded, not measured.

Env: KEEWANO_API_KEY (send), KEEWANO_PAT + KEEWANO_PROJECT_ID (query).
With none set, everything here is a no-op and Morph runs offline as before.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import uuid
import zlib
from urllib.request import Request

from . import settings
from .studio.security import open_keyed  # the key never follows a redirect

API = "https://api.keewano.com/event/ingress/v1/json"
MCP = "https://api.keewano.com/mcp"

# Append-only: Keewano never lets a registered id change type. Add new events at the end, each with an id from
# its feature's range (docs/internal/RELEASE_CONTRACTS.md, section 10), so features built in parallel never collide:
#   2000-2021  existing events (below)        2160-2179  video and photos
#   2022-2099  W0 foundations                 2180-2229  teams (pseudonymous: hashed member ids, never names)
#   2100-2129  brief (new robot from a line)  2230-2259  world mode
#   2130-2159  import                         2260-2279  SubQ
# Nothing is sent unless the person turns on Keewano events (settings.telemetry_enabled()).
CUSTOM_EVENTS = [
    {"eventTypeId": 2000, "name": "Project", "dataType": "string"},
    {"eventTypeId": 2001, "name": "Revision", "dataType": "string"},
    {"eventTypeId": 2002, "name": "ObjectName", "dataType": "string"},
    {"eventTypeId": 2003, "name": "PadStyle", "dataType": "string"},
    {"eventTypeId": 2004, "name": "Servo", "dataType": "string"},
    {"eventTypeId": 2005, "name": "Material", "dataType": "string"},
    {"eventTypeId": 2006, "name": "Sensor", "dataType": "string"},
    {"eventTypeId": 2007, "name": "StageRecomputed", "dataType": "string"},
    {"eventTypeId": 2008, "name": "PredictedMinHoldMilliN", "dataType": "uint"},
    {"eventTypeId": 2009, "name": "TargetForceMilliN", "dataType": "uint"},
    {"eventTypeId": 2010, "name": "MeasuredMinHoldMilliN", "dataType": "uint"},
    {"eventTypeId": 2011, "name": "HoldForceRatioPct", "dataType": "uint"},
    {"eventTypeId": 2012, "name": "GraspSuccessPct", "dataType": "uint"},
    {"eventTypeId": 2013, "name": "HumanStep", "dataType": "string"},
    {"eventTypeId": 2014, "name": "HumanMinutes", "dataType": "uint"},
    {"eventTypeId": 2015, "name": "ToolCommand", "dataType": "string"},
    {"eventTypeId": 2016, "name": "ToolSeconds", "dataType": "uint"},
    {"eventTypeId": 2017, "name": "ExploreRecommendedPad", "dataType": "string"},
    {"eventTypeId": 2018, "name": "SourcingPart", "dataType": "string"},
    {"eventTypeId": 2019, "name": "SourcingAgentsFound", "dataType": "uint"},
    {"eventTypeId": 2020, "name": "SourcingTopAgent", "dataType": "string"},
    {"eventTypeId": 2021, "name": "SourcingGap", "dataType": "string"},
    # brief (F1, morph/studio/agents.py)
    {"eventTypeId": 2100, "name": "BriefStarted", "dataType": "string"},
    {"eventTypeId": 2101, "name": "BriefRoute", "dataType": "string"},
    {"eventTypeId": 2102, "name": "BriefTriageSource", "dataType": "string"},
    {"eventTypeId": 2103, "name": "BriefQuestion", "dataType": "string"},
    {"eventTypeId": 2104, "name": "BriefAnswer", "dataType": "string"},
    {"eventTypeId": 2105, "name": "BriefDefaulted", "dataType": "string"},
    {"eventTypeId": 2106, "name": "BriefRounds", "dataType": "uint"},
    {"eventTypeId": 2107, "name": "BriefOutOfScope", "dataType": "string"},
    {"eventTypeId": 2108, "name": "BriefRefused", "dataType": "string"},
    {"eventTypeId": 2109, "name": "BriefRepair", "dataType": "string"},
    {"eventTypeId": 2110, "name": "BriefPreflightMs", "dataType": "uint"},
    {"eventTypeId": 2111, "name": "BriefEstimateUsd", "dataType": "uint"},
    {"eventTypeId": 2112, "name": "BriefNearestAccepted", "dataType": "string"},
    {"eventTypeId": 2113, "name": "BriefBuilt", "dataType": "string"},
    {"eventTypeId": 2114, "name": "BriefBuildFeasible", "dataType": "uint"},
    # video and photos (F3, morph/studio/api_video.py)
    {"eventTypeId": 2160, "name": "VideoAnalysis", "dataType": "string"},
    {"eventTypeId": 2161, "name": "VideoFrames", "dataType": "uint"},
    {"eventTypeId": 2162, "name": "VideoAnalyzeMs", "dataType": "uint"},
    {"eventTypeId": 2163, "name": "VideoSource", "dataType": "string"},
    {"eventTypeId": 2164, "name": "VideoUsable", "dataType": "uint"},
    {"eventTypeId": 2165, "name": "VideoQuestions", "dataType": "uint"},
    {"eventTypeId": 2166, "name": "VideoCostMilliUsd", "dataType": "uint"},
    {"eventTypeId": 2167, "name": "VideoChangesApplied", "dataType": "uint"},
    {"eventTypeId": 2168, "name": "VideoDecodeMs", "dataType": "uint"},
    {"eventTypeId": 2169, "name": "VideoDecoder", "dataType": "string"},
    {"eventTypeId": 2170, "name": "VideoSecondOpinion", "dataType": "string"},
    # teams (F4, morph/studio/teams/activity.py; hashed member ids, never names)
    {"eventTypeId": 2180, "name": "TeamActor", "dataType": "string"},
    {"eventTypeId": 2181, "name": "TeamRole", "dataType": "string"},
    {"eventTypeId": 2182, "name": "CommentAdded", "dataType": "string"},
    {"eventTypeId": 2183, "name": "ChangeRequestOpened", "dataType": "string"},
    {"eventTypeId": 2184, "name": "ChangeRequestApplied", "dataType": "string"},
    {"eventTypeId": 2185, "name": "ReviewRequested", "dataType": "string"},
    {"eventTypeId": 2186, "name": "ReviewDecision", "dataType": "string"},
    {"eventTypeId": 2187, "name": "ApprovedRevision", "dataType": "string"},
    {"eventTypeId": 2188, "name": "ReleaseLevel", "dataType": "string"},
    {"eventTypeId": 2189, "name": "ReviewDisciplines", "dataType": "uint"},
    # import (importers.common.KEEWANO_EVENTS): categories and counts, never file names, links or robot names
    {"eventTypeId": 2130, "name": "ImportCommitted", "dataType": "string"},
    {"eventTypeId": 2131, "name": "ImportFormat", "dataType": "string"},
    {"eventTypeId": 2132, "name": "ImportFrom", "dataType": "string"},
    {"eventTypeId": 2133, "name": "ImportMotorsPicked", "dataType": "uint"},
    {"eventTypeId": 2134, "name": "ImportWarnings", "dataType": "uint"},
    # world mode (studio.worlds.KEEWANO_EVENTS): the kind of room, counts, the measured verdict, timings
    {"eventTypeId": 2230, "name": "WorldSaved", "dataType": "string"},
    {"eventTypeId": 2231, "name": "WorldBoxes", "dataType": "uint"},
    {"eventTypeId": 2232, "name": "WorldPeopleAreas", "dataType": "uint"},
    {"eventTypeId": 2233, "name": "WorldVerdict", "dataType": "string"},
    {"eventTypeId": 2234, "name": "WorldReview", "dataType": "string"},
    {"eventTypeId": 2235, "name": "WorldInspectMs", "dataType": "uint"},
    # SubQ (studio.subq.KEEWANO_EVENTS): which decision, cached or not, time and input tokens
    {"eventTypeId": 2260, "name": "SubqDecision", "dataType": "string"},
    {"eventTypeId": 2261, "name": "SubqSource", "dataType": "string"},
    {"eventTypeId": 2262, "name": "SubqMs", "dataType": "uint"},
    {"eventTypeId": 2263, "name": "SubqInputTokens", "dataType": "uint"},
]
_ID = {e["name"]: (e["eventTypeId"], e["dataType"]) for e in CUSTOM_EVENTS}
EVENTS_HASH = zlib.crc32(json.dumps(CUSTOM_EVENTS, sort_keys=True).encode()) or 1

_session = str(uuid.uuid4())
_batch = 0
_registered = False
_warned = False


def entity_id(project_name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"morph:{project_name}"))


def _mn(newtons) -> int | None:
    if newtons is None or newtons == float("inf"):
        return None
    return max(0, round(float(newtons) * 1000))


def _design_ctx(design: dict, servo: str | None = None) -> list:
    f, e, o = design.get("finger", {}), design.get("electronics", {}), design.get("object", {})
    return [("ObjectName", o.get("name")), ("PadStyle", f.get("pad")), ("Servo", servo or e.get("servo")),
            ("Material", f.get("material")), ("Sensor", f.get("sensor"))]


def events_for(entry: dict, design: dict | None = None) -> list:
    """Ledger entry -> [(event name, value)]. Pure; Keewano wants Name(value), not logs."""
    t = entry.get("type")
    ev = []
    if t == "revision":
        p = entry.get("predicted") or {}
        ev += [("Revision", entry.get("rev"))] + _design_ctx(entry.get("design") or {}, p.get("servo"))
        ev += [("StageRecomputed", s) for s in entry.get("recomputed") or []]
        ev += [("PredictedMinHoldMilliN", _mn(p.get("min_hold_force_n"))),
               ("TargetForceMilliN", _mn(p.get("target_force_n")))]
    elif t == "measurement":
        a, err = entry.get("actual") or {}, entry.get("error_vs_prediction") or {}
        ev += [("Revision", entry.get("rev"))] + _design_ctx(design or {})
        meas = a.get("min_hold_force_n")
        ev.append(("MeasuredMinHoldMilliN", _mn(meas)))
        synthetic = bool(a.get("simulated") or entry.get("synthetic"))  # a rehearsal trace is not a build
        if meas is not None and "min_hold_force_n" in err and not synthetic:
            pred = meas - err["min_hold_force_n"]
            if pred > 0:
                ev.append(("HoldForceRatioPct", round(100 * meas / pred)))
        if a.get("grasp_success_rate") is not None:
            ev.append(("GraspSuccessPct", round(100 * a["grasp_success_rate"])))
    elif t == "human_time":
        ev += [("HumanStep", entry.get("step")), ("HumanMinutes", round(entry.get("minutes") or 0))]
    elif t == "tool_time":
        ev += [("ToolCommand", entry.get("cmd")), ("ToolSeconds", round(entry.get("seconds") or 0))]
    elif t == "exploration":
        pads = {rev: pad for pad, rev in (entry.get("candidates") or {}).items()}
        ev.append(("ExploreRecommendedPad", pads.get(entry.get("recommended"))))
    elif t == "sourcing":
        ev += [("SourcingPart", entry.get("part")), ("SourcingAgentsFound", entry.get("found", 0))]
        if entry.get("top_agent"):
            ev.append(("SourcingTopAgent", entry["top_agent"]))
        if entry.get("gap"):
            ev.append(("SourcingGap", entry["gap"]))
    elif t in ("video_analysis", "video_apply", "video_decode"):  # counts and timings only (api_video)
        from .studio.api_video import keewano_events
        ev += keewano_events(entry)
    elif t == "import":  # categories and counts only (importers.common)
        from .importers.common import keewano_events
        ev += keewano_events(entry)
    elif t in ("world", "world_inspect"):  # room kind, counts, verdict and timings (studio.worlds)
        from .studio.worlds import keewano_events
        ev += keewano_events(entry)
    elif t == "subq":  # which decision, cached or not, time and tokens (studio.subq)
        from .studio.subq import keewano_events
        ev += keewano_events(entry)
    return [(n, v) for n, v in ev if v is not None and v != ""]


def _post(path: str, body: dict, key: str):
    req = Request(f"{API}/{path}", data=json.dumps(body).encode(),
                  headers={"k-token": key, "Content-Type": "application/json"})
    with open_keyed(req, timeout=5) as resp:
        return json.load(resp)


def send(project_name: str, events: list, ts: int | None = None, key: str | None = None) -> bool:
    """POST one batch for one project entity. Never raises: Keewano must not break a design run."""
    global _batch, _registered, _warned
    if not settings.telemetry_enabled():  # opt-in: a key alone never sends anything
        return False
    key = key or os.environ.get("KEEWANO_API_KEY")
    if not key or not events:
        return False
    ts = int(ts or time.time())
    try:
        if not _registered:
            _post("custom", {"customEventsHash": EVENTS_HASH, "eventTypes": CUSTOM_EVENTS}, key)
            _registered = True
        _batch += 1
        body = [{"eventTypeId": _ID[n][0], "dataType": _ID[n][1], "timestamp": ts, "data": v}
                for n, v in [("Project", project_name)] + events]
        _post("in", {"dataSessionId": _session, "batch": _batch, "customEventsHash": EVENTS_HASH,
                     "installId": entity_id(project_name), "events": body}, key)
        return True
    except Exception as exc:  # network, auth, schema: report once, keep designing
        if not _warned:
            print(f"keewano: send failed ({exc}); continuing offline", file=sys.stderr)
            _warned = True
        return False


_queue = None
_queue_lock = threading.Lock()


def emit(project_name: str, entry: dict, design: dict | None = None) -> bool:
    """Queue one ledger entry for Keewano and return at once: a slow or unreachable Keewano must not
    hold up a design run or the Studio (each send can take up to its 5 s timeout). One background
    thread sends in order, so batch numbers stay sequential."""
    global _queue
    if not settings.telemetry_enabled() or not os.environ.get("KEEWANO_API_KEY"):
        return False
    events = events_for(entry, design)
    if not events:
        return False
    with _queue_lock:
        if _queue is None:
            _queue = queue.Queue()

            def worker():
                while True:
                    name, evs, ts = _queue.get()
                    send(name, evs, ts=ts)

            threading.Thread(target=worker, daemon=True, name="keewano-sender").start()
    _queue.put((project_name, events, int(time.time())))
    return True


# -- query side ------------------------------------------------------------------------------

_ratio_cache: dict = {}
MIN_BUILDS = 3  # fewer logged measurements than this calibrate nothing


def _lit(v: str) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def query(sql: str, max_rows: int = 100) -> list | None:
    """Run read-only SQL against the project's events via Keewano's MCP run_sql_query tool.

    Only the personal access token and project id are needed. None when not configured or
    unreachable, so a design run never waits on it.
    """
    pat, project = os.environ.get("KEEWANO_PAT"), os.environ.get("KEEWANO_PROJECT_ID")
    if not (pat and project) or not settings.network_allowed():
        return None
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_sql_query",
                       "arguments": {"projectId": project, "sqlQuery": sql, "maxRows": max_rows}}}
    try:
        req = Request(MCP, data=json.dumps(body).encode(),
                      headers={"k-token": pat, "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"})
        with open_keyed(req, timeout=30) as resp:
            out = json.load(resp)
        res = out.get("result") or {}
        if res.get("isError") or "structuredContent" not in res:
            raise RuntimeError((res.get("content") or [{}])[0].get("text") or out.get("error"))
        return res["structuredContent"]["rows"]
    except Exception as exc:
        print(f"keewano: query failed ({exc})", file=sys.stderr)
        return None


def hold_ratio(pad: str, material: str) -> dict | None:
    """How logged builds' holding force compared with the simulation, for this pad + material, or None.

    Only when "Use Keewano calibration" is on (settings.py), from at least MIN_BUILDS measurements, and
    never from synthetic-* projects (tools/keewano_backfill.py). The ratio is only as real as the project's
    history: the R Cubed demo project is seeded, so the Studio labels it as logged, not measured.

    Holding force is a friction question (pad shape, pad material), so that is the key; the
    servo only sets how hard the gripper *can* squeeze.

    Returns {"ratio": measured/predicted, "n": measurements, "source": "keewano"} or None.
    Each HoldForceRatioPct event already carries the PadStyle and Material in effect when it was
    measured (Keewano columns hold their last value forward), so this is a plain WHERE.
    """
    if not settings.calibration_enabled():
        return None
    k = (pad, material)
    if k not in _ratio_cache:
        rows = query('SELECT avg("HoldForceRatioPct") AS r, count("HoldForceRatioPct") AS n FROM events '
                     f'WHERE "PadStyle" = {_lit(pad)} AND "Material" = {_lit(material)} '
                     """AND "Project" NOT LIKE 'synthetic-%'""")
        r = None
        if rows and rows[0] and rows[0][0] is not None and int(rows[0][1] or 0) >= MIN_BUILDS:
            ratio = min(3.0, max(0.5, float(rows[0][0]) / 100))  # one bad bench run can't wreck a design
            r = {"ratio": round(ratio, 3), "n": int(rows[0][1]), "source": "keewano", "min_builds": MIN_BUILDS}
        _ratio_cache[k] = r
    return _ratio_cache[k]
