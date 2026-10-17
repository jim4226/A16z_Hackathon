# Integrations: Keewano and Darwin

Both integrations use only the Python standard library, so nothing new to install. With no keys,
both still work: Darwin Search needs no account, and Keewano switches itself off. Both follow the
**Privacy and network** switches in the Keys dialog: with network lookups off, Darwin shows saved
answers only; Keewano receives events only with **Send build events to Keewano** on, and tunes the
grip force only with **Use Keewano calibration** on. Both Keewano switches are off by default.

## In the Studio

| Where | What it shows |
|---|---|
| **BOM & Sourcing → Darwin agents** | One click (**Find agents on Darwin**) asks Darwin Search for an agent per purchased BOM line (servos, controller, supply, sensors, a fastener kit, servo cables), plus the fabrication handoffs: PCB fab from the Gerbers (custom PCB builds), 3D printing from the STLs, CNC from the DXFs and sheet metal from the flat patterns (pilot builds), and a design review. ★ marks the agent Morph routes that work to (`PREFERRED` in `morph/darwin.py`). Results stream in as each search returns and are saved to `<rev>/sourcing_darwin.json`. |
| **Inspector → Grip force window** | The Keewano line. With **Use Keewano calibration** on and at least 3 logged builds for the robot's pad and material (`synthetic-*` projects never count), the simulated holding force is scaled by logged/predicted (for example ×1.12 from 8 contour/PETG builds) before the grip target and firmware limit are set. The line calls the history seeded demo data unless your project holds real bench results. |
| **Present mode, steps 8 and 9** | Darwin and Keewano each get a step with a caption. |

With **Send build events to Keewano** on, every revision, chat edit, Darwin lookup and bench
measurement is sent to Keewano as typed events (`Project.log()` → `morph/keewano.py`). Scripts can opt
in with `MORPH_TELEMETRY_ENABLED=1`; keys alone never send anything.

## Keys (Keewano only; Darwin needs none)

Easiest: click **Keys** (top right of the Studio), paste the three Keewano values and click **Save
keys**. The dialog then tests them (a read-only query) and says whether they work. They are written
to `.env` in the repo folder (git-ignored), apply at once, and are never shown again.

Or edit `.env` yourself (the first launch creates it from `env.example`):

| Variable | What it is | Where in Keewano |
|---|---|---|
| `KEEWANO_API_KEY` | project key of type **SDK** (the server type is rejected) | Settings → Project → API Keys |
| `KEEWANO_PAT` | personal access token (`kwpat-...`) | Settings → Personal → Security |
| `KEEWANO_PROJECT_ID` | project UUID | Settings → Details → Project ID |

`.env` is read at every start (`run_studio.bat` needs nothing else). Keys set in the terminal win
over the file.

Check, in a terminal set up as in [Commands in a terminal](../README.md#commands-in-a-terminal): `morph doctor` tests
every key that is set, or `morph ask "SELECT count(*) FROM events"` prints a number.

**If the keys are for a project other than R cubed**, it has no build history yet, so calibration has
nothing to use until builds are logged (`morph measure`, or `morph bench` on real hardware).
`python tools/keewano_backfill.py --send` loads two weeks of `synthetic-*` builds for dashboards; their
force ratios are invented and the calibration never uses them. The R cubed project's history is seeded
demo data too, which is why the Studio calls it logged, not measured.

The grip calibration is applied when a robot is generated, so after turning it on click **Reset demo
and rebuild** in the Keys dialog (shown when calibration is on and the Keewano keys are set; it deletes
chat edits and regenerates both robots in about a minute). Deleting the `projects` folder and starting
again does the same.

## Darwin's roles

Darwin is a provider Morph asks, not the layer Morph's agent tools run on ([TOOLS.md](TOOLS.md)). It has three jobs:

1. **The sourcing shortlist** (BOM tab, **Find agents on Darwin**; `morph source`): an agent per purchased BOM line and
   per fabrication handoff, as above.
2. **Inside Live prices** (beta): for a part no distributor tool covers, Claude can search Darwin (`darwin_search`: up
   to 5 agents from its public search, cached, no retry). Darwin's status and any question it asks, such as an email
   address, are passed on to you and never answered for you.
3. **Suggestions for a need no tool covers:** the chat card "The agent needs a tool Morph doesn't have" lists up to 3
   services Darwin's public search knows for that need, with each one's public website and whether it needs a sign-in
   or payment (cached for 7 days). They are suggestions only: Morph never connects to a suggested service, signs in or
   pays. The owner can draft a tool from that service's own documentation instead (*Morph writes a tool*, preview).

None of them needs a key, and all three follow **Allow network lookups**: off, Live prices doesn't run and the other two
show saved answers only.
Darwin's own agents (Act) need your Darwin account and your approval of any payment; Morph doesn't use them yet.

## Darwin when its API is down

Darwin's API returns intermittent 500s. Every good answer is cached in `~/.cache/morph/darwin/`, and
the repo ships answers for the demo's queries in `data/darwin-cache/`. When a live search fails, the
cached answer is used and labelled **cached**.

## Command line

Darwin's shortlist for the latest revision, and a Darwin search:

```shell
python -m morph.cli source egg-picker
python -m morph.cli darwin "PCB fabrication quote from Gerber files"
```

SQL on the Keewano events names its columns in double quotes, which each shell passes on its own way. On macOS and
Linux:

```sh
python -m morph.cli ask 'SELECT "PadStyle", avg("HoldForceRatioPct") FROM events GROUP BY "PadStyle"'
```

In Command Prompt:

```cmd
python -m morph.cli ask "SELECT \"PadStyle\", avg(\"HoldForceRatioPct\") FROM events GROUP BY \"PadStyle\""
```

In PowerShell (`--%` passes the rest of the line on as typed):

```powershell
python --% -m morph.cli ask "SELECT \"PadStyle\", avg(\"HoldForceRatioPct\") FROM events GROUP BY \"PadStyle\""
```

The commands read `.env` too.

## Morph never pays

Morph's Darwin calls are Darwin's free public Search: no Darwin account, no payment method, no paid tool. Ordering
always stops at the shortlist: Darwin Act needs your own Darwin account and your approval of any payment, outside
Morph, and Morph never claims an order was placed. (The hackathon's notes on using Darwin and Keewano from Claude Code,
a separate tool, are in `docs/internal/MCP_SERVERS.md`; Morph doesn't need them.)
