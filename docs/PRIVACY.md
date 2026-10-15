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
  - *Let agents use tools (beta)* is on, but an agent run (Live prices) starts only when someone presses its button.
    Off: no run starts, and a run under way stops before its next step. *Check live prices after a build changes the
    purchased parts* (it starts Live prices by itself) is off until you turn it on.
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
**Team mode (`--team`)** is the same over the network: invite and sign-in links, session cookies and everything the
team does cross it as plain HTTP unless the Studio sits behind HTTPS (a tunnel or VPN).

## Optional services

| Service | When it is called | What it receives | What it keeps |
|---|---|---|---|
| **Anthropic** (Claude) | Only with an Anthropic key (`ANTHROPIC_API_KEY`) and network lookups on: each chat message in the Studio, except one Morph answers by offering a new robot or an import instead; what you type in **New robot** and `morph robot new "<request>"`; **Show Morph the job**, only after you agree to send the frames or photos and press Analyze; World mode's **AI review** button; `morph robot new NAME --need` and `morph new --need`; `morph research`, where Claude also searches the web for your question; **Live prices** (beta: the BOM tab's button, or after a build when *Check live prices after a build changes the purchased parts* is on) and **Draft a tool** (preview, the owner only), both only with *Let agents use tools* on; **Test keys** and the online `morph doctor`, which only list models. | Your message and the whole current design (every parameter, as JSON); the New robot request and your answers; the still frames or photos you chose (at most 12, re-encoded without EXIF or location) with your marks, never the video or its sound; six rendered views of the room and robot with the measured checks; the need text; the research question; for Live prices, the revision's purchased lines (item, category, quantity, Morph's estimated price and vendor; at most 40) and what each tool answered, cut to the fields its manifest keeps and with every key removed; to draft a tool, the need, the documentation's address and the tool and key names Morph already uses, while Claude's web fetch reads up to 4 pages of that documentation site (Anthropic's servers fetch them, not your computer). Never a tool's key. | Anthropic deletes API inputs and outputs within 30 days, keeps them up to 2 years if its systems flag a request for a Usage Policy violation, and doesn't train on them by default ([retention](https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data), [privacy policy](https://www.anthropic.com/legal/privacy)). |
| **Keewano** | Only with `KEEWANO_API_KEY` and *Send build events to Keewano* on (or `MORPH_TELEMETRY_ENABLED=1` for a script): every entry Morph adds to a project's ledger. Reading needs `KEEWANO_PAT` and `KEEWANO_PROJECT_ID`: **Test keys**, the online `morph doctor`, `morph ask`, and the grip-force calibration when *Use Keewano calibration* is on. | The project name and an id derived from it; for each revision the object name, pad, servo, material, sensor, the stages recomputed and the predicted and target grip forces; bench measurements; logged human minutes; CLI command names and run times; Darwin lookups (part, number of agents found, top agent, gaps); for **New robot**, the kind of robot, which questions were asked, chip answers, defaults, rounds, why a request was out of scope or refused, the cost estimate and whether the build was feasible (never what you typed); for **Show Morph the job**, video or photos, frame counts, timings, which AI answered, its cost, the decoder and how many changes you applied; for **Import**, the kind of thing, its format, files or a link, whether you picked the motors and how many warnings it had (never file names, links or robot names); for **World mode**, the kind of room, how many boxes and people areas it has, the measured verdict, which review ran and how long the check took; for SubQ, which decision it was asked, whether the answer was cached, the time and input tokens. Team activity is never sent. No design files, frames or rooms. Reads send SQL queries about your own Keewano project. | Your Keewano project keeps the events, under your agreement with Keewano ([privacy policy](https://keewano.com/privacy-policy)). |
| **Darwin** | Only with network lookups on, when you click **Find agents** (BOM tab), after a **New robot**'s first build (sourcing on, the default), at step 8 of Present, and for `morph source`, `morph darwin` and the online `morph doctor` check; during Live prices, when Claude looks for a shop for a part no distributor tool covers; on a card for a need no tool covers, for suggestions. No key. | Search queries: the names of the parts in the BOM (servos, boards, materials) and the fabrication jobs (PCB from Gerbers, 3D printing, a design review); the few words Claude searches for (a part or a job); the need on the card. Files are listed in the local results, never uploaded. Morph never signs in to Darwin, answers its questions for you or pays. | Darwin doesn't associate searches made without a Darwin account with any account; it logs IP addresses, timing and similar diagnostics for as long as it needs them ([privacy policy](https://darwin.so/privacy)). Answers are cached on your computer. |
| **SubQ** | Only with `SUBQ_API_KEY` and network lookups on, for the steps the Studio labels "SubQ Snap (AI)": the first look at a **New robot** request (also `morph robot new "<request>"`); a chat message whose wording can't tell whether it asks for a new robot or a change to the open one; the *Second opinion (beta)* switch in **Show Morph the job** (offered only when the frames go to Claude); World mode's *also SubQ* box; `morph subq`; **Test keys** and the online `morph doctor`, which send one tiny question ("Morph key check"). The doctor also asks whether api.subq.ai is up, without a key. | The request text and the state of the design or brief the question is about (for a chat message: the message, the open project's name and its robot source); images only from a feature you used (at most 4 frames, photos or rendered views, never video). | Requests go to SubQ (Subquadratic Inc.) under your own account. SubQ keeps them per your organisation's retention setting: by default until you delete them, or after N days, or not at all (zero retention, on request). It does not train on API content ([data retention](https://docs.subq.ai/using-the-api/data-retention/)). Answers are cached on your computer for 7 days. |
| **Distributors and other agent tools** (beta: Mouser; Farnell, element14 and Newark, off until you turn it on; tools the owner adds or approves) | Only during an agent run, with *Let agents use tools* and network lookups on, only for tools that are on (Keys, *Tools agents can use*), and only after you said yes: by saving that service's key in Keys (for Morph's own read-only tools) or by allowing the tool on its card. A tool that needs a key never runs without it; one that writes or uploads asks before every call; one that spends money is refused in this beta. | What the tool's card says it sends, to its own host only: for the distributor searches, a keyword or manufacturer part number from your parts list (and the store, for Farnell), with your key for that service. Never your design, files, chat, other keys or the rest of the parts list. | Each provider's own terms. What each tool answers (cut to the fields its manifest keeps, without keys) goes to Claude and stays in the run's file for up to 7 days. Farnell's API terms limit storing its data and passing it on, and haven't had a legal review yet, so Morph ships its tool off: turn it on only if your agreement with Farnell allows that. What a provider does with data you approved sending is up to its terms: Morph can't control it. |
| **GitHub, Hugging Face** | Only when you paste a link in **Import** (or `morph import` with a link), with network lookups on. On a Studio other computers use, links are off unless it was started with `--allow-url-import`. | Requests for the files of that public repository (your IP address). | Their own privacy statements apply. Nothing is uploaded. |
| **BoardRepo** | Only `morph boardrepo`. | Requests for the public pages of the reference boards it lists; nothing about your design. Uploading a board is something you do yourself, on boardrepo.com. | Nothing of yours. |
| **Gemini** (Google) | Not called by Morph 0.2.0b1. A `GEMINI_API_KEY` is ignored unless a feature that names Gemini is added; this table will list it first. | | Google uses free-tier Gemini API data to improve its products and people may read it; paid-tier data isn't ([terms](https://ai.google.dev/gemini-api/terms)). Use a paid key. |

Each service's own terms govern what it does with data you send it.

## What stays on your computer

| What | Where |
|---|---|
| Projects: designs, generated CAD, simulations, electronics, firmware, manuals, revision labels (they can include your chat requests) and each project's ledger | `projects/` in the Morph folder, or `MORPH_HOME`; an installed wheel uses `projects` in `%LOCALAPPDATA%\Morph`, `~/Library/Application Support/Morph` or `~/.local/share/morph` (`morph doctor` prints it), and the `projects/` paths below are inside it there |
| API keys, in plain text (readable by your user only on macOS and Linux) | `.env` in the Morph folder; an installed wheel uses `%LOCALAPPDATA%\Morph`, `~/Library/Application Support/Morph` or `~/.config/morph` |
| The privacy switches | `projects/.settings.json` |
| Each project's chat: your messages and the replies | `projects/<project>/.morph/chat.jsonl` |
| Deleted projects (moved, not erased) | `projects/.trash/` |
| Cached answers from Darwin, BoardRepo and SubQ | `~/.cache/morph/` (on Windows, `%USERPROFILE%\.cache\morph`) |
| New robot drafts (your request and answers), deleted after 7 days or once built; the final brief of a project | `projects/.briefs/`; `projects/<project>/brief.json` |
| Imported robots with their licence and source, and import sessions (cleared after a day) | `projects/.robots/`; `projects/.imports/` |
| World mode's room, including an uploaded room scan and each saved version of the room | `projects/<project>/world/` |
| Teams: members, hashed invite codes and tokens, the audit log (with sign-in IP addresses); comments, change requests, reviews and releases | `projects/.team/`; `projects/<project>/.team/` |
| Agent runs (beta): the conversation with Claude and what each tool answered (cut down, without keys), deleted 7 days after the last change | `projects/.runs/` |
| Agent tools: which are on, your approvals, your own and drafted manifests, drafts (never the documentation's text), needs no tool covered, and their log (names, hosts, outcomes and timings; never a key, a tool's arguments or an answer) | `projects/.tools/` |

**Export** (next to the revision number) zips one revision's engineering files with a manifest. It never includes
keys, settings, chat, the ledger or hidden files. Review the design before you share it: names, dimensions and the
request text can still be confidential. In a team Studio it also carries the team's record as `team.json`, with
members' names: the approvals, release gate and releases of that revision, or, for a project's whole history, its
comments, change requests, reviews, branches and releases. It never carries member ids, invite codes, tokens or the
audit log.

To remove everything, delete the Morph folder and `~/.cache/morph`, and revoke your keys with each provider
([INSTALL.md](INSTALL.md#uninstalling)).
