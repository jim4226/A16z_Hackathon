# BoardRepo for Morph

[BoardRepo](https://boardrepo.com/) hosts 8,263 public KiCad and Altium boards (count as of Oct 11 2026). Each board
has schematic, PCB and BOM views, and you can search by keyword (`/browse?q=servo`), MCU
(`/browse?component=esp32`) or maker (`/browse?q=author%3Aadafruit`). It also runs a **remote MCP server**
at `https://boardrepo.com/mcp`. The server uses Streamable HTTP and browser OAuth with dynamic client
registration, so you don't need an API key. Through it an assistant can:

- search public boards;
- read schematics, nets, component values, BOMs and source files;
- trace signals and run design checks;
- compare revisions;
- with explicit write consent, save a new version of a KiCad board you own.

A read connection can't publish.

```bash
claude mcp add --scope user --transport http boardrepo https://boardrepo.com/mcp   # then /mcp to sign in
```

In claude.ai, go to Settings → Connectors → Add custom connector and paste the same URL.

## Where it plugs into Morph

File paths refer to the robot-platform branch (`claude/determined-keller-or95aa`).

| # | where | what to add | effort |
|---|---|---|---|
| 1 | `docs/DATA_SOURCES.md`, "Used or directly usable today" | a BoardRepo row: open reference circuits with netlists and BOMs, read via MCP, licensed per board | 2 min |
| 2 | `morph/robot/catalog.py` `MODULES` | an optional `reference_designs: [url, ...]` key on `pca9685_board`, `bus_servo_adapter`, `u2d2` and `fsr402`, filled from the table below. The contract allows adding keys. Keep these links out of `search()`/`V()`, which are priced vendor rows | 10 min |
| 3 | `electronics2.plan/finalize`, `custom_pcb` with `bus: ttl_half_duplex` | Pollen's HAT already drives Dynamixel and Feetech servos over TTL and RS-485. Read its nets and BOM through the MCP and reuse the bus interface instead of inventing one | when that module is built |
| 4 | Stretch goal 4: load cell + HX711 | two HX711 reference boards below, one of them with an ESP32 | when attempted |
| 5 | Publishing Morph's carrier board | **This is the main gap.** BoardRepo saves KiCad projects, but `morph/board.py` only writes Gerbers and Morph's own `board.json`. Adding a `.kicad_pcb` writer (plus `.kicad_sch` if possible) would let each Morph revision (r001…r00N) become a BoardRepo version with a shareable link | 1–2 h |
| 6 | `morph/agent.py` `research` | Calling the MCP from the Claude API would mean getting and passing a BoardRepo OAuth token yourself. For today, use it interactively from Claude Code or claude.ai instead of wiring it into the CLI | skip today |

## Reference boards worth reading

Each of these was opened on BoardRepo on Oct 11 2026, and the license is the one its page shows. Check the license before
you copy a layout or redistribute files. GPL-3.0 boards are fine to learn from, but copying them makes
your board a derivative work.

| board | what it is | maps to | license |
|---|---|---|---|
| [evroc/robotic-gripper](https://boardrepo.com/evroc/robotic-gripper) | 3D-printed servo gripper for robot arms (shown on a UR3e), with a Seeeduino carrier, USB-C and a DC jack. 29.75 × 57 mm, 2 layers | closest analog to Morph's gripper carrier board; prior art for the pitch | MIT |
| [pollen-robotics/elec-rpi-robot-hat](https://boardrepo.com/pollen-robotics/elec-rpi-robot-hat) | Pi HAT driving Dynamixel/Feetech over TTL and RS-485, with IMU, 5–28 V in. 65 × 30.9 mm, 4 layers, KiCad 9 | `smart_bus` / `dynamixel` families on a `custom_pcb` | not shown |
| [c-ake/pca9685-rpi-hat](https://boardrepo.com/c-ake/pca9685-rpi-hat) | 16-channel PCA9685 PWM HAT, through-hole for hand soldering. 65 × 30 mm | `hobby_pwm` + `pca9685_board` on a `custom_pcb` | GPL-3.0 |
| [szbeni/forcegauge](https://boardrepo.com/szbeni/forcegauge) | HX711 + ESP32-C3 load-cell force gauge that streams force over WiFi. 60 × 40 mm | stretch goal 4 | GPL-3.0 |
| [solderedelectronics/load-cell-ampfilier-hx711-board-hardware-design](https://boardrepo.com/solderedelectronics/load-cell-ampfilier-hx711-board-hardware-design) | HX711 breakout. 22 × 22 mm | stretch goal 4 | not shown |

Seen in search results but not opened yet: `adamb314/servoproject` (closed-loop hobby servo),
`marginallyclever/sixi-2`, `sensagroup/roboticarm`, `jorgehsm/scara-arm`, and
`twisted-fields/rp2040-motor-controller`. Searches run: `robot` (476 boards), `servo` (225), `load cell` (339),
`robot arm` (60), `pca9685` (45), `feetech` (3) and `dynamixel` (3). Search sometimes answers "Search is busy
right now"; retrying works.
