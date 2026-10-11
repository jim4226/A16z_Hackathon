# Sponsor integrations: Keewano and Darwin

Both integrations use only the Python standard library, so nothing new to install. With no keys,
both still work: Darwin Search needs no account, and Keewano switches itself off.

## In the Studio

| Where | What it shows |
|---|---|
| **BOM & Sourcing → Darwin agents** | One click (**Find agents on Darwin**) asks Darwin Search for an agent per purchased BOM line (servos, controller, supply, sensors, a fastener kit, servo cables), plus the fabrication handoffs: PCB fab from the Gerbers (custom PCB builds), 3D printing from the STLs, CNC from the DXFs and sheet metal from the flat patterns (pilot builds), and a design review. ★ marks the agent Morph routes that work to (`PREFERRED` in `morph/darwin.py`). Results stream in as each search returns and are saved to `<rev>/sourcing_darwin.json`. |
| **Inspector → Grip force window** | The Keewano line. With Keewano connected and measured builds for the robot's pad and material, the simulated holding force is scaled by measured/predicted (for example ×1.12 from 8 contour/PETG builds) before the grip target and firmware limit are set. |
| **Present mode, steps 8 and 9** | Darwin and Keewano each get a step with a caption. |

Every revision, chat edit, Darwin lookup and bench measurement is sent to Keewano as typed events
(`Project.log()` → `morph/keewano.py`).

## Keys (Keewano only)

Copy `env.example` to `.env` in the repo folder and fill in:

| Variable | What it is | Where in Keewano |
|---|---|---|
| `KEEWANO_API_KEY` | project key of type **SDK** (the server type is rejected) | Settings → Project → API Keys |
| `KEEWANO_PAT` | personal access token (`kwpat-...`) | Settings → Personal → Security |
| `KEEWANO_PROJECT_ID` | project UUID | Settings → Details → Project ID |

`.env` is git-ignored and read at every start (`run_studio.bat` needs nothing else). Keys set in the
terminal win over the file.

Check: `python -m morph.cli ask "SELECT count(*) FROM events"` prints a number.

**If the keys are for a project other than R cubed**, it has no build history yet. Load it once and
wait about 5 minutes before demoing: `python tools/keewano_backfill.py --send`. The backfilled
projects are named `synthetic-*`, and their force ratios are invented: say so if asked.

After setting keys for the first time, delete the `projects` folder (or use **Present → Reset demo**,
then restart) so the gripper stage reruns and picks up the calibration.

## Darwin when its API is down

Darwin's API returns intermittent 500s. Every good answer is cached in `~/.cache/morph/darwin/`, and
the repo ships answers for the demo's queries in `data/darwin-cache/`. When a live search fails, the
cached answer is used and labelled **cached**.

## Command line

```
python -m morph.cli source egg-picker          # Darwin shortlist for the latest revision
python -m morph.cli darwin "PCB fabrication quote from Gerber files"
python -m morph.cli ask 'SELECT "PadStyle", avg("HoldForceRatioPct") FROM events GROUP BY "PadStyle"'
```

## Claude Code: Darwin and Keewano as MCP servers (for a live agent call or a judge's question)

Run in a terminal, not inside a Claude Code session:
```
claude mcp add --scope user --transport http darwin https://mcp.darwin.so/mcp
claude mcp add --scope user --transport http keewano https://api.keewano.com/mcp --header "k-token: <your KEEWANO_PAT>"
```
- Darwin search works without signing in. Using an agent (Act) asks for a Darwin sign-in the first
  time; `part-lookup` costs $0.04 per call, so add a payment method first.
- Example asks: "use Darwin part-lookup to get live price and stock for the Feetech STS3215";
  "in our Keewano project, which pad style needed the most extra holding force?"

Ordering always stops at the shortlist: Darwin Act needs the user's own account and their approval
of any payment, and Morph never claims an order was placed.
