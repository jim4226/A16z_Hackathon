"""Static HTML report: compare any set of revisions side by side."""

from __future__ import annotations

import html
import json
import os

from .pipeline import Project

COLORS = ["#2f6fdf", "#e07a1f", "#2a9d6f", "#b5479b", "#8a6d3b"]

METRICS = [
    # key, label, unit, better ("lo"/"hi"/None)
    ("feasible", "Passes all checks", "", "hi"),
    ("sim_success_rate", "Sim grasp success (randomized)", "", "hi"),
    ("min_hold_force_n", "Min grip force to hold (sim)", "N", "lo"),
    ("required_grip_n", "Required grip force (closed-form, with SF)", "N", "lo"),
    ("mean_slip_mm", "Mean slip after shake", "mm", "lo"),
    ("tip_deflection_mm", "Finger tip deflection", "mm", "lo"),
    ("bending_sf", "Bending safety factor", "", "hi"),
    ("finger_mass_g", "Finger mass", "g", "lo"),
    ("fab_process", "Process", "", None),
    ("fab_minutes", "Fabrication time (2 fingers)", "min", "lo"),
    ("fab_cost", "Fabrication cost", "$", "lo"),
    ("dfm_warnings", "DFM warnings", "", "lo"),
]

CSS = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e3e1da;--good:#1f7a4d;--goodbg:#e3f3ea;--bad:#b42318;--badbg:#fdecea;--chip:#efede6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141416;--card:#1d1d20;--ink:#ededf0;--muted:#a0a0a8;--line:#2e2e33;--good:#5fd39a;--goodbg:#173a28;--bad:#ff8a80;--badbg:#3a1b1b;--chip:#2a2a2f}}
:root[data-theme="dark"]{--bg:#141416;--card:#1d1d20;--ink:#ededf0;--muted:#a0a0a8;--line:#2e2e33;--good:#5fd39a;--goodbg:#173a28;--bad:#ff8a80;--badbg:#3a1b1b;--chip:#2a2a2f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}
.sub{color:var(--muted);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:0 0 16px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-weight:600;font-size:13px;color:var(--muted)}td.best{background:var(--goodbg);color:var(--good);font-weight:600}td.fail{background:var(--badbg);color:var(--bad)}
.chip{display:inline-block;padding:2px 8px;border-radius:99px;background:var(--chip);font-size:12px;margin:2px 4px 2px 0}.chip.hot{background:var(--badbg);color:var(--bad)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:middle}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}svg{max-width:100%;height:auto}
a{color:inherit}code{font-size:13px}.warn{color:var(--bad)}details summary{cursor:pointer;color:var(--muted)}
"""


def _fmt(v, unit=""):
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        if v == float("inf"):
            return "fails"
        v = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{unit}{v}" if unit == "$" else f"{v} {unit}".strip()


def _overlay_svg(project: Project, revs) -> str:
    shapes, xs, ys = [], [], []
    for rev in revs:
        rings = project.load_results(rev)["cad"]["profile"]
        shapes.append(rings)
        for r in rings:
            xs += [p[0] for p in r]
            ys += [p[1] for p in r]
    d0 = project.load_design(revs[0])
    R = d0.object.diameter_mm / 2
    c0 = project.load_results(revs[0])["cad"]
    # object cross-section where it sits when gripped (in rev 0's frame)
    f = d0.finger
    cu = f.length_mm - f.pad_height_mm / 2
    cv = (f.thickness_mm + R) if f.pad != "flat" else (f.thickness_mm + f.pad_depth_mm + R)
    xs += [cu - R, cu + R]
    ys += [cv - R, cv + R]
    pad = 6
    minx, maxx, miny, maxy = min(xs) - pad, max(xs) + pad, min(ys) - pad, max(ys) + pad
    # drawn hanging down: x = across the finger (v), y = along it (u)
    out = [f'<svg viewBox="{miny:.1f} {minx:.1f} {maxy-miny:.1f} {maxx-minx:.1f}" height="{min((maxx-minx)*4, 420):.0f}" '
           f'xmlns="http://www.w3.org/2000/svg">',
           f'<circle cx="{cv:.2f}" cy="{cu:.2f}" r="{R:.2f}" fill="none" stroke="currentColor" '
           f'stroke-opacity=".35" stroke-dasharray="2 1.5" stroke-width=".5"/>']
    for i, rings in enumerate(shapes):
        d = " ".join("M" + " L".join(f"{v:.2f},{u:.2f}" for u, v in r) + " Z" for r in rings)
        col = COLORS[i % len(COLORS)]
        out.append(f'<path d="{d}" fill="{col}" fill-opacity=".18" fill-rule="evenodd" stroke="{col}" stroke-width=".6"/>')
    out.append("</svg>")
    del c0
    return "".join(out)


def build(project: Project, revs, path: str, title: str = "") -> str:
    res = {r: project.load_results(r) for r in revs}
    des = {r: project.load_design(r) for r in revs}
    meta_path = os.path.join(project.root, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
    head = "".join(f'<th><span class="dot" style="background:{COLORS[i % len(COLORS)]}"></span>{r}'
                   f'<br><span style="font-weight:400">{html.escape(res[r].get("label") or "")}</span></th>'
                   for i, r in enumerate(revs))

    rows = []
    for key, label, unit, better in METRICS:
        vals = [res[r]["summary"].get(key) for r in revs]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool) and v != float("inf")]
        best = None
        if better and len(revs) > 1 and nums and key != "feasible":
            best = (min if better == "lo" else max)(nums)
            if nums.count(best) == len(nums):
                best = None
        cells = []
        for v in vals:
            cls = ""
            if key == "feasible":
                cls = "best" if v else "fail"
            elif best is not None and v == best:
                cls = "best"
            cells.append(f'<td class="{cls}">{html.escape(_fmt(v, unit))}</td>')
        rows.append(f"<tr><th>{label}</th>{''.join(cells)}</tr>")

    params = ["finger.pad", "finger.thickness_mm", "finger.width_mm", "finger.length_mm",
              "finger.pad_depth_mm", "finger.material", "fab.process", "gripper.force_n", "object.mass_g"]
    prow = []
    for p in params:
        vals = [des[r].flat()[p] for r in revs]
        diffcls = ' class="best"' if len(set(map(str, vals))) > 1 else ""
        prow.append(f"<tr><th><code>{p}</code></th>" + "".join(f"<td{diffcls}>{html.escape(str(v))}</td>" for v in vals) + "</tr>")

    prop = []
    for r in revs:
        rr = res[r]
        if not rr.get("parent"):
            continue
        ch = ", ".join(f"<code>{html.escape(k)}</code>: {html.escape(str(a))} → {html.escape(str(b))}"
                       for k, (a, b) in rr["changes"].items()) or "no parameter changes"
        chips = "".join(f'<span class="chip{" hot" if v == "computed" else ""}">{n}: {html.escape(v)}</span>'
                        for n, v in rr["provenance"].items())
        prop.append(f"<p><b>{r}</b> from {rr['parent']}: {ch}<br>{chips}</p>")

    fabcards = []
    for i, r in enumerate(revs):
        fp = res[r]["fab"]
        files = " ".join(f'<a href="{r}/fab/{f}">{f}</a>' for f in fp["files"])
        warn = "".join(f'<div class="warn">⚠ {html.escape(w)}</div>' for w in fp["dfm_warnings"])
        issues = "".join(f'<div class="warn">✗ {html.escape(w)}</div>' for w in res[r]["summary"]["issues"])
        img = f'<img src="{r}/fab/toolpath.svg" style="width:100%;margin-top:8px" alt="toolpath">' \
            if "toolpath.svg" in fp["files"] else ""
        fabcards.append(f'<div class="card" style="margin:0"><h2><span class="dot" style="background:{COLORS[i % len(COLORS)]}"></span>{r} · {fp["process"]}</h2>'
                        f'<div>{_fmt(fp["minutes"], "min")} · ${fp["total_cost"]}</div><div>{files} · <a href="{r}/cad/finger.stl">cad/finger.stl</a> · '
                        f'<a href="{r}/sim/scene.xml">scene.xml</a></div>{warn}{issues}{img}</div>')

    meas = [e for e in project.entries() if e["type"] == "measurement" and e["rev"] in revs]
    mrows = "".join(f"<tr><td>{e['rev']}</td><td>{html.escape(json.dumps(e['actual']))}</td>"
                    f"<td>{html.escape(json.dumps(e['error_vs_prediction']))}</td></tr>" for e in meas)

    trials = []
    for r in revs:
        t = res[r]["sim"]["trials"]
        trials.append(f"<p><b>{r}</b>: " + " ".join(
            f'<span class="chip{"" if x["success"] else " hot"}">{"✓" if x["success"] else "✗"} slip {x["final_slip_mm"]} mm '
            f'μ×{x["trial"]["friction_scale"]:.2f} m×{x["trial"]["mass_scale"]:.2f}</span>' for x in t) + "</p>")

    gifs = "".join(f'<figure style="margin:0"><img src="{r}/sim/grasp.gif" style="width:100%;border-radius:8px" alt="grasp simulation {r}">'
                   f'<figcaption class="sub" style="margin:4px 0 0">{r}: close, lift 80 mm, shake</figcaption></figure>'
                   for r in revs if os.path.exists(os.path.join(project.rev_dir(r), "sim", "grasp.gif")))
    title = title or f"Morph · {des[revs[0]].name}"
    need = f'<p class="sub">Need: “{html.escape(meta["need"])}”</p>' if meta.get("need") else ""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>{need}
<p class="sub">Object: {html.escape(des[revs[0]].object.name)} · Ø{des[revs[0]].object.diameter_mm} mm · {des[revs[0]].object.mass_g} g · μ≈{des[revs[0]].object.friction}</p>
<div class="grid" style="margin-bottom:16px"><div class="card" style="margin:0"><h2>Finger profiles (dashed: object as gripped)</h2>{_overlay_svg(project, revs)}</div>
{'<div class="card" style="margin:0"><h2>Simulation</h2>' + gifs + '</div>' if gifs else ''}</div>
<div class="card"><h2>Predicted performance</h2><table><tr><th></th>{head}</tr>{''.join(rows)}</table></div>
<div class="card"><h2>Design parameters</h2><table><tr><th></th>{head}</tr>{''.join(prow)}</table></div>
{'<div class="card"><h2>Change propagation</h2>' + ''.join(prop) + '</div>' if prop else ''}
<h2 style="margin:20px 0 12px">Fabrication</h2><div class="grid">{''.join(fabcards)}</div>
{'<div class="card" style="margin-top:16px"><h2>Measured vs predicted</h2><table><tr><th>rev</th><th>actual</th><th>error vs prediction</th></tr>' + mrows + '</table></div>' if mrows else ''}
<div class="card" style="margin-top:16px"><details><summary>Simulation trials (randomized offset, friction, mass; lift 80 mm + 0.5 g shake)</summary>{''.join(trials)}</details></div>
</main></body></html>"""
    with open(path, "w") as fh:
        fh.write(doc)
    return path
