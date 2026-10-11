# BoardRepo and Morph

[BoardRepo](https://boardrepo.com/) hosts 8,263 public KiCad and Altium boards (count as of Oct 11 2026). Each
board has schematic, PCB, 3D and BOM views. It is run by Flintt, Inc. and is not a hackathon sponsor.

Morph uses it two ways. Neither needs an account or a key.

## 1. The carrier board as a KiCad project

`morph/kicad.py` turns `board.json` into a KiCad 8/9 project. `board.build()` writes it on every revision,
next to the Gerbers, in `<rev>/electronics/board/kicad/`. The project contains:

- **PCB:** one inline footprint per part (no library links), every trace as a segment, and the GND pour as a
  zone that KiCad refills. Pads cover both mask layers, so fab outputs KiCad generates include solder mask,
  which Morph's own Gerbers don't.
- **Schematic:** one box symbol per part. Each connected pin has a net label and each unused pin has a
  no-connect flag, so KiCad's PCB-vs-schematic check passes.
- **Project file and README.**

The writer is pure Python, so the demo laptop needs no KiCad. When `kicad-cli` is installed, `kicad.check()`
runs DRC (with schematic parity) and ERC.

Results with KiCad 9.0.9:

- Morph builds the Arduino Nano, Raspberry Pi Pico and ESP32 boards at 3.9k, 10k and 22k dividers, plus the
  committed `examples/raw-egg/r004` board.
- Every one has 0 DRC violations, 0 unconnected items, 0 parity issues and 0 ERC errors.
- `tests/test_boardrepo.py` repeats the per-MCU check whenever `kicad-cli` is present.

Darwin's PCB-fab handoff (`morph source`) lists `kicad/board-kicad.zip` next to `gerbers.zip`. The search query
is unchanged, so Darwin's cache still hits.

## 2. Publishing a revision

```bash
morph kicad raw-egg r004
```

This command:

- writes `projects/raw-egg/r004/boardrepo/raw-egg-r004/` (the project, plus a README with the design facts and
  related open designs);
- zips it as `raw-egg-r004-kicad.zip`;
- runs KiCad's checks if `kicad-cli` is installed;
- prints the upload steps;
- logs a `board_export` ledger row. Keewano ignores this row type, so nothing is sent.

BoardRepo takes a zipped KiCad project folder at <https://boardrepo.com/upload>. Signing in with GitHub or
Google creates the account. Gerber-only uploads are stored, but BoardRepo doesn't render them, which is why
the KiCad project exists.

Uploading needs the user's own sign-in, so Morph stops at the zip. After processing,
`https://boardrepo.com/<you>/<board>.md` lists "PCB layout" under Contents. A public GitHub repo can also be
imported by URL.

## 3. Reference designs

`morph boardrepo` lists a short set of checked boards. For each one it reads the board's public markdown page
(`https://boardrepo.com/<owner>/<slug>.md`, no auth) for the live licence and views, with a disk cache.
`morph boardrepo owner/slug` shows any single board. `morph kicad` adds the boards that match the design
(gripper, sensor, servo family) to the published README.

| board | what it is | used for | licence on BoardRepo |
|---|---|---|---|
| [evroc/robotic-gripper](https://boardrepo.com/evroc/robotic-gripper) | 3D-printed servo gripper with its own carrier board (Seeeduino, USB-C, DC jack), no force sensor | every gripper: the closest open analog | MIT |
| [pollen-robotics/elec-rpi-robot-hat](https://boardrepo.com/pollen-robotics/elec-rpi-robot-hat) | Dynamixel/Feetech over TTL and RS-485, IMU, 5–28 V in | robot revisions with `smart_bus` / `dynamixel` servos | none recognised: link only |
| [c-ake/pca9685-rpi-hat](https://boardrepo.com/c-ake/pca9685-rpi-hat) | 16-channel PCA9685 PWM, through-hole | `hobby_pwm` arms | GPL-3.0 |
| [szbeni/forcegauge](https://boardrepo.com/szbeni/forcegauge) | load cell + HX711 + ESP32-C3 | FSR designs: the absolute-force upgrade | GPL-3.0 |
| [solderedelectronics/load-cell-ampfilier-hx711-board-hardware-design](https://boardrepo.com/solderedelectronics/load-cell-ampfilier-hx711-board-hardware-design) | HX711 breakout, 22 × 22 mm | same | none recognised: link only |

GPL-3.0 boards are fine to learn from, but copying their layout makes a derivative work. Boards with no
recognised licence keep all rights, so link to them and don't copy them.

## Not used: the MCP server

BoardRepo's MCP server (`https://boardrepo.com/mcp`) can search, read nets and BOMs, and save versions of
your own boards. Every call needs a browser OAuth token, public boards included, and the only advertised
scope is `read:public`. Morph therefore reads the public `.md` pages instead.

For interactive use from Claude Code:

```bash
claude mcp add --scope user --transport http boardrepo https://boardrepo.com/mcp   # then /mcp to sign in
```
