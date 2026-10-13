# Privacy: what leaves your computer

Morph Studio runs on your computer. R Cubed runs no server for it: there is no Morph account, no analytics and no
telemetry unless you switch it on. Your projects stay on your disk. This page lists every way data can leave the
machine: when it happens, what is sent, and who keeps it. If you find a network call that isn't here, please report
it ([SECURITY.md](../SECURITY.md)).

## Switches

- **Keys, then Privacy and network** (in the Studio, saved in `projects/.settings.json`):
  - *Allow network lookups* is on by default. Off keeps everything on this computer: chat uses the built-in rules,
    Darwin shows only saved answers, and Keewano and SubQ do nothing.
  - *Send build events to Keewano* and *Use Keewano calibration* are off until you turn them on.
- `MORPH_OFFLINE=1` in the environment turns every network call off, whatever the switches say.
- A service whose key isn't set is never called. Darwin needs no key, so the network switch covers it.

## Installing and updating

- **First launch:** the launcher downloads Python packages from PyPI (pypi.org and files.pythonhosted.org) into `.venv`.
  PyPI sees your IP address and which packages and versions pip asks for; pip also sends your OS and Python
  version. It does this again only after an update changes `requirements.txt` or `constraints.txt`.
- **`--update`:** asks GitHub for the list of releases (`git fetch`) and checks out the newest one. GitHub sees your IP
  address. Other launches use neither git nor PyPI.
- Downloading or cloning Morph from GitHub falls under GitHub's own privacy statement.

## Opening the Studio

The page and everything it needs (scripts, fonts, the 3D viewer) come from Morph on your own computer. It loads
nothing from Google Fonts, a CDN or any other site, and its content security policy blocks such requests.

**Remote mode (`--remote`)** sends the page, your edits, the access code, its cookie and any key you save in the
Keys dialog over plain HTTP between the browser and the computer running Morph. Anyone who can watch that network can
read them. Use it on a network you trust or over a VPN, or use an SSH tunnel instead ([SECURITY.md](../SECURITY.md)).

## Optional services

| Service | When it is called | What it receives | What it keeps |
|---|---|---|---|
| **Anthropic** (Claude) | Only with an Anthropic key (`ANTHROPIC_API_KEY`) and network lookups on: each chat message in the Studio; `morph robot new --need` and `morph new --need`; `morph research`, where Claude also searches the web for your question; **Test keys** and the online `morph doctor`, which only list models. | Your message and the whole current design (every parameter, as JSON); the need text; the research question. | Anthropic deletes API inputs and outputs within 30 days, keeps them up to 2 years if its systems flag a request for a Usage Policy violation, and doesn't train on them by default ([retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data), [privacy policy](https://www.anthropic.com/legal/privacy)). |
| **Keewano** | Only with `KEEWANO_API_KEY` and *Send build events to Keewano* on (or `MORPH_TELEMETRY_ENABLED=1` for a script): every entry Morph adds to a project's ledger. Reading needs `KEEWANO_PAT` and `KEEWANO_PROJECT_ID`: **Test keys**, the online `morph doctor`, `morph ask`, and the grip-force calibration when *Use Keewano calibration* is on. | The project name and an id derived from it; for each revision the object name, pad, servo, material, sensor, the stages recomputed and the predicted and target grip forces; bench measurements; logged human minutes; CLI command names and run times; Darwin lookups (part, number of agents found, top agent, gaps). No design files. Reads send SQL queries about your own Keewano project. | Your Keewano project keeps the events, under your agreement with Keewano ([privacy policy](https://keewano.com/privacy-policy)). |
| **Darwin** | Only with network lookups on, when you click **Find agents** (BOM tab), at step 8 of Present, and for `morph source`, `morph darwin` and the online `morph doctor` check. No key. | Search queries: the names of the parts in the BOM (servos, boards, materials) and the fabrication jobs (PCB from Gerbers, 3D printing, a design review). Files are listed in the local results, never uploaded. | Darwin doesn't associate searches made without a Darwin account with any account; it logs IP addresses, timing and similar diagnostics for as long as it needs them ([privacy policy](https://darwin.so/privacy)). Answers are cached on your computer. |
| **SubQ** | Only with `SUBQ_API_KEY` and network lookups on, for the steps the Studio labels "SubQ Snap (AI)"; `morph subq`; **Test keys** and the online `morph doctor`, which send one tiny question ("Morph key check"). The doctor also asks whether api.subq.ai is up, without a key. | The request text and the state of the design or brief the question is about; photos only if you add them (at most 4, never video). | Requests go to SubQ (Subquadratic Inc.) under your own account. SubQ keeps them per your organisation's retention setting: by default until you delete them, or after N days, or not at all (zero retention, on request). It does not train on API content ([data retention](https://docs.subq.ai/using-the-api/data-retention/)). Answers are cached on your computer for 7 days. |
| **BoardRepo** | Only `morph boardrepo`. | Requests for the public pages of the reference boards it lists; nothing about your design. Uploading a board is something you do yourself, on boardrepo.com. | Nothing of yours. |
| **Gemini** (Google) | Not called by Morph 0.2.0b1. A `GEMINI_API_KEY` is ignored unless a feature that names Gemini is added; this table will list it first. | | Google uses free-tier Gemini API data to improve its products and people may read it; paid-tier data isn't ([terms](https://ai.google.dev/gemini-api/terms)). Use a paid key. |

Each service's own terms govern what it does with data you send it.

## What stays on your computer

| What | Where |
|---|---|
| Projects: designs, generated CAD, simulations, electronics, firmware, manuals, revision labels (they can include your chat requests) and each project's ledger | `projects/` in the Morph folder, or `MORPH_HOME` |
| API keys, in plain text (readable by your user only on macOS and Linux) | `.env` in the Morph folder; an installed wheel uses `%LOCALAPPDATA%\Morph`, `~/Library/Application Support/Morph` or `~/.config/morph` |
| The privacy switches | `projects/.settings.json` |
| Each project's chat: your messages and the replies | `projects/<project>/.morph/chat.jsonl` |
| Deleted projects (moved, not erased) | `projects/.trash/` |
| Cached answers from Darwin, BoardRepo and SubQ | `~/.cache/morph/` (on Windows, `%USERPROFILE%\.cache\morph`) |

**Export** (next to the revision number) zips one revision's engineering files with a manifest. It never includes
keys, settings, chat, the ledger or hidden files. Review the design before you share it: names, dimensions and the
request text can still be confidential.

To remove everything, delete the Morph folder and `~/.cache/morph`, and revoke your keys with each provider
([INSTALL.md](INSTALL.md#uninstalling)).
