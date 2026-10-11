# Data sources for Morph

Ranked for **today's use case** (gripper fingers, fabricated and tested). Check the license on each
before redistributing data or training a commercial model.

## Used or directly usable today

| source | what we'd take | status |
|---|---|---|
| **Our own ledger** (`projects/*/ledger.jsonl`) | design → predicted grasp/deflection/fab time → measured result, per revision | **built**: every `run` and `measure` appends a row. The only source that links the whole workflow |
| Material datasheets (typical E, density, yield, $/kg) | `morph/materials.py` | built-in typical values. Replace with measured coupons as data comes in |
| [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie) | real gripper/arm models to replace our box hand | stretch goal. Per-model licenses |
| YCB object set (standard grasping benchmark objects with meshes and masses) | object dimensions and masses by name (cans, boxes, fruit, tools) | stretch goal: object library for `morph new` |

## Useful next (beyond today)

| source | what it provides | where it helps | caveat |
|---|---|---|---|
| [Fusion 360 Gallery](https://github.com/AutodeskAILab/Fusion360GalleryDataset) | 8,625 CAD construction sequences + assemblies/joints | learning editable CAD construction | [non-commercial research license](https://github.com/AutodeskAILab/Fusion360GalleryDataset/blob/master/LICENSE.md) |
| [Open X-Embodiment](https://github.com/google-deepmind/open_x_embodiment) | robot episodes in a common format | grasp/manipulation behavior priors | per-dataset terms |
| Grasp datasets (e.g. ACRONYM, GraspNet-1Billion) | labeled grasps on object meshes | grasp-pose priors for which surfaces fingers should conform to | check each license |
| [NIST AM-Bench](https://www.nist.gov/ambench/am-bench-data-and-challenge-problems-0) | controlled additive-manufacturing measurements | calibrating print-accuracy predictions | |
| [NASA Milling Wear](https://catalog.data.gov/dataset/milling-wear) | speeds/feeds/DOC vs. tool wear | tool-life term in CNC cost | not a CAD→toolpath dataset |
| [Sandvik Coromant tool library](https://www.sandvik.coromant.com/en-us/tools/digital-machining/coroplus-tool-library) | tool geometry, cutting data, CAD | replacing our generic feeds table | supplier reference data, not bulk-reusable by default |
| [PhysioNet sEMG gait](https://physionet.org/content/semg/1.0.1/) | EMG + joint angles, 31 subjects | wearables/prosthetics only | low priority here |

## Software we build on

MuJoCo (simulation), shapely + manifold3d + trimesh (2.5D CAD → watertight solids),
Claude API (need → spec, web research), GRBL / OctoPrint (machines). FreeCAD's CAM
workbench is the upgrade path if we need full 3D toolpaths. Today's 2.5D parts don't need it.

## What to capture per revision (already the ledger schema)

- design: every parameter (geometry, material, process, gripper, object)
- predictions: grip force budget, deflection, stress SF, sim success and min holding force, fab time and cost, DFM warnings
- fabrication: machine, files, actual time
- measured: dimensions, grasp success over N trials, holding force, notes
