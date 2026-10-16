# Import (beta)

Import brings an existing design into Morph Studio: a whole robot, the object the gripper should hold, a parts list,
a KiCad board, or a project someone exported from Morph. Open it with **Import** (top right, or in the project menu),
drop files anywhere on the page, or use `morph import` in a terminal.

## What you can import

| You have | Morph reads | What happens |
|---|---|---|
| A robot | a folder or `.zip` with a URDF, a xacro file (needs the `xacro` and `PyYAML` packages: `morph doctor` gives the command) or MuJoCo XML, plus its meshes | Morph finds the gripper joint, the tool point, the base and a ready pose, asks only what the files can't tell (usually which motors drive the joints), saves the robot and builds it as a **new project** |
| The object to grasp | an STL, OBJ, PLY, OFF, GLB or glTF mesh, or STEP with the `cascadio` package (`morph doctor` gives the command) | its size, shape and (with a density) mass become the open project's object: one new revision, like a parameter edit. An object Morph knows by name (an egg, a strawberry) brings its damage force and friction; a fragile one, or one narrower than the plain finger closes on, gets the force-sensing finger; one whose grip would bend the fingers too far gets thicker finger beams, as New robot would make them |
| A parts list | CSV, TSV, XLSX or a Markdown table (KiCad's grouped CSV and KiBoM too) | the rows, the total (none when no line has a price) and the parts Darwin can look up; the dialog shows each line's MPN and description and offers them as a CSV. A line's part name says what it is, read from its first noun phrase, before what it comes with or fits ("with", "incl.", "(", a comma, "for", "compatible with", "fits"): a motor word with nothing after it but how the motor is built ("MG996R Metal Gear Servo", "DS3218 Upgrade 20KG Full Metal Gear Digital Servo", "Feetech STS3215 Servo Motor 12V Metal Case", "MG996R Servo Motor Metal Gear Dual Ball Bearing", "STS3215 servo, horn included"), or a motor's model with its maker ("Feetech STS3215-C018", "DS3218MG"), is the catalog motor its name or MPN names, and so is one named with the joint, arm or motor ID it drives or is, before it, in brackets or after it ("Shoulder STS3215 12V", "Elbow flex: STS3215 C001", "Waist XM430-W350-T", "Left master arm - XL430-W250-T", "ID 1: STS3215", "[J1] STS3215", "J1 (STS3215)", "Base (J1): STS3215 12V", "STS3215 C018 shoulder pan", "MG996R: upper arm", "Servo for gripper (SG90)", "Schulter MG996R"). Its description or notes pick the variant (an STS3215 described as 12 V, 12VDC or 30 kg·cm, or with the code C018 or C047, is the 12 V servo; Waveshare's ST3215 is too unless it says 7.4 V; a note about the other arm, "the follower uses the 12V version", picks nothing). A label is made only of joint, motion, arm, side and ID words (beside makers', kind, colour and filler words, which only qualify the motor): without a motor word ("Wheel servo STS3215 12V" is the servo), any other noun makes the line that part, so "Shoulder bracket STS3215", "Lift arm MG996R", "Upper arm link (MG996R)", "Gripper rack (SG90)", "Counterweight (MG996R)", "Moving jaw SG90", "Gripper (uses SG90)" and "Pan tilt SG90 kit" are not motors (a drive wheel or a numbered link is a label only before a ":" or a dash: "Wheel: STS3215 12V", "Link 2: MG996R"). A printed part is never a motor: "printed", "3D printed", "PLA", "PETG", "ABS", "resin" or "STL" anywhere in a line's name or notes makes it a part ("Base (PLA, holds STS3215)", "Base MG996R (printed)", "Upper arm STS3215" noted "printed"), except in what a servo fits ("MG996R servo for the 3D printed arm" is the servo). Nor is a model followed by a part's word with no joint motion and no spec or code between them ("SG90 gripper", "MG996R base"; "STS3215 C018 gripper" is the servo). "STS3215 servo cable 200 mm", "STS3215 servo horn aluminum", "SG90 servo arm set", "XL430 frame FR12-S102K", "Servo horn compatible with MG996R" and "U2D2 for Dynamixel XL430" are not motors, nor is a screw or a board whose notes mention the servo, and a Dynamixel is the catalog motor only with its own code (an XL330-M077 is not the M288). A line that names two models is the one in its head ("DS3218 servo (MG996R compatible)" is the DS3218). A stand-in ("STS3215 dummy servo") is no motor. A line named "Feetech servo", "Wrist pitch/roll servo", "Servo #1" or just "Dynamixel" is the motor its description names, read the same way (just "Dynamixel" described as "U2D2 interface for XL430" is no motor); a part named only by its joint is the motor its MPN names, or its description when that says it is a servo ("Base" described as "STS3215 12V servo"; "Gripper" described as "SG90" may be a whole gripper, so it is not read as a motor); a "Model", "Motor" or "Servo" column counts as the MPN. In a board's parts list (one with a footprint column), a line whose reference designators are connectors or chips ("J2 J3") is never a motor. Steppers, BLDC and gear motors are listed as actuators with no catalog motor ("Stepper motor NEMA 17", "Linear actuator 100mm stroke 12V"), and so is a servo the catalog lacks when its name or description says servo ("Hiwonder" described as "LX-16A bus servo"); named by its maker and code alone ("Hiwonder LX-16A") it is listed as purchased. A pack bought once ("STS3215 12V servo (6 pcs)", "STS3215 servo ×6 (follower arm)", "Set of 6 STS3215 servos", Qty 1) counts its motors. Nothing is changed |
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
