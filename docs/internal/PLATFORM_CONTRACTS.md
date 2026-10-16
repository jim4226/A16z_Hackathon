# Morph robot platform: module contracts

Morph generates a **whole robot** (arm + sensorized gripper) and lets you edit every
layer (CAD, electronics, simulation, manufacturing, sourcing, assembly) in one place.
This file is the contract between modules that are built in parallel. If you own a
module, implement exactly these signatures and output schemas. Never rename keys
that other modules read. Don't edit files you don't own; if a contract is missing
something you need, add it inside your own module and note it in your final report.

Python env: the launchers' `.venv` with the developer packages (INSTALL.md, For developers: mujoco, trimesh,
manifold3d, shapely, numpy, pytest; playwright for the browser tests). Run from the repo root with `PYTHONPATH=.`.
Units are **mm, rad, kg, N·m**. The robot base frame is **z-up**, x forward, and the origin is on the
table under the base yaw axis.

## Shared, already written (read, don't rewrite)

| file | what |
|---|---|
| `morph/spec.py` | `Design` dataclass: `object`, `finger`, `gripper`, `electronics` (+ `config` modules/custom_pcb, `extras`), `arm` (dof, reach_mm, base_height_mm, actuator_family hobby_pwm/smart_bus/dynamixel, actuator_overrides, payload_margin), `task` (pick/place xy, cycle time), `build` (config prototype/pilot/production, quantity), `fab`. `with_changes({"arm.dof": 6})`, `flat()` |
| `morph/robot/catalog.py` | purchased-part keys + specs + vendors: `ACTUATORS`, `FAMILY_ACTUATORS`, `MODULES`, `BEARINGS`, `FASTENER_SIZES`, `FASTENER_TORQUE_NM`, `get(key)`, `price(key)`, `V(...)`, `search(...)` |
| `morph/robot/assembly.py` | `Assembly`, `Part`, `Link`, `Joint`, `Cable`, `Step`; `world_transforms(q)`, `chain(tip)`, `link_mass`, `export_glb`, `to_robot_json`, `export_urdf`, `validate()`; helpers `rot`, `trans`, `rpy` |
| `morph/design.py`, `morph/mechanism.py` | finger profiles (`build_profile`, `to_mesh`, FSR dovetail pad) and the rack-and-pinion gripper planner (`plan`, `pinion_profile`, `rack_teeth`, `object_center_v`, `SERVOS`) |
| `morph/board.py` | existing 2-layer through-hole board generator (placement, maze router, pour, DRC, Gerbers) for the gripper carrier board; JSON format used by the Studio's PCB animation |
| `tools/snapshot.py` | `python tools/snapshot.py robot.glb out.png --views="35,20;-60,15"` renders a GLB headlessly (PBR + edges), for looking at your geometry |
| `morph/studio/static/fixtures/robot/robot.{glb,json}` | stub robot built by the stub generator, for front-end development |

## Kinematic contract (arm generator → sim, viewer, manual)

```
links:  base -> turret -> upper_arm -> forearm -> wrist [-> wrist_yaw] -> gripper_base
        gripper_base -> jaw_left, jaw_right, pinion, tcp
joints (zero pose = arm straight up, fingers pointing +z of gripper_base):
  j1_base_yaw    revolute +z | j2_shoulder +y | j3_elbow +y | j4_wrist_pitch +y
  j5_wrist_yaw   (dof 6 only) | j5_wrist_roll (dof 5) / j6_wrist_roll (dof 6) +z | dof 4 -> fixed "wrist_fixed"
  gripper        prismatic, gripper_base->jaw_left, axis -x, value = mm opened beyond contact with the design object
  gripper_right  prismatic mimic(gripper, 1) | gripper_pinion revolute mimic(gripper, 1/pitch_radius)
  tcp_fixed      fixed; tcp origin = object centre when grasped; tcp +z = approach direction
assembly.meta = {"arm_joints": [...], "gripper_joint": "gripper", "tcp_link": "tcp",
                 "home": {joint: value}, "ready": {joint: value}, "sizing": {...}, ...}
```
Every part mesh is in its link's frame. Joint `origin` maps parent→child at q=0.
`Part.kind` ∈ structural, actuator, horn, bearing, fastener, electronics, pcb, sensor, pad, cable, cover, visual.
`Part.process` ∈ fdm_print, sla_print, mjf_print, cnc_mill, cnc_turn, laser_cut, sheet_metal, injection_mold,
pcb_assembly, cable_assembly, purchased. Flat parts (plates, sheet-metal flat patterns) set
`profile` (shapely Polygon in mm) + `profile_frame` (4x4: profile u,v,w→link frame) + `thickness`.
Sheet-metal parts also set `meta["sheet"] = {"thickness", "bends": [{"line": [[u0,v0],[u1,v1]], "angle_deg", "radius"}], "k_factor"}`
with `profile` = the FLAT PATTERN. `group` = identical parts made once with qty n. `step` = assembly step index
(must exist in `asm.steps`). `explode` = exploded-view offset (mm, link frame).

