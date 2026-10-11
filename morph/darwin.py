"""Darwin: find agents that can supply the parts and do the work Morph doesn't.

Morph designs and programs. After a revision is built, `morph source` asks Darwin Search
(no account needed) for:
  - every purchased BOM line: an agent that can quote and sell it (servo, MCU, FSR, supply...)
  - engineering handoffs: PCB fab from the Gerbers, printing the STLs, a design review

Search only shortlists. Ordering is Darwin Act, which needs the user's own OAuth grant and
their approval of any payment, so Morph stops at the shortlist plus each agent's page and
never claims an order was placed. Every lookup is logged to the ledger (and so to Keewano),
including the parts nobody on the agentic web can supply yet: Darwin's brief asks for those
gaps explicitly.

Darwin's API returns intermittent 500s, so each query retries once and every good answer is
cached on disk; a cached answer is labelled as such.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.darwin.so/api/v3/search"
AGENT_PAGE = "https://index.darwin.so/agent/{agent}?capability={cap}"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "morph", "darwin")


def _cache_path(query: str) -> str:
    return os.path.join(CACHE, hashlib.sha1(query.encode()).hexdigest()[:16] + ".json")


def _normalize(query: str, raw: dict) -> dict:
    resp = raw.get("response") or {}
    agents = []
    for a in resp.get("agents") or []:
        agents.append({
            "name": a.get("name"),
            "description": (a.get("description") or "")[:240],
            "readiness": a.get("readiness"),
            "can_start": bool(a.get("canStartThread")),
            "reasons": a.get("reasons") or [],
            "uncertainties": a.get("uncertainties") or [],
            "agent_id": a.get("agentId"),
            "capability_id": a.get("capabilityId"),
            "url": AGENT_PAGE.format(agent=a.get("agentId"), cap=a.get("capabilityId")),
        })
    return {"query": query, "ok": True, "search_id": raw.get("searchId"), "agents": agents,
            "no_match_reason": resp.get("noMatchReason")}


def search(query: str, max_results: int = 5, retries: int = 1, timeout: float = 45) -> dict:
    """Darwin Search -> {"query", "ok", "agents": [...], "cached"?, "error"?}. Never raises."""
    body = json.dumps({"query": query, "maxResults": max_results}).encode()
    err = None
    for attempt in range(retries + 1):
        try:
            req = Request(API, data=body, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                out = _normalize(query, json.load(resp))
            os.makedirs(CACHE, exist_ok=True)
            with open(_cache_path(query), "w") as fh:
                json.dump({**out, "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
            return out
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            err = f"{exc.__class__.__name__}: {exc}"
            if attempt < retries:
                time.sleep(2)
    try:
        with open(_cache_path(query)) as fh:
            return {**json.load(fh), "cached": True, "error": err}
    except (OSError, ValueError):
        return {"query": query, "ok": False, "agents": [], "error": err}


def part_query(item: str) -> str:
    return f"buy {item}; quote unit price, stock and shipping to the US"


def is_purchased(row: dict) -> bool:
    item = (row.get("item") or "").lower()
    made = any(w in item for w in ("fdm_print", "sla_print", "laser", "cnc", "printed", "friction tape"))
    return row.get("cost", 0) > 0 and not made and row.get("note") != "existing hardware"


def handoffs(rev_dir: str, results: dict) -> list:
    """Engineering work Morph hands off once the design exists: (kind, query, files)."""
    out = []
    gerbers = os.path.join(rev_dir, "electronics", "board", "gerbers", "gerbers.zip")
    if os.path.exists(gerbers):
        files = [os.path.relpath(gerbers, rev_dir)]
        kicad_zip = os.path.join(rev_dir, "electronics", "board", "kicad", "board-kicad.zip")
        if os.path.exists(kicad_zip):   # the KiCad project, for a fab that wants sources (adds solder mask)
            files.append(os.path.relpath(kicad_zip, rev_dir))
        out.append(("pcb_fab", "PCB fabrication and assembly quote from Gerber files, 2-layer through-hole", files))
    fab_dir = os.path.join(rev_dir, "fab")
    stls = sorted(f for f in os.listdir(fab_dir) if f.endswith(".stl")) if os.path.isdir(fab_dir) else []
    if stls:
        mat = ((results.get("fab") or {}).get("material") or "PETG")
        out.append(("print", f"3D printing service quote for STL files in {mat}", [f"fab/{f}" for f in stls]))
    out.append(("design_review", "mechanical design review of a servo-driven robot gripper with force sensing", []))
    return out


def source(project, rev: str, max_results: int = 3, workers: int = 4) -> dict:
    """Shortlist Darwin agents for every purchased part and handoff of `rev`; log each lookup."""
    results = project.load_results(rev)
    rev_dir = project.rev_dir(rev)
    tasks = [("part", r["item"], part_query(r["item"]), []) for r in results.get("bom") or [] if is_purchased(r)]
    tasks += [(kind, kind, q, files) for kind, q, files in handoffs(rev_dir, results)]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = list(pool.map(lambda t: search(t[2], max_results), tasks))

    rows, gaps = [], []
    for (kind, label, query, files), res in zip(tasks, found):
        agents = res["agents"]
        gap = None
        if not res["ok"]:
            gap = "darwin_unavailable"
        elif not agents:
            gap = "no_agent"
        elif not any(a["can_start"] for a in agents):
            gap = "not_executable"  # listed, but Darwin can't start work with it yet
        if gap:
            gaps.append({"kind": kind, "item": label, "gap": gap, "reason": res.get("no_match_reason")})
        rows.append({"kind": kind, "item": label, "query": query, "files": files, "agents": agents,
                     "cached": res.get("cached", False), "error": res.get("error"), "gap": gap})
        project.log({"type": "sourcing", "rev": rev, "kind": kind, "part": label, "found": len(agents),
                     "top_agent": agents[0]["name"] if agents else None, "gap": gap})

    report = {"rev": rev, "source": "darwin search", "rows": rows, "gaps": gaps,
              "note": "Shortlist only. Ordering needs the user's Darwin account (Act) and their approval."}
    with open(os.path.join(rev_dir, "sourcing_darwin.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    return report
