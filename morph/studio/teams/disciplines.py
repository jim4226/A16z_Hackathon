"""Stage owners by discipline: Morph's CODEOWNERS.

Each pipeline stage (morph.robot.pipeline2.ORDER) belongs to one discipline. A change's affected stages are the
pipeline's own dirty set (pipeline2.dirty_nodes), so review routing follows exactly what the change recomputes.
Members carry discipline tags; `generalist` (a founder or solo builder) owns no stage exclusively but is the
fallback owner of every discipline that has no specialist on the team, and their approval counts for any
discipline unless the policy's `specialist_required` lists it for that build level.
"""

from __future__ import annotations

from ...robot.pipeline2 import GRAPH, LABELS as STAGE_LABELS, ORDER, dirty_nodes

DISCIPLINES = ("mechanical", "simulation", "electronics", "manufacturing", "sourcing", "documentation")
GENERALIST = "generalist"
TAGS = DISCIPLINES + (GENERALIST,)
LABELS = {"mechanical": "Mechanical", "simulation": "Simulation", "electronics": "Electronics", "manufacturing": "Manufacturing",
          "sourcing": "Sourcing", "documentation": "Documentation", GENERALIST: "Generalist"}
STAGES = {"mechanical": ("sizing", "gripper", "cad"), "simulation": ("sim",), "electronics": ("electronics", "wiring"),
          "manufacturing": ("manufacturing",), "sourcing": ("bom",), "documentation": ("manual",)}
STAGE_DISCIPLINE = {s: d for d, ss in STAGES.items() for s in ss}
STAGE_TITLES = dict(STAGE_LABELS)
LEVELS = ("prototype", "pilot", "production")
RANK = {"viewer": 1, "editor": 2, "owner": 3}

assert set(STAGE_DISCIPLINE) == set(ORDER), "every pipeline stage needs an owning discipline"


def validate(tags) -> list:
    """Discipline tags in canonical order. Raises ValueError for anything that isn't a known tag."""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [t for t in tags.replace(" ", "").split(",") if t]
    if not isinstance(tags, (list, tuple, set)):
        raise ValueError("Send disciplines as a list, e.g. [\"electronics\"].")
    bad = [t for t in tags if t not in TAGS]
    if bad:
        raise ValueError(f"Unknown discipline {str(bad[0])[:40]!r}: use " + ", ".join(TAGS) + ".")
    return [t for t in TAGS if t in tags]


def _keys(changes) -> list:
    if not changes:
        return []
    if isinstance(changes, dict):
        return [str(k) for k in changes]
    return [str(k) for k in changes]


def downstream(stages) -> list:
    """The stages plus everything downstream of them in the pipeline graph."""
    out = set(s for s in stages if s in GRAPH)
    grew = True
    while grew:
        grew = False
        for node, (_, ups, _) in GRAPH.items():
            if node not in out and any(u in out for u in ups):
                out.add(node)
                grew = True
    return [n for n in ORDER if n in out]


def affected_stages(changes=None, stages=()) -> list:
    """Stages a change recomputes: pipeline2.dirty_nodes(design keys), plus stages named directly (a change to the
    manual's wording, a file) with everything downstream of them. A key that is a stage name targets that stage."""
    keys = _keys(changes)
    direct = [k for k in keys if k in GRAPH] + [s for s in (stages or ()) if s in GRAPH]
    design_keys = [k for k in keys if k not in GRAPH]
    out = set(dirty_nodes(design_keys)) | set(downstream(direct))
    return [n for n in ORDER if n in out]


def disciplines_for_stages(stages) -> list:
    owned = {STAGE_DISCIPLINE[s] for s in stages if s in STAGE_DISCIPLINE}
    return [d for d in DISCIPLINES if d in owned]


def affected_disciplines(changes=None, stages=()) -> list:
    """Disciplines owning any stage the change recomputes, in canonical order."""
    return disciplines_for_stages(affected_stages(changes, stages))


def _active(members) -> list:
    if isinstance(members, dict):
        members = list(members.values())
    return [m for m in (members or []) if isinstance(m, dict) and not m.get("revoked")]


def specialists(members, discipline: str) -> list:
    return [m for m in _active(members) if discipline in (m.get("disciplines") or [])]


def generalists(members) -> list:
    return [m for m in _active(members) if GENERALIST in (m.get("disciplines") or [])]


def owners(members) -> list:
    return [m for m in _active(members) if m.get("role") == "owner"]


def watching(member: dict) -> bool:
    """'Watch all changes': on by default for generalists, off for specialists, overridable per member."""
    w = member.get("watch")
    return bool(w) if isinstance(w, bool) else GENERALIST in (member.get("disciplines") or [])


