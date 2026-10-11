# Morph: first recording script (3 minutes, single take)

**Setup:** double-click `run_studio.bat`. Wait for the Morph Studio window (the first start after an update rebuilds the projects in CMD for about a minute), then maximize it. Screen recorder: full screen, 1080p, mic on.
Reset before each take: close the window, Ctrl+C then Y in CMD, delete the `projects` folder, and double-click `run_studio.bat` again. This gives you a clean r001.

Lines to say are in quotes; actions are in **bold**.

---

**0:00 — Hook (15 s).** *Morph Studio open on the **egg-picker** robot, Robot tab.*
"Designing one robot (CAD, simulation, electronics, firmware, sourcing, manufacturing, assembly docs) takes an engineer about six weeks: 241 hours by published numbers. Every change ripples through all of it. Morph does it in about a minute, and every change ripples automatically."

**0:15 — The robot (20 s).**
- Click **Motion demo**: "Everything that moves is real kinematics: five joints plus a rack-and-pinion gripper."
- Click **X-ray** and **Labels**: "The electronics in the base, the servo bus, the cables routed through every joint, the force sensor in the fingers."
- Drag **Explode** to about 60%, then back.

**0:35 — Simulation (20 s).** Open the **Simulation** tab and choose **Pick & place**, then press play.
"It doesn't just draw the robot, it tests it: MuJoCo physics, pick and place with the force-limited grip, torque on every joint, payload sweep, workspace and a shake test."

**0:55 — Edit the electronics in chat (30 s).** Type: **Swap to Dynamixel servos**
- "Watch the pipeline." *Point at the strip: sizing → electronics → CAD → simulation → schematic/PCB/firmware → manufacturing → BOM → manual regenerate; the gripper analysis is reused.*
- When the auto-tour lands on **Electronics**: "New bus, new protocol, new firmware, new power budget, new harness."
- On **BOM & Sourcing**: "Every part with a vendor, a price and a link. Buy it, or build it."

**1:25 — Custom PCB (20 s).** Type: **Use a custom PCB**
- *The PCB builds itself on screen: parts, ratsnest, routed traces, ground pour, DRC pass.*
- "From off-the-shelf boards to one controller PCBA, with Gerbers plus the JLCPCB BOM and pick-and-place files ready to order."

**1:45 — Manufacturing configs (20 s).** Click the **Pilot** pill (or type **Pilot build: CNC + sheet metal**).
"Same robot, different way to make it: CNC aluminium plates, bent sheet-metal brackets with flat patterns, molded covers. Per-part process, vendor, cost and lead time, and all three build configs compared."

**2:05 — Assembly (15 s).** Open the **Assembly** tab and click **Autoplay**.
"Step-by-step assembly with fasteners, torques and tools. The printable manual is generated too."

**2:20 — An open-source robot (25 s).** In the **project switcher** (top left), choose **so101**.
- "It also imports real open-source robots. This is Hugging Face's SO-101, from its own files."
- Type: **Swap to Dynamixel servos**. "The servos are swapped, and Morph flags in orange the printed holders that no longer fit."
- Type: **Add a wrist camera**. "Camera mount added, USB device added, wiring and BOM updated."

**2:45 — Time saved (15 s).** Point at the **time card**.
"One revision: 241 engineer-hours by published numbers, about 15 minutes with Morph, including the engineer's review. Every change after that: minutes instead of days. That's Morph."

---

**If something goes wrong on camera:**
- A stage shows an error: keep talking. Every other stage keeps working, and the strip shows exactly what failed.
- Chat doesn't understand a sentence: use the suggestion chips under the chat box. They always work, even without an API key.
- The 3D view is slow: turn off **X-ray** and **Labels**.

**Numbers to have on screen or a slide:** see `docs/TIME_SAVED.md` (cited table and one-liners).
