# Third-party notices

Morph (R Cubed) is licensed under the Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). The repository
and the wheel also carry the third-party files below, each under its own licence, unchanged unless the table says
otherwise.

| What | Where | Licence | Source |
|---|---|---|---|
| three.js r160: the module build and the examples the 3D viewer and world mode use (GLTFLoader, STLLoader, OrbitControls, CSS2DRenderer, BufferGeometryUtils, the Line2 family, RoomEnvironment, PointerLockControls, TransformControls, Octree, Capsule) | `morph/studio/static/vendor/` | MIT, [`THREE_LICENSE`](morph/studio/static/vendor/THREE_LICENSE). three.js adapted RoomEnvironment from Google's model-viewer (Apache-2.0). The build is unchanged; the examples differ from r160 only in their import lines, which name the files next to them (`./three.module.min.js` for `three`, and `./Capsule.js`, `./BufferGeometryUtils.js`, `./LineMaterial.js`, `./LineSegmentsGeometry.js` for their upstream folders). | https://github.com/mrdoob/three.js (`build/` and `examples/jsm/` at r160) |
| SO-101 robot arm: URDF, MJCF and STL meshes | `robots/so101/` | Apache-2.0, [`LICENSE`](robots/so101/LICENSE) and [`NOTICE`](robots/so101/NOTICE) | https://github.com/TheRobotStudio/SO-ARM100 (Simulation/SO101) |
| Inter font, weights 400 to 700, Latin subset (WOFF2) | `morph/studio/static/vendor/fonts/inter-latin-*.woff2` | SIL Open Font License 1.1, [`OFL-Inter.txt`](morph/studio/static/vendor/fonts/OFL-Inter.txt) | https://github.com/rsms/inter; the subset files are from Fontsource (`@fontsource/inter` 5.3.0) |
| JetBrains Mono font, weights 400 to 600, Latin subset (WOFF2) | `morph/studio/static/vendor/fonts/jetbrains-mono-latin-*.woff2` | SIL Open Font License 1.1, [`OFL-JetBrainsMono.txt`](morph/studio/static/vendor/fonts/OFL-JetBrainsMono.txt) | https://github.com/JetBrains/JetBrainsMono; the subset files are from Fontsource (`@fontsource/jetbrains-mono` 5.3.0) |

The Studio serves the two fonts itself, so opening it loads nothing from Google Fonts or any other site.

## Darwin Search answers

`data/darwin-cache/` holds answers from Darwin's public Search API ([darwin.so](https://darwin.so)), saved on
2026-10-11 so that **Find agents** still shows suppliers when Darwin can't be reached. Each file is one query and
the public agent listings Darwin returned for it: names, ids, capabilities and links. Darwin's terms
(https://darwin.so/terms) cover that content, not this repository's licence. Delete the folder to ship without it;
the Studio then shows only live answers.

## Test fixtures

The source archive carries the tests and their fixtures. Two hold answers recorded from real services:

| What | Where | Terms |
|---|---|---|
| Darwin Search answers, saved in October 2026: a search that asked a question back, and one capability's details | `tests/fixtures/darwin/` | Darwin's terms (https://darwin.so/terms), as for `data/darwin-cache/` |
| Mouser's Search API answer to a wrong key (a key of zeros), recorded on 2026-10-14: an `Errors` entry and nothing else | `tests/fixtures/tools/answers/mouser_wrong_key.json` | Mouser's API terms (https://www.mouser.com/apihome/) |

Every other fixture is Morph's own: the distributor answers in `tests/fixtures/tools/answers/` follow the documented
answer shape with made-up products, prices and stock (no copy of a provider's documentation example);
`tests/fixtures/tools/fixparts.json` describes a made-up distributor; `tests/fixtures/launchers/run_studio_v01.bat` is
Morph's own launcher from before v0.2; `tests/fixtures/brief/` holds New robot requests written or generated for
the tests (people, hand-over, harm and SO-101 tables), `tests/fixtures/key_filter/` made-up key shapes and names,
and `tests/fixtures/restart/` made-up restart cases; `tests/fw_stubs/` holds minimal declarations, written for the tests, of the
Arduino, Feetech, Dynamixel and Adafruit library functions the generated firmware calls.

## Python packages

The launchers install Morph's dependencies from PyPI into `.venv`; neither the repository nor the wheel contains
them. Each comes under its own licence:

| Package | Licence |
|---|---|
| numpy | BSD-3-Clause (with small parts under 0BSD, MIT, Zlib and CC0-1.0) |
| scipy | BSD-3-Clause |
| shapely | BSD-3-Clause |
| manifold3d | Apache-2.0 |
| trimesh | MIT |
| mujoco | Apache-2.0 |
| pydantic | MIT |
| anthropic | MIT |
| pillow | MIT-CMU |

`constraints.txt` lists the exact versions, including the packages these depend on. Optional packages, installed only
when you add them (`morph doctor` gives the command; a wheel's extras `video`, `cad` and `xacro` name the same ones):
imageio-ffmpeg (BSD-2-Clause), whose platform wheels contain an FFmpeg executable under FFmpeg's own licence (LGPL or
GPL, depending on the build); cascadio (MIT), which embeds Open CASCADE Technology (LGPL-2.1 with an exception); xacro
(BSD-3-Clause) and PyYAML (MIT). Morph doesn't ship any of them.

## What Morph generates

Designs, firmware, boards and documents that Morph generates for you are yours. Morph doesn't put its own licence
on them: `morph kicad` adds a LICENSE file to a board export only when you ask for one (`--license apache-2.0` or
`--license <your file>`).