## Pipeline order and function signatures

```
sizing      = arm.size(design)                                 # morph/robot/arm.py
eplan       = electronics2.plan(design, sizing)                # morph/robot/electronics2.py
asm         = arm.build(design, sizing, eplan)                 # places eplan["modules"] in the base bay, routes cables
sims        = simulate.run_all(asm, design, outdir, scenarios=None, fast=False)   # morph/robot/simulate.py
efinal      = electronics2.finalize(design, sizing, eplan, asm, outdir)
mfg         = manufacturing.plan(design, asm, outdir, efinal)  # morph/robot/manufacturing.py
manual      = manual.build(design, asm, mfg, efinal, outdir)   # morph/robot/manual.py
bom         = manufacturing.bom(design, asm, mfg, efinal)      # merged BOM + sourcing rows
```

### `arm.size(design) -> dict`
`{"links_mm": {"base_h","L1","L2","L3"}, "reach_mm", "payload_g", "gripper_actuator": key,
  "joints": [{"name","actuator","torque_req_nm","torque_with_margin_nm","torque_avail_nm","margin"}], "issues": []}`

### `electronics2.plan(design, sizing) -> dict`
```
{"config": "modules"|"custom_pcb", "bus": "pwm"|"ttl_half_duplex",
 "supply": {"key": "psu_12v", "v": 12.0, "a": 10.0},
 "modules": [{"ref": "U1", "key": "esp32_devkit", "role": "controller"}, ...],   # catalog.MODULES keys; arm.build places non-external ones
 "custom_pcb": {"name", "size_mm": [w, h], "board_json": "<filled by finalize>"} | None,  # when config == custom_pcb the
                                                                                         # base bay holds this board instead of separate modules
 "actuators": [{"joint", "key", "id": int, "channel": int|None}],
 "sensors": [{"ref", "key", "where": "jaw_left"|"wrist"|...}],
 "power_budget": {"rails": [{"name","v","peak_a","avg_a","source"}], "supply_ok": bool},
 "issues": [], "warnings": []}
```
### `electronics2.finalize(design, sizing, eplan, asm, outdir) -> dict`
Writes into `outdir` (= `<rev>/electronics/`): `schematic.svg`, `schematic.json`, `harness.csv`,
`firmware/morph_arm/morph_arm.ino`, and for custom_pcb `board/board.json` (same JSON format as
`morph/board.py`: size_mm, components[{ref,value,outline,pads[{name,x,y,net}]}], traces[{net,layer,width,points}],
pour, ratsnest, drc), `board/gerbers/*.gbr|.drl|gerbers.zip`, `board/jlc_bom.csv`, `board/jlc_cpl.csv`.
Returns
```
{"config", "bus", "schematic_svg": "schematic.svg", "nets": [{"name","nodes":["U1.IO17","J1.2"]}],
 "board": {"json": "board/board.json", "gerbers": "board/gerbers/gerbers.zip", "size_mm", "drc"} | None,
 "harness": [{"name","from","to","length_mm","conductors","wire","connector_a","connector_b"}],
 "firmware": "firmware/morph_arm/morph_arm.ino", "power_budget": {...},
 "sourcing": [ROW...], "configs": {"modules": {"cost","summary"}, "custom_pcb": {"cost","summary"}},
 "issues": [], "warnings": []}
ROW = {"item","category":"actuator"|"electronics"|"pcb"|"cable"|"connector"|"fastener"|"mechanical"|"fabricated",
       "qty","make_or_buy":"buy"|"make","how": "buy from vendor" | "order PCBA from JLCPCB with jlc_bom.csv/jlc_cpl.csv" | ...,
       "vendors":[{"vendor","url","unit_price","lead_days","note"}],"selected":{"vendor","unit_price"},"ext_price"}
```

