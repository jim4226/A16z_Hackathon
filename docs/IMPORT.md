# Import (beta)

Import brings an existing design into Morph Studio: a whole robot, the object the gripper should hold, a parts list,
a KiCad board, or a project someone exported from Morph. Open it with **Import** (top right, or in the project menu),
drop files anywhere on the page, or use `morph import` in a terminal.

## What you can import

| You have | Morph reads | What happens |
|---|---|---|
| A robot | a folder or `.zip` with a URDF, a xacro file (needs the `xacro` and `PyYAML` packages: `morph doctor` gives the command) or MuJoCo XML, plus its meshes | Morph finds the gripper joint, the tool point, the base and a ready pose, asks only what the files can't tell (usually which motors drive the joints), saves the robot and builds it as a **new project** |
| The object to grasp | an STL, OBJ, PLY, OFF, GLB or glTF mesh, or STEP with the `cascadio` package (`morph doctor` gives the command) | its size, shape and (with a density) mass become the open project's object: one new revision, like a parameter edit. An object Morph knows by name (an egg, a strawberry) brings its damage force and friction; a fragile one, or one narrower than the plain finger closes on, gets the force-sensing finger; one whose grip would bend the fingers too far gets thicker finger beams, as New robot would make them |
| A parts list | CSV, TSV, XLSX or a Markdown table | the rows, the total and the parts Darwin can look up; the dialog shows each line's MPN and description and offers them as a CSV. A line's part name or MPN says which catalog motor it is, and its description or notes pick the variant (an STS3215 described as 12 V is the 12 V servo); a screw or a driver board whose notes mention the servo is not a motor, and neither is a horn, a bracket, a power supply or a board named after it ("STS3215 servo horn"); a line named "Feetech servo" or "Gripper servo" is the motor its description names. Nothing is changed |
| A board | a KiCad 6 or newer `.kicad_pcb` or `.kicad_sch` | its size, footprints and layers. Nothing is changed |
| A Morph export | the ZIP from **Export** (or `morph export`) | its newest design is rebuilt as a new project; earlier designs are kept next to it for reference |
| A link | `https://github.com/...`, `raw.githubusercontent.com`, `https://huggingface.co/...` | the files are downloaded (public repositories only), then imported as above |

Refused with a reason: native CAD files (SolidWorks, Fusion, Inventor, Onshape documents: export a URDF or a STEP),
USD and SDF scenes (convert to URDF), KiCad 5 files (save them again in KiCad 6 or newer), 7-Zip and RAR archives
(unzip them first). COLLADA (`.dae`) and 3MF meshes need packages Morph doesn't install; export STL, OBJ or GLB.

In a terminal (set up as in
[Commands in a terminal](../README.md#commands-in-a-terminal)): a folder, a `.zip`, a URDF or a mesh; a link, with
one motor family for every joint; a URDF with a motor for each joint; the object for an existing project; and a parts
list, showing what Morph found without changing anything:

```shell
morph import robots/my-arm
morph import https://github.com/owner/repo --family smart_bus
morph import arm.urdf --actuators "shoulder=sts3215_7v4,elbow=xl430_w250" --name my-arm
morph import cup.stl --as object --project egg-picker --units mm
morph import parts.xlsx --dry-run
```

`--dry-run` detects and reports without saving anything; `--json` prints the result for scripts. `morph import --help`
lists every option.

## What Morph keeps

- An imported robot is saved in `projects/.robots/<name>/` with its licence file and where it came from (the files,
  the link, the answers you gave). Its new project names it in `arm.source`.
- An import that made or changed a project is noted in that project's ledger: the kind of thing, its file format,
  whether it came from files or a link, whether you picked the motors and how many warnings it had. Never file names,
  links or robot names. Keewano gets these only when you turned its events on.
- Import sessions (the files while you review them) live in `projects/.imports/` and are deleted after a day, and
  when the build that used them ends.
- On a team Studio, every import goes into the team's audit log with the link it came from.

A link is the only part of an import that uses the network ([PRIVACY.md](PRIVACY.md)).

## How sizing works for imported arms

Morph sizes an auto-imported arm for its worst pose over the joint range, so the motors it suggests hold the load in
every reachable pose. That is more conservative than the shipped SO-101 demo, which is sized in its stretched-out
pose: an SO-101 imported from its own URDF reports its shoulder needing about 2.0 N·m with margin against the
STS3215's 1.91 N·m, so it builds as not feasible until you choose stronger motors. The shipped demo is the same arm,
sized the way it is used.

Picking one motor family in the dialog (or `--family`) sizes every joint the same way, with the motors counted as
bought parts as the build counts them, and keeps the arm on one supply voltage: the 7.4 V STS3215 on every joint while
it holds them all, otherwise the 12 V STS3215 on all of them; XL330s never share a supply with XL430s and XM430s.
The SO-101 with **Smart bus** picked builds as feasible on 12 V STS3215s. A motor picked per joint shares that one
servo supply: a 12 V STS3215 beside 7.4 V ones would run at 6 V and give about half its rated torque, so the build
flags it. When an imported robot builds but its own checks flag it, the dialog says so and lists why, and so does the
new project's chat, with the fixes Morph can make as buttons (each builds a new revision): **Use 12 V STS3215 servos**
on every joint for an arm on 7.4 V STS3215s that can't hold its load (the SO-101 above then builds as feasible),
Dynamixel servos otherwise, and thicker fingers when they would bend too far. An object import whose build isn't
feasible says why in the same way.

The gripper, tool point and motors Morph derived are marked auto-calibrated: check them in the Robot tab before you
trust the numbers.

## Limits and safety

Every file and link is treated as hostile:

- **Archives:** at most 2,000 entries, 300 MB in total, 100 MB per file and a 100x compression ratio. No links,
  nested archives, absolute paths or `..` paths.
- **Uploads:** at most 300 MB per file and 300 MB and 2,000 files per import, streamed to disk in 1 MiB chunks and
  identified by their content, not their extension.
- **XML (URDF, MuJoCo XML, xacro, COLLADA):** no `<!DOCTYPE>` or `<!ENTITY>`, at most 20 MB.
- **Meshes:** at most 1.5 million triangles; every mesh path must stay inside the imported folder.
- **xacro** runs in a separate process with no environment, a 20 second timeout, an output cap and file access
  limited to the imported folder; its expressions are checked before they run. It is not a full sandbox: on Windows
  there is no memory limit, so a hostile file can use memory until the timeout.
- **Links:** HTTPS only, to github.com, codeload.github.com, raw.githubusercontent.com, huggingface.co and `*.hf.co`;
  every redirect hop must be a public address (at most 3 redirects); at most 300 MB and 400 files, 30 seconds.
  On a Studio other computers use (`--remote`, `--team`) links are off unless it was started with
  `--allow-url-import`. With network lookups off (Keys, Privacy and network) or `MORPH_OFFLINE=1`, links are refused.
- No program in the imported files is ever run.

## Known limits (beta)

- Morph builds arms: at least 3 arm joints before a two-finger gripper. Robots without movable joints, and legged or
  multi-arm robots, are refused with a reason; when Morph can't tell which joint opens the jaws, it asks.
- The motor re-skin (Morph's servos in place of the robot's own) is accurate for SO-100 and SO-101 style arms; for
  others, check the Electronics tab.
- Some community arms build all nine stages but fail the pick-and-place simulation (for example, the Koch arm drops
  the object); the Simulation tab shows why.
- Not tested on Windows and macOS yet, and not with STEP, COLLADA or 3MF files.
