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
    ("target_force_n", "Grip force target (firmware)", "N", None),
    ("servo", "Servo", "", None),
    ("servo_stall_force_n", "Servo force at stall, open loop", "N", None),
    ("open_loop_crushes", "Open-loop servo would damage object", "", None),
    ("bom_cost", "Total BOM (fab + electronics)", "$", "lo"),
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
    from .timestudy import study

    explored = [e["recommended"] for e in project.entries() if e["type"] == "exploration"]
    fab_rev = explored[-1] if explored and explored[-1] in revs else revs[-1]
    ts = study(project, fab_rev)
    # a multiplier without the human minutes would overstate the claim
    speed = f" · {ts['speedup']}×" if ts["speedup"] and ts["human_logged"] else ""
    trows = "".join(f"<tr><th>{html.escape(r['step'])}</th><td>{_fmt(r['before_min'], 'min')}</td>"
                    f"<td>{_fmt(r['after_min'], 'min')}</td></tr>" for r in ts["rows"])
    caveat = ("Before = estimates for a CAD-fluent engineer doing this grasp test by hand"
              + (" (overridden by baseline.json)" if ts["baseline_overridden"] else "; time one step yourself and put it in baseline.json")
              + f". After = measured: Morph command wall-clock ({ts['tool_minutes']} min), machine time "
              + ("measured" if ts["fab_measured"] else "predicted")
              + ", and human minutes logged with <code>morph clock</code>"
              + (f" ({', '.join(ts['human_logged'])})." if ts["human_logged"] else " (none logged yet, so the comparison is incomplete)."))
    timecard = (f'<div class="card"><h2>Time: before vs after ({ts["variants_evaluated"]} design variants fully evaluated; fabricating {fab_rev})</h2>'
                f'<div class="grid" style="margin-bottom:12px"><div><div class="sub" style="margin:0">by hand</div><div style="font-size:28px;font-weight:700">{_fmt(ts["before_min"], "min")}</div></div>'
                f'<div><div class="sub" style="margin:0">with Morph</div><div style="font-size:28px;font-weight:700">{_fmt(ts["after_min"], "min")}</div></div>'
                f'<div><div class="sub" style="margin:0">saved</div><div style="font-size:28px;font-weight:700;color:var(--good)">{_fmt(ts["saved_min"], "min")}'
                f'{speed}</div></div></div>'
                f'<table><tr><th>step</th><th>by hand</th><th>with Morph</th></tr>{trows}</table>'
                f'<p class="sub" style="margin:10px 0 0">{caveat}</p></div>')
    ecards = []
    for i, r in enumerate(revs):
        ep = res[r].get("electronics")
        if not ep or ep.get("sensor") == "none":
            continue
        ecards.append(_electronics_card(project, r, ep, COLORS[i % len(COLORS)]))
    title = title or f"Morph · {des[revs[0]].name}"
    need = f'<p class="sub">Need: “{html.escape(meta["need"])}”</p>' if meta.get("need") else ""
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<h1>{html.escape(title)}</h1>{need}
<p class="sub">Object: {html.escape(des[revs[0]].object.name)} · Ø{des[revs[0]].object.diameter_mm} mm · {des[revs[0]].object.mass_g} g · μ≈{des[revs[0]].object.friction}</p>
{timecard}
<div class="grid" style="margin-bottom:16px"><div class="card" style="margin:0"><h2>Finger profiles (dashed: object as gripped)</h2>{_overlay_svg(project, revs)}</div>
{'<div class="card" style="margin:0"><h2>Simulation</h2>' + gifs + '</div>' if gifs else ''}</div>
<div class="card"><h2>Predicted performance</h2><table><tr><th></th>{head}</tr>{''.join(rows)}</table></div>
<div class="card"><h2>Design parameters</h2><table><tr><th></th>{head}</tr>{''.join(prow)}</table></div>
{'<div class="card"><h2>Change propagation</h2>' + ''.join(prop) + '</div>' if prop else ''}
{'<h2 style="margin:20px 0 12px">Electronics + live bench</h2>' + ''.join(ecards) if ecards else ''}
<h2 style="margin:20px 0 12px">Fabrication</h2><div class="grid">{''.join(fabcards)}</div>
{'<div class="card" style="margin-top:16px"><h2>Measured vs predicted</h2><table><tr><th>rev</th><th>actual</th><th>error vs prediction</th></tr>' + mrows + '</table></div>' if mrows else ''}
<div class="card" style="margin-top:16px"><details><summary>Simulation trials (randomized offset, friction, mass; lift 80 mm + 0.5 g shake)</summary>{''.join(trials)}</details></div>
</main></body></html>"""
    with open(path, "w") as fh:
        fh.write(doc)
    return path


def _window_svg(ep: dict) -> str:
    lo, hi = ep["window_n"]
    stall, target = ep["servo_stall_force_n"], ep["target_force_n"]
    lim = ep.get("object_limit_n")
    top = max(stall, lim or 0, target * 3) * 1.05
    W, H = 640, 64
    x = lambda f: 8 + (W - 16) * min(f, top) / top  # noqa: E731
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-size="11" fill="currentColor">',
             f'<rect x="8" y="18" width="{W-16}" height="14" rx="3" fill="currentColor" fill-opacity=".08"/>',
             f'<rect x="{x(0):.1f}" y="18" width="{x(lo)-x(0):.1f}" height="14" fill="#b42318" fill-opacity=".35"/>']
    if hi:
        parts.append(f'<rect x="{x(lo):.1f}" y="18" width="{x(hi)-x(lo):.1f}" height="14" fill="#1f7a4d" fill-opacity=".45"/>')
    if lim:
        parts.append(f'<rect x="{x(lim):.1f}" y="18" width="{x(top)-x(lim):.1f}" height="14" fill="#b42318" fill-opacity=".55"/>')
    for f, label, dy in ((target, f"target {target} N", 12), (stall, f"servo stall {stall} N", 50)) + (((lim, f"damage {lim} N", 50),) if lim else ()):
        parts.append(f'<line x1="{x(f):.1f}" x2="{x(f):.1f}" y1="14" y2="36" stroke="currentColor" stroke-width="1.5"/>'
                     f'<text x="{min(x(f), W-90):.1f}" y="{dy}">{label}</text>')
    parts.append(f'<text x="8" y="50">slips &lt; {lo} N</text></svg>')
    return "".join(parts)


def _trace_svg(project, rev: str, ep: dict) -> str:
    import csv

    bdir = os.path.join(project.rev_dir(rev), "bench")
    if not os.path.isdir(bdir):
        return ""
    runs = sorted(f for f in os.listdir(bdir) if f.endswith(".csv"))
    if not runs:
        return ""
    rows = list(csv.DictReader(open(os.path.join(bdir, runs[-1]))))
    if not rows:
        return ""
    ts = [float(r["t_s"]) for r in rows]
    fs = [float(r["force_n"]) for r in rows]
    W, H, pad = 640, 180, 28
    tmax = max(ts) or 1.0
    fmax = max(max(fs), ep["target_force_n"] * 1.6, 0.5)
    X = lambda t: pad + (W - pad - 8) * t / tmax  # noqa: E731
    Y = lambda f: H - pad - (H - pad - 8) * f / fmax  # noqa: E731
    pts = " ".join(f"{X(t):.1f},{Y(f):.1f}" for t, f in zip(ts, fs))
    tgt = ep["target_force_n"]
    sim = "SIMULATED" in runs[-1]
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" font-size="11" fill="currentColor">'
            f'<line x1="{pad}" x2="{W-8}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="currentColor" stroke-opacity=".3"/>'
            f'<line x1="{pad}" x2="{W-8}" y1="{Y(tgt):.1f}" y2="{Y(tgt):.1f}" stroke="#1f7a4d" stroke-dasharray="4 3"/>'
            f'<text x="{W-120}" y="{Y(tgt)-4:.1f}" fill="#1f7a4d">predicted target {tgt} N</text>'
            f'<polyline points="{pts}" fill="none" stroke="#2f6fdf" stroke-width="1.8"/>'
            f'<text x="{pad}" y="14">{html.escape(runs[-1])} · force (N) vs time (s, 0–{tmax:.1f})'
            f'{" · SIMULATED rehearsal trace, not hardware" if sim else " · measured on hardware"}</text></svg>')


def _electronics_card(project, rev: str, ep: dict, color: str) -> str:
    wiring = "".join(f"<tr><td>{html.escape(a)}</td><td>{html.escape(b)}</td></tr>" for a, b in ep["wiring"])
    parts = "".join(f"<tr><td>{html.escape(p['item'])}</td><td>{p['qty']}</td><td>${p['cost']:.2f}</td></tr>" for p in ep["parts"])
    sens = ep.get("sensing") or {}
    notes = "".join(f'<div class="warn">✗ {html.escape(w)}</div>' for w in ep["issues"])
    notes += "".join(f'<div class="sub" style="margin:4px 0">• {html.escape(w)}</div>' for w in ep["warnings"])
    crush = (f'<p><b>Open-loop {ep["servo"]} squeezes up to {ep["servo_stall_force_n"]} N</b> — above the '
             f'{ep["object_limit_n"]} N damage limit. The generated firmware closes on the force sensor instead and '
             f'stops at {ep["target_force_n"]} N (hard cutoff {ep["limit_n"]} N).</p>') if ep.get("open_loop_crushes") else ""
    fw = ep.get("firmware")
    return (f'<div class="card"><h2><span class="dot" style="background:{color}"></span>{rev} · {html.escape(ep["mcu"])} + '
            f'{html.escape(ep["servo"])} + {html.escape(ep["sensor"])}</h2>{crush}'
            f'<div class="sub" style="margin:0 0 4px">Grip-force window (green = holds without damage)</div>{_window_svg(ep)}'
            f'{_trace_svg(project, rev, ep)}'
            f'<div class="grid" style="margin-top:8px"><div><h2>Wiring</h2><table>{wiring}</table>'
            f'<p class="sub">Divider {sens.get("divider_ohm", "–")} Ω · {sens.get("v_at_target", "–")} V at target · '
            f'~{sens.get("resolution_n_per_lsb", "–")} N per ADC step</p></div>'
            f'<div><h2>Electronics BOM</h2><table>{parts}</table>'
            f'{f"<p><a href={chr(34)}{rev}/{fw}{chr(34)}>morph_grip.ino</a> (generated firmware)</p>" if fw else ""}</div></div>'
            f'{notes}</div>')