### `simulate.run_all(asm, design, outdir, scenarios=None, fast=False) -> dict`
Scenarios: `pick_place`, `payload_sweep`, `workspace`, `shake`. One JSON per scenario at `outdir/sim_<name>.json`:
```
{"scenario","title","dt": 0.0333, "joints": [joint names incl. "gripper"],
 "frames": [{"t","q":[...same order as joints; gripper in mm],"obj":[x,y,z,qw,qx,qy,qz] (mm, base frame),
             "tau":[N·m per arm joint], "grip_n": float, "phase": "approach"|"grasp"|"lift"|"move"|"place"|"release"|"retreat"}],
 "markers": [{"t","label"}], "object": {"shape","diameter_mm","length_mm","color"},
 "targets": {"pick":[x,y,z],"place":[x,y,z]}, "points": [[x,y,z],...]  (workspace only),
 "metrics": {"success": bool, ...scenario specific..., "max_torque_pct": {joint: pct}}}
```
Returns `{"scenarios": {name: {"file": "sim_<name>.json", "metrics": {...}}}, "mjcf": "robot.xml"}` and writes `outdir/robot.xml`.

### `manufacturing.plan(design, asm, outdir, efinal) -> dict`
Build configs: `prototype` (FDM PETG/PLA + off-the-shelf modules), `pilot` (CNC 6061 plates, sheet-metal
brackets, MJF/SLA covers, custom PCBA), `production` (injection-molded covers with tooling amortized over
`build.quantity`, sheet metal, PCBA). Writes per-group machine files under `outdir/<group>/`.
```
{"config", "configs": {name: {"unit_cost","tooling_cost","lead_days","summary"}},
 "parts": [{"group","qty","name","process","material","vendor","vendor_url","unit_cost","setup_cost","lead_days",
            "files":["<group>/<file>"],"dfm":[...],"notes"}],
 "totals": {"parts_cost","tooling_cost","purchased_cost","lead_days"}, "issues": [], "warnings": []}
```
`manufacturing.bom(design, asm, mfg, efinal) -> {"rows":[ROW...], "totals":{"buy","make","total","per_config":{...}}}`

### `manual.build(design, asm, mfg, efinal, outdir) -> dict`
Writes `outdir/manual.json` + `outdir/manual.html` (standalone, printable). Returns
`{"json":"manual.json","html":"manual.html","steps":[{"index","title","parts":[...],"fasteners":[...],"tools":[...],
"instructions","warnings":[...],"time_min"}],"tools":[...],"time_min_total"}`.

## Studio contract (server → front end)

`GET /api/state` → `{project, revs, history, current: REV, previous: REV|null}`; `GET /api/rev?rev=r003` → REV.
`POST /api/chat {message}`; `POST /api/params {changes: {"arm.dof": 6}}` (both trigger a run).
SSE `GET /api/events`: `{"type":"user"|"assistant","text","changes"?}`, `{"type":"start","rev","parent","dirty"}`,
`{"type":"node","node","status":"computing"|"computed"|"reused","seconds"?}`, `{"type":"done","current":REV,"previous":REV|null,"seconds"}`.
```
REV = {rev, label, parent, changes: {key: [old, new]}, provenance: {node: "computed"|"reused from r002"}, timings,
       nodes: [[key, label], ...],                       # pipeline strip order
       design: {flat}, params: [{key,label,group,type:"number"|"select",value,options?,min?,max?,step?,unit?}],
       summary: {...},
       robot: {json: url, glb: url, urdf: url},
       sizing: {...},
       sims: {name: {url, title, metrics}},
       electronics: {...finalize dict..., base: url-prefix for its files, plan: {...}},
       manufacturing: {...plan dict..., base: url-prefix},
       bom: {rows, totals}, manual: {json: url, html: url, steps: [...]}}
```
Fixtures for front-end work live in `morph/studio/static/fixtures/` (robot GLB/JSON are real stub output;
the rest are hand-written to the schemas above).