def _required(policy, level) -> set:
    req = (policy or {}).get("specialist_required") or {}
    if isinstance(req, (list, tuple)):  # a flat list applies above prototype
        return set(req) if level in ("pilot", "production") else set()
    if isinstance(req, dict):
        return set(req.get(level) or [])
    return set()


def _ids(authors) -> set:
    if authors is None:
        return set()
    if isinstance(authors, str):
        return {authors}
    return {a for a in authors if a}


def eligible(member: dict, discipline: str, members, policy=None, level: str = "prototype", authors=()) -> str | None:
    """How `member`'s approval counts for `discipline`: "specialist", "generalist", "owner" (the fallback when the
    team has no specialist and no generalist other than the authors) or None. Viewers count only when the policy
    lets viewers approve. Whether an author's approval counts at all is the gate's call (prototype: it does)."""
    if not member or member.get("revoked"):
        return None
    if RANK.get(member.get("role"), 0) < RANK["editor"] and not (policy or {}).get("viewers_can_approve"):
        return None
    tags = member.get("disciplines") or []
    if discipline in tags:
        return "specialist"
    if discipline in _required(policy, level):
        return None
    if GENERALIST in tags:
        return "generalist"
    others = _ids(authors)
    if member.get("role") == "owner" and not [m for m in specialists(members, discipline) if m.get("id") not in others] \
            and not [m for m in generalists(members) if m.get("id") not in others]:
        return "owner"
    return None


def covers(member: dict, disciplines, members, policy=None, level: str = "prototype", authors=()) -> list:
    """The disciplines (of `disciplines`) this member's approval covers, for the approve button's label."""
    return [d for d in disciplines if eligible(member, d, members, policy, level, authors)]


def approve_label(disciplines) -> str:
    names = [LABELS.get(d, d) for d in disciplines]
    if not names:
        return "Approve"
    return "Approve as " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def _pub(m: dict, via: str) -> dict:
    return {"id": m.get("id"), "name": m.get("name"), "color": m.get("color"), "via": via}


def route_discipline(discipline: str, author, members) -> dict:
    """Who is asked to review one discipline: its specialists other than the author; else the generalists (the
    fallback owners, also when the only specialist is the author); else the team owners."""
    authors = _ids(author)
    spec = [m for m in specialists(members, discipline) if m.get("id") not in authors]
    if spec:
        return {"discipline": discipline, "label": LABELS[discipline], "via": "specialist", "members": [_pub(m, "specialist") for m in spec]}
    gen = [m for m in generalists(members) if m.get("id") not in authors]
    if gen:
        return {"discipline": discipline, "label": LABELS[discipline], "via": "generalist", "members": [_pub(m, "generalist") for m in gen]}
    own = [m for m in owners(members) if m.get("id") not in authors]
    if own:
        return {"discipline": discipline, "label": LABELS[discipline], "via": "owner", "members": [_pub(m, "owner") for m in own]}
    return {"discipline": discipline, "label": LABELS[discipline], "via": "none", "members": []}


def chip_text(route: dict) -> str:
    """'Electronics: Ana' / 'Manufacturing: Jaron (generalist)' / 'Documentation: nobody yet'."""
    if not route["members"]:
        return f"{route['label']}: nobody yet"
    names = ", ".join(m["name"] or "?" for m in route["members"])
    return f"{route['label']}: {names}" + (f" ({route['via']})" if route["via"] in ("generalist", "owner") else "")


def routing(changes=None, author=None, members=None, stages=(), disciplines=None) -> dict:
    """The review routing for a change: affected stages and disciplines, who is asked per discipline (never the
    author), the watchers who get it in their inbox without being required, and the chip text."""
    st = affected_stages(changes, stages)
    ds = list(disciplines) if disciplines is not None else disciplines_for_stages(st)
    routes = [route_discipline(d, author, members) for d in ds]
    authors = _ids(author)
    asked = {m["id"] for r in routes for m in r["members"]}
    watchers = [m.get("id") for m in _active(members) if watching(m) and m.get("id") not in asked and m.get("id") not in authors]
    return {"stages": st, "stage_titles": [STAGE_TITLES[s] for s in st], "disciplines": ds, "routes": routes,
            "reviewers": sorted(asked), "watchers": watchers, "text": " · ".join(chip_text(r) for r in routes)}


def reviewers_for(changes=None, author=None, members=None, stages=()) -> list:
    """Member ids whose review a change requests (owners of the affected disciplines, excluding the author)."""
    return routing(changes, author, members, stages)["reviewers"]
