"""Darwin: find agents that can supply the parts and do the work Morph doesn't.

Morph designs and programs. After a revision is built, `morph source` asks Darwin Search
(no account needed) for:
  - quote and order agents that work for any part (part-lookup, store_buy, ...)
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
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.darwin.so/api/v3/search"
AGENT_PAGE = "https://index.darwin.so/agent/{agent}?capability={cap}"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "morph", "darwin")
# Answers from live searches, shipped with the repo: the last fallback when Darwin is down
# and this machine has never searched that query (a fresh demo laptop).
SHIPPED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "darwin-cache")

# The agents we route each kind of work to, by Darwin agent id (names are not unique).
# Picked from live searches on 2026-10-11; Search still runs, these just go first and are
# the ones `morph source` shows. Others found are kept in the JSON.
QUOTE = {"cmv08cln8001t06nyw4bux80d": "Request a quote from matching suppliers",
         "cmuzoyi2j00lj06ovdhroor86": "upload_rfq_file"}  # accepts Gerber zips, STL, STEP
PREFERRED = {
    "part": {"cmuds975i00op0agm4ievg1pe": "shopping_search",
             "cmudup4lk00eg0agmi6cusm2t": "Buy a Listing",
             "cmudqk7iu023z0agmkk5b7fiw": "Price shipping"},
    "pcb_fab": QUOTE,
    "print": QUOTE,
    "design_review": {"cmuzr1sh100ib06pbyy3kq62p": "Design Review Orchestrator"},
    # Generic across parts, so looked up once per run (ROLES) rather than per BOM line.
    # part-lookup: live stock + price breaks across DigiKey/Mouser/Farnell/RS/Arrow from an MPN,
    #   $0.04 USDC per call via x402 (a Darwin payment request the user approves).
    "quote": {"cmud0999x004b09gml2r8q5zf": "part-lookup",
              "cmv04e1l1007f06mh94n2o7m0": "Get component marketplace products"},  # ElectroGen kits
    # store_buy hands back a Stripe checkout_url for the human to pay; Quick buy is instant checkout.
    "order": {"cmudvaa8f01dl0agm69qoso01": "store_buy", "cmudseo52018i0agmp89pntne": "Quick buy"},
}
ROLES = [("quote", "look up live distributor stock and price for a manufacturer part number"),  # part-lookup
         ("quote", "get a price quote for electronic components"),  # kits / modules
         ("order", "buy and checkout a product online for me")]


def _cache_path(query: str, root: str = CACHE) -> str:
    return os.path.join(root, hashlib.sha1(query.encode()).hexdigest()[:16] + ".json")


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
    for root in (CACHE, SHIPPED):
        try:
            with open(_cache_path(query, root)) as fh:
                return {**json.load(fh), "cached": True, "error": err}
        except (OSError, ValueError):
            continue
    return {"query": query, "ok": False, "agents": [], "error": err}


def part_query(item: str) -> str:
    # Darwin 500s on longer phrasings ("...; quote unit price, stock and shipping to the US");
    # this short form is the one that searched reliably on 2026-10-11.
    return f"buy {item}, price and shipping"


def is_purchased(row: dict) -> bool:
    item = (row.get("item") or "").lower()
    made = any(w in item for w in ("fdm_print", "sla_print", "laser", "cnc", "printed", "friction tape"))
    return row.get("cost", 0) > 0 and not made and row.get("note") != "existing hardware"


def handoffs(rev_dir: str, results: dict) -> list:
    """Engineering work Morph hands off once the design exists: (kind, query, files)."""
    out = []
    gerbers = os.path.join(rev_dir, "electronics", "board", "gerbers", "gerbers.zip")
    if os.path.exists(gerbers):
        out.append(("pcb_fab", "PCB fabrication quote from Gerber files",
                    [os.path.relpath(gerbers, rev_dir)]))
    fab_dir = os.path.join(rev_dir, "fab")
    stls = sorted(f for f in os.listdir(fab_dir) if f.endswith(".stl")) if os.path.isdir(fab_dir) else []
    if stls:
        mat = ((results.get("fab") or {}).get("material") or "PETG")
        # "3D printing service ..." 500s on Darwin; this wording reaches the RFQ agents, which take STL
        out.append(("print", f"upload STL design file for a 3D printed parts quote, {mat}", [f"fab/{f}" for f in stls]))
    out.append(("design_review", "mechanical design review of a robot gripper", []))
    return out


# -- whole robots (morph.robot.pipeline2): BOM is {"rows": [...]} with make_or_buy per line ------

def short_item(item: str) -> str:
    """Catalogue description -> what a shop search understands ("Feetech STS3215 serial bus servo")."""
    s = re.sub(r"\s*\([^)]*\)", "", item)
    head, _, tail = s.partition(":")
    s = (head if len(head) >= 15 or not tail else f"{head} {tail}").split(";")[0].split(",")[0]
    return re.sub(r"\s+", " ", s.replace("\u00b7", " ").replace("\u00d7", "x")).strip()[:70]


def robot_parts(results: dict) -> list:
    """[(BOM item, query)]: one lookup per purchased line; fasteners and servo cables as one kit each."""
    out, seen, kits = [], set(), {}
    for r in (results.get("bom") or {}).get("rows") or []:
        if r.get("make_or_buy") != "buy" or not r.get("ext_price"):
            continue
        cat = r.get("category")
        if cat in ("fastener", "cable"):
            kits.setdefault(cat, r["item"])
            continue
        q = short_item(r["item"])
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append((r["item"], part_query(q)))
    if "cable" in kits:
        out.append(("Servo cables", part_query(short_item(kits["cable"]))))
    if "fastener" in kits:
        out.append(("Fastener kit (M2-M4)", part_query("metric socket screw assortment kit M2 M2.5 M3 M4")))
    return out


def robot_handoffs(rev_dir: str, results: dict) -> list:
    """Fabrication Morph hands off for a whole robot: the PCB, the prints, CNC and sheet-metal parts."""
    out = []
    gerbers = os.path.join(rev_dir, "electronics", "board", "gerbers", "gerbers.zip")
    if os.path.exists(gerbers):
        out.append(("pcb_fab", "PCB fabrication quote from Gerber files", [os.path.relpath(gerbers, rev_dir)]))
    by_proc = {}
    for g in (results.get("manufacturing") or {}).get("parts") or []:
        files = [f"mfg/{f}" for f in g.get("files") or [] if isinstance(f, str)]
        by_proc.setdefault(g.get("process") or "", []).extend(files)
    mfg = os.path.join(rev_dir, "mfg")
    if not by_proc and os.path.isdir(mfg):
        for root, _, fns in os.walk(mfg):
            by_proc.setdefault("fdm_print", []).extend(os.path.relpath(os.path.join(root, f), rev_dir)
                                                       for f in fns if f.endswith(".stl"))
    prints = [f for k, fs in by_proc.items() if k.endswith("_print") for f in fs if f.endswith((".stl", ".3mf"))]
    if prints:
        out.append(("print", "upload STL design file for a 3D printed parts quote, PETG", prints))
    cnc = [f for k, fs in by_proc.items() if k.startswith("cnc") or k == "laser_cut" for f in fs if f.endswith((".dxf", ".nc", ".stl"))]
    if cnc:
        out.append(("cnc", "CNC machining quote from DXF and STEP files", cnc))
    sheet = [f for f in by_proc.get("sheet_metal", []) if f.endswith(".dxf")]
    if sheet:
        out.append(("sheet_metal", "sheet metal fabrication quote from DXF flat patterns", sheet))
    out.append(("design_review", "mechanical design review of a robot gripper", []))  # "...robot arm" 500s
    return out


def tasks_for(results: dict, rev_dir: str) -> list:
    """[(kind, label, query, files)] for one revision: roles, purchased parts, handoffs."""
    tasks = [(kind, kind, q, []) for kind, q in ROLES]
    if isinstance(results.get("bom"), dict):  # whole robot
        tasks += [("part", item, q, []) for item, q in robot_parts(results)]
        tasks += [(kind, kind, q, files) for kind, q, files in robot_handoffs(rev_dir, results)]
    else:
        tasks += [("part", r["item"], part_query(r["item"]), []) for r in results.get("bom") or [] if is_purchased(r)]
        tasks += [(kind, kind, q, files) for kind, q, files in handoffs(rev_dir, results)]
    return tasks


def source(project, rev: str, max_results: int = 3, workers: int = 4, on_progress=None, timeout: float | None = None) -> dict:
    """Shortlist Darwin agents for every purchased part and handoff of `rev`; log each lookup.

    on_progress(done, total, task) is called as each search returns (for the Studio's live view).
    """
    results = project.load_results(rev)
    rev_dir = project.rev_dir(rev)
    tasks = tasks_for(results, rev_dir)

    found = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        kw = {} if timeout is None else {"timeout": timeout}  # None: search()'s own default
        futs = {pool.submit(search, t[2], max_results, **kw): i for i, t in enumerate(tasks)}
        for n, fut in enumerate(as_completed(futs), 1):
            i = futs[fut]
            found[i] = fut.result()
            if on_progress:
                on_progress(n, len(tasks), {"kind": tasks[i][0], "item": tasks[i][1], "found": len(found[i]["agents"]),
                                            "cached": found[i].get("cached", False)})

    rows, gaps = [], []
    for (kind, label, query, files), res in zip(tasks, found):
        want = PREFERRED.get(kind, {})
        agents = sorted(res["agents"], key=lambda a: a["agent_id"] not in want)  # stable: picks first
        picked = [a for a in agents if a["agent_id"] in want]
        gap = None
        if not res["ok"]:
            gap = "darwin_unavailable"
        elif not agents:
            gap = "no_agent"
        # Anonymous Search skips Darwin's readiness check, so "unavailable" there means
        # unverified, not missing: found, but startable only after the user signs in to Act.
        verified = any(a["can_start"] for a in agents)
        if gap:
            gaps.append({"kind": kind, "item": label, "gap": gap, "reason": res.get("no_match_reason")})
        rows.append({"kind": kind, "item": label, "query": query, "files": files, "agents": agents,
                     "readiness_verified": verified, "picked": picked,
                     "cached": res.get("cached", False), "error": res.get("error"), "gap": gap})
        project.log({"type": "sourcing", "rev": rev, "kind": kind, "part": label, "found": len(agents),
                     "top_agent": agents[0]["name"] if agents else None, "gap": gap})
        # top_agent is a picked agent whenever one was found, since picks sort first

    report = {"rev": rev, "source": "darwin search", "rows": rows, "gaps": gaps,
              "note": "Shortlist only. Ordering needs the user's Darwin account (Act) and their approval."}
    with open(os.path.join(rev_dir, "sourcing_darwin.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    return report
