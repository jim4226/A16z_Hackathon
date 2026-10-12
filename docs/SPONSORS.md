# Sponsor integrations: Keewano and Darwin

The local beta keeps these integrations optional. Darwin Search needs no account but sends search
queries over the network, so enable sourcing in Settings before using it in Studio. Keewano event
sharing requires both your own credentials and explicit telemetry consent. Network-off mode blocks
both. Remote history never adjusts grip targets in this beta.

The presentation descriptions below document the original hackathon workflow. In the local beta,
the grip-force inspector identifies calibration as disabled, and presentation controls require
`python -m morph.cli studio --demo --open`.

## In the Studio

| Where | What it shows |
|---|---|
| **BOM & Sourcing → Darwin agents** | One click (**Find agents on Darwin**) asks Darwin Search for an agent per purchased BOM line (servos, controller, supply, sensors, a fastener kit, servo cables), plus the fabrication handoffs: PCB fab from the Gerbers (custom PCB builds), 3D printing from the STLs, CNC from the DXFs and sheet metal from the flat patterns (pilot builds), and a design review. ★ marks the agent Morph routes that work to (`PREFERRED` in `morph/darwin.py`). Results stream in as each search returns and are saved to `<rev>/sourcing_darwin.json`. |
| **Inspector → Grip force window** | The Keewano line. With Keewano connected and measured builds for the robot's pad and material, the simulated holding force is scaled by measured/predicted (for example ×1.12 from 8 contour/PETG builds) before the grip target and firmware limit are set. |
| **Present mode, steps 8 and 9** | Darwin and Keewano each get a step with a caption. |

When event sharing is enabled, supported revision, Darwin lookup and bench entries are sent as
typed events (`Project.log()` → `morph/keewano.py`). By default nothing is sent to Keewano.

## Keys (Keewano only)

Developer setup: copy `env.example` to `.env` in the repo folder and fill in:

| Variable | What it is | Where in Keewano |
|---|---|---|
| `KEEWANO_API_KEY` | project key of type **SDK** (the server type is rejected) | Settings → Project → API Keys |
| `KEEWANO_PAT` | personal access token (`kwpat-...`) | Settings → Personal → Security |
| `KEEWANO_PROJECT_ID` | project UUID | Settings → Details → Project ID |

`.env` is git-ignored and loaded only when `MORPH_LOAD_DOTENV=1` is explicitly set for the CLI.
Keys set in the terminal take precedence. Enable event sharing in Settings or use
`MORPH_TELEMETRY_ENABLED=1`; credentials alone do not enable it. The installed launcher's working
directory is separate from the repository, so prefer explicit environment variables for this
advanced integration. Normal Claude setup is available in Settings.

Check: `python -m morph.cli ask "SELECT count(*) FROM events"` prints a number.

The original `tools/keewano_backfill.py` contains invented `synthetic-*` measurements. It is retained
for isolated developer demonstrations only; do not seed a real engineering dataset with it. Legacy
R Cubed demo history is the same kind of seeded data. Automatic history-based calibration is now
disabled until verified measurements and a review workflow exist. Do not delete your projects when
connecting a service; ordinary settings changes preserve revisions.

## Darwin when its API is down

Darwin's API returns intermittent 500s. Every good answer is cached in `~/.cache/morph/darwin/`, and
the repo ships answers for the demo's queries in `data/darwin-cache/`. When a live search fails, the
cached answer is used and labelled **cached**.

## Command line

```
python -m morph.cli source egg-picker          # Darwin shortlist for the latest revision
python -m morph.cli darwin "PCB fabrication quote from Gerber files"
python -m morph.cli ask "SELECT \"PadStyle\", avg(\"HoldForceRatioPct\") FROM events GROUP BY \"PadStyle\""
```
That quoting works in Windows cmd and in bash (in PowerShell, run it from cmd). The commands read `.env` too.

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
