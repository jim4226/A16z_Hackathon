"""Optional Keewano analytics. Remote history never changes generated force targets.

Send side (REST, stdlib only): Project.log() calls emit(entry). Each project is one Keewano
entity (UUID v5 of its name), so its revisions, measurements, human minutes and Darwin
sourcing calls land in one ordered timeline. Design context (servo, pad, ...) is sent before
a measurement, so every measured value carries the design it was measured on, no join needed.

Query side (SQL through Keewano's MCP run_sql_query tool) is for explicit analytics only.
Automatic force calibration is disabled until measurement provenance is verified.

Env: KEEWANO_API_KEY (send), KEEWANO_PAT + KEEWANO_PROJECT_ID (query).
Sending additionally requires explicit telemetry consent in Settings or
MORPH_TELEMETRY_ENABLED=1. MORPH_OFFLINE=1 prevents all network operations.
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
from urllib.request import Request, urlopen

from . import settings

API = "https://api.keewano.com/event/ingress/v1/json"
MCP = "https://api.keewano.com/mcp"

# Append-only: Keewano never lets a registered id change type. Add new events at the end.
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
    {"eventTypeId": 2022, "name": "MeasurementSource", "dataType": "string"},
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
        synthetic = bool(a.get("simulated") or entry.get("synthetic"))
        ev.append(("MeasurementSource", "synthetic" if synthetic else "unverified"))
        ev += [("Revision", entry.get("rev"))] + _design_ctx(design or {})
        meas = a.get("min_hold_force_n")
        ev.append(("MeasuredMinHoldMilliN", _mn(meas)))
        if not synthetic and meas is not None and "min_hold_force_n" in err:
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
    return [(n, v) for n, v in ev if v is not None and v != ""]


def _post(path: str, body: dict, key: str):
    req = Request(f"{API}/{path}", data=json.dumps(body).encode(),
                  headers={"k-token": key, "Content-Type": "application/json"})
    with urlopen(req, timeout=5) as resp:
        return json.load(resp)


def send(project_name: str, events: list, ts: int | None = None, key: str | None = None) -> bool:
    """POST one batch for one project entity. Never raises: Keewano must not break a design run."""
    global _batch, _registered, _warned
    if not settings.telemetry_enabled():
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
        if project_name.startswith("synthetic-"):
            events = [(n, v) for n, v in events if n not in ("HoldForceRatioPct", "MeasurementSource")]
            events.append(("MeasurementSource", "synthetic"))
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
    if not settings.telemetry_enabled():
        return False
    events = events_for(entry, design)
    if not events or not os.environ.get("KEEWANO_API_KEY"):
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

def query(sql: str, max_rows: int = 100) -> list | None:
    """Run read-only SQL against the project's events via Keewano's MCP run_sql_query tool.

    Only the personal access token and project id are needed. None when not configured or
    unreachable, so a design run never waits on it.
    """
    if not settings.network_allowed():
        return None
    pat, project = os.environ.get("KEEWANO_PAT"), os.environ.get("KEEWANO_PROJECT_ID")
    if not (pat and project):
        return None
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_sql_query",
                       "arguments": {"projectId": project, "sqlQuery": sql, "maxRows": max_rows}}}
    try:
        req = Request(MCP, data=json.dumps(body).encode(),
                      headers={"k-token": pat, "Content-Type": "application/json",
                               "Accept": "application/json, text/event-stream"})
        with urlopen(req, timeout=30) as resp:
            out = json.load(resp)
        res = out.get("result") or {}
        if res.get("isError") or "structuredContent" not in res:
            raise RuntimeError((res.get("content") or [{}])[0].get("text") or out.get("error"))
        return res["structuredContent"]["rows"]
    except Exception as exc:
        print(f"keewano: query failed ({exc})", file=sys.stderr)
        return None


def hold_ratio(pad: str, material: str) -> dict | None:
    """Disabled in the public beta: history has no verified measurement provenance.

    Kept as a compatibility shim. It never queries the network or returns a multiplier,
    including when remote history contains old synthetic calibration events.
    """
    return None
