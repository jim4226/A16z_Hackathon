# Third-party notices

Morph (R Cubed) is licensed under the Apache License 2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). The repository
and the wheel also carry the third-party files below, each under its own licence, unchanged unless the table says
otherwise.

| What | Where | Licence | Source |
|---|---|---|---|
| three.js r160: the module build and the examples the 3D viewer uses (GLTFLoader, STLLoader, OrbitControls, CSS2DRenderer, BufferGeometryUtils, the Line2 family, RoomEnvironment) | `morph/studio/static/vendor/` | MIT, [`THREE_LICENSE`](morph/studio/static/vendor/THREE_LICENSE). three.js adapted RoomEnvironment from Google's model-viewer (Apache-2.0). | https://github.com/mrdoob/three.js |
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

`constraints.txt` lists the exact versions, including the packages these depend on. The optional extras add more:
`morph[video]` installs imageio-ffmpeg (BSD-2-Clause), whose platform wheels contain an FFmpeg executable under
FFmpeg's own licence (LGPL or GPL, depending on the build); `morph[cad]` installs cascadio (MIT), which embeds
Open CASCADE Technology (LGPL-2.1 with an exception); `morph[xacro]` installs xacro (BSD-3-Clause) and PyYAML (MIT).
Morph doesn't ship any of them.

## What Morph generates

Designs, firmware, boards and documents that Morph generates for you are yours. Morph doesn't put its own licence
on them: `morph kicad` adds a LICENSE file to a board export only when you ask for one (`--license apache-2.0` or
`--license <your file>`).
