# Morph Studio v0.2 beta: build contracts

Shared rules for everyone building the v0.2 beta in parallel. If your work needs something that
contradicts this file, stop and say so in your report instead of improvising. Research behind these
decisions lives outside the repo; the essentials are summarized here.

## 0. Release goals and non-negotiables

- **Release:** v0.2.0 beta, distributed as a tagged GitHub release with the launchers (`run_studio.bat`,
  `run_studio.sh`). `pip install -e .` keeps working. No PyPI publish in this release.
- **Features in scope:** foundations and security fixes; a new robot from an ambiguous one-line request with
  follow-up questions; import anything; video and photo uploads; SubQ integration; Teams ("GitHub for
  physical things", preview); World mode (beta).
- **Offline first.** Every feature works with no API keys: Claude, SubQ, Keewano, Gemini and Darwin are
  optional. Every model call has a timeout, a fallback, and a `source` label (`subq`, `claude`, `rules`,
  `cache`, `user`) shown in the UI.
- **Models propose, code decides.** Model output is schema-validated and clamped to the capability
  envelope (section 6) before it touches a design. A model never chooses file paths, `arm.source` or
  project names.
- **Platforms:** Windows, macOS and Linux; Python 3.10 to 3.14; no GPU. Open text files with an explicit
  `encoding="utf-8"`. Don't print non-ASCII symbols such as ✓ or ✗ to stdout without a fallback.
- **Dependencies:** no new *required* runtime dependency. Optional extras are allowed when justified
  (`morph[video]` = imageio-ffmpeg, `morph[cad]` = cascadio, `morph[xacro]` = xacro + PyYAML). Never
  bundle GPL binaries.
- **Honesty.**
  - The UI never overstates: "estimated", "seeded demo data", "beta" and "not bench-validated" where true.
  - Never claim an order was placed.
  - Never identify people in images.
- **Security.** Treat every upload, URL, URDF, zip and model output as hostile (section 9).
- **Style.**
  - Match the surrounding code: dense, few comments, plain English in UI copy, no emoji.
  - Tests go under `tests/` and must pass offline: no network, no keys.
  - Run `node --check` on every JS file you touch.
- **Don't rebuild the demo robots by accident.** `pipeline_version()` hashes pipeline code only: every
  `morph/robot/*.py` plus `PIPELINE_MODULES` in pipeline2.py (spec, design, analysis, electronics, sim,
  mechanism, fab, materials, pipeline) and, for an imported robot, its bundle files. Put new Studio/service
  modules in `morph/studio/` or `morph/services/`, never as top-level `morph/*.py` unless they are pipeline
  code; editing keewano.py (event ids), settings, keys or the CLI rebuilds nothing.

## 1. Storage layout (MORPH_HOME, default `projects/` in the repo folder)

| Path | Owner | Contents |
|---|---|---|
| `<p>/meta.json` | W0b | `{kind:"robot", source, need, origin: default\|brief\|import\|fork, demo: bool, created_utc, created_by?, forked_from?: {project, rev}, brief_id?}`. `projects.create(meta=)` adds keys but never sets kind, created_utc, created_by, origin, demo or source (those come from its own arguments) |
| `<p>/rNNN/` | pipeline | unchanged; names sort numerically (W0b) |
| `<p>/ledger.jsonl` | pipeline | unchanged; new entries carry `ts` in UTC with a `Z` suffix, plus `by` (member id) when Teams is on |
| `<p>/.morph/chat.jsonl` | W0b | persisted chat for the project room |
| `<p>/brief.json` | F1 | the final brief that created the project |
| `<p>/world/world.json` + assets | F5 | world mode |
| `<p>/.team/comments.jsonl`, `reviews.jsonl`, `proposals.jsonl`, `branches.json`, `releases.json` | F4 | Teams |
| `.briefs/<id>.json` | F1 | brief drafts (deleted after 7 days) |
| `.imports/<id>/` | F2 | import sessions (cleared at startup after 24 h) |
| `.uploads/` | W0a | temporary uploads: `Request.upload_path(suffix)` names a new file there for `Request.save()`; move the file where it belongs or delete it. The Studio clears entries older than an hour when it starts |
| `.trash/<name>-<UTC time>/` | W0b | deleted projects, moved whole (never erased by Morph) |
| `.robots/<name>/` | F2 | user-imported robot bundles (`morph_profile.json`, URDF/MJCF, meshes, LICENSE). `arm.source` resolves the shipped `robots/` first, then `.robots/` |
| `.team/` | F4 | `team.json`, `secret` (0600), `audit.jsonl` |

Folders starting with `.` are never projects: `projects()` and `reset()` must skip them.

## 2. Server routing contract (W0b implements; everyone else uses)

`morph/studio/routes.py`:

```python
route(method, path, *, role="editor", raw_body=False, max_body=1 << 20)  # decorator
# path: exact ("/api/brief") or prefix ("/api/import/*"). role: "viewer" | "editor" | "owner".
# raw_body=True: the handler reads req.handler.rfile itself (streamed uploads, bounded by max_body).

class Request:
    method: str; path: str; query: dict[str, list[str]]; headers
    body: dict | None        # parsed JSON object (None for raw_body routes)
    studio: Studio
    room: Room               # from ?project= (validated name), default = the start project
    member: dict | None      # {"id","name","role","disciplines"} when Teams is on, else None
    handler: BaseHTTPRequestHandler

# A handler returns one of:
#   (status, dict | list)          -> JSON
#   (status, bytes, content_type)  -> raw
#   None                           -> the handler already wrote the response
```

- Every request passes the existing checks first: Host/Origin, the access token or Teams session, the
  body cap and the role check. In remote mode, an address that sent 10 different wrong access codes is
  answered 429 for 15 minutes without its codes being compared.
- POST, PUT and DELETE also need the header `X-Morph-Client` (any non-empty value; the page sends `1`; 403
  without it); `studio.api` sends it.
- Feature modules register their routes in `morph/studio/api_<feature>.py`. `server.py` imports every
  `api_*.py` in that folder at start, so a feature never edits server.py. Registering a method and path
  twice raises.
- JSON bodies are capped at 1 MiB by default. Raise the cap per route only when needed (video frames:
  16 MiB).
- Also in routes.py: `HTTPError(status, message)` (raise it for a JSON error), `Request.arg(name)` (first
  query value), `Request.length` (the checked Content-Length), `Request.stream(chunk)` and
  `Request.save(path)` (raw bodies in 1 MiB chunks; `save` removes the file again if the upload is cut
  off), `Request.upload_path(suffix)` (a new file in `.uploads/`), `set_role_hook(fn)` for Teams:
  `fn(req, role)` returns the member dict or raises `HTTPError(401|403)`. A body that stops arriving answers
  400 ("cut off"). An unexpected exception in a handler answers 500 with the error text.
- Roles of the core routes (Teams' role table starts from these): **viewer**: `GET /api/state`, `/api/rev`,
  `/api/events`, `/api/jobs`, `/api/projects`, `/api/settings`, `/api/export`, `/files/*`, and the old
  `POST /api/project` (it only checks a name); **editor**: `POST /api/chat`, `/api/params`, `/api/jobs/cancel`,
  `/api/jobs/resolve`, `/api/source`, `/api/projects`, `/api/projects/fork`; **owner**: `DELETE /api/projects`,
  `POST /api/reset`, `GET`/`POST /api/keys`, `POST /api/keys/check`, `POST /api/settings`.

| Area | Routes |
|---|---|
| W0b projects | `GET /api/projects` → `{projects, start}`; `POST /api/projects {name?, from: "default"\|"so101"\|"design", design?, need?, brief_id?}` → 202 `{project, job}` (the first build is queued in the new room); `POST /api/projects/fork {project, rev?, name?}` → 201 when copied, 202 `{job}` when rebuilt; `DELETE /api/projects?project=` (owner; moves to `.trash/`; 409 while it builds and for the start project) |
| W0b rooms | Every `/api` route takes `?project=` (default: the start project). `GET /api/state` (adds `start`, `queue`, `building`); `GET /api/rev?rev=`; `GET /api/events`; `POST /api/chat {message, base_rev?}` and `POST /api/params {changes, base_rev?}` → 202 `{job}` (429 when 10 are waiting); `GET /api/jobs`; `POST /api/jobs/cancel {job}`; `POST /api/jobs/resolve {job, action: "apply"\|"drop"}`; files at `/files/<project>/<rev>/...` (`/files/<rev>/...` is the start project) |
| F1 brief | `POST /api/brief {text?, brief_id?, answers?: {slot: value}, action?: "defaults"\|"build"\|"cancel"}`; `GET /api/brief?id=` |
| F2 import | `POST /api/import {url?}` → `{id}`; `PUT /api/import/<id>/file?path=` (raw, ≤300 MB); `POST /api/import/<id>/detect`; `GET /api/import/<id>/preview.glb?candidate=`; `POST /api/import/<id>/commit {candidate, name, answers}`; `DELETE /api/import/<id>`; `GET /api/export?project=&rev=&full=` |
| F3 video | `POST /api/video/analyze {frames:[{t_s, jpeg_b64}], marks?, crop?, subq?: bool}` (16 MiB); `PUT /api/video/upload` (raw, ≤500 MB, ffmpeg fallback) → `{frames}`; `POST /api/video/apply {changes}` |
| F4 teams | `GET /api/me`; `GET /api/team`; `POST /api/team/join`, `/invites`, `/revoke`, `/members`, `/policy`; `GET\|POST /api/comments`; `POST /api/comments/resolve`; `GET\|POST /api/proposals` (change requests); `POST /api/proposals/apply`; `GET\|POST /api/reviews`; `POST /api/reviews/decide`; `GET /api/activity`; `GET /api/inbox`; `POST /api/presence`; `GET /api/audit`; `GET\|POST /api/branches`; `GET\|POST /api/releases` |
| F5 world | `GET /api/world?project=`; `POST /api/world` (save world.json); `PUT /api/world/upload` (raw .glb ≤300 MB); `POST /api/world/obstacles/auto`; `POST /api/world/inspect {views:[{name, jpeg_b64}]}` |

### W0b Python API for feature modules

```python
studio.room(name=None) -> Room           # ValueError (bad name), rooms.UnknownProject (a LookupError)
Room: name, p (RobotProject), hub (Hub: send(event), history), jobs (JobQueue), lock, completed(), latest(),
      emit(event)                         # emit: pipeline events, remembered for tabs that open mid-build
studio.queue_chat(room, text, *, base_rev=None, by=None) -> Job                  # rooms.QueueFull when 10 wait
studio.queue_changes(room, changes, *, label, reply=None, source="", user_text=None, base_rev=None, by=None)
studio.queue_build(room, design, *, label="first design", by=None)              # a parentless build
studio.create_project(name=None, *, source="", design=None, need=None, origin="default", by=None, meta=None)
    -> (room, job)                        # projects.create + queue_build + project_created to every tab
studio.fork_project(project, rev=None, name=None, *, by=None) -> (room, job | None, info)
studio.delete_project(name) -> trash folder; studio.broadcast(event)
Job: id, kind, by, status, base_rev, result (new rev), wait(timeout), info(position)
projects (morph/studio/projects.py): create(home, name=None, *, source, design, need, origin, demo, by,
    unique=True, rename=True, meta=None) -> {name, root, design, meta}; fork(); delete(); list_projects(home);
    slug(text); unique_name(home, base); read_meta(root); write_meta(root, meta); check_design(design)
```

A job applies its changes to the newest revision when its turn comes. If one of its keys changed after its
`base_rev` (default: the newest revision when it was queued), it ends as `conflict` instead. Every changed value
(chat, Claude's tool output, parameters, a resolved conflict) must pass `security.design_values()` first, and
`projects.create()` checks every value of a design it is given; a value no robot can have ends the job as `error`
("Couldn't apply that: ...") or raises ValueError. `projects.as_design()` refuses keys a Design doesn't have.

## 3. Live events (SSE)

- Every event carries `project`. With Teams on, events about a person's request (`user`, `assistant`, the
  jobs in `queue`) also carry `by`: their member id; tabs look up the name and colour from the team. An event
  with `to` (a member id) reaches only that member's tabs and is never saved in the chat history.
- `user` and `assistant` events carry the `job` they belong to. A conflict is an `assistant` event with
  `conflict {job, keys, base_rev, rev}`: the change was not applied because `keys` changed after
  `base_rev`; the tab offers "Apply to rev" or "Drop it" (`POST /api/jobs/resolve`).
- Existing events: `user`, `assistant`, `start`, `node`, `done`, `sourcing`, `project`, and `resync`: a tab
  that stopped reading fell more than 500 events behind, lost the oldest ones and should reload its state.
- New events:
  - `brief {id, status, round, questions, slots, assumptions, scope, preflight}`
  - `agent {role, provider, status: "start"\|"done"\|"error"\|"skipped", ms, text}`, the agents timeline
  - `project_created {project, origin}` and `project_deleted {project}`: sent to every tab
  - `project {project, reset: true}`: Reset demo changed this project; tabs on it reload
  - `import {id, stage, status, text}`
  - `video {id, stage, status, text, result?}`
  - `queue {jobs:[{id, by, status, position, kind, label, base_rev, rev?, conflict?}]}`: the running job
    (position 0), the waiting ones (1, 2, ...) and, once, a job that just ended (position null) with its
    final status: done, noop, error, conflict, cancelled, dropped or resolved
  - `presence {members}`
  - `comment`, `proposal`, `review`, `activity`, `inbox` (addressed to one member only)
  - `world {stage, status, text, result?}`
- Agent roles for `agent` events: `triage` (SubQ), `designer` (Claude), `feasibility` (code),
  `pipeline`, `sourcing` (Darwin), `memory` (Keewano), `vision` (Claude/SubQ on frames), `reviewer`.

## 4. Front-end feature contract (W0b implements in `app.js`)

`app.js` builds one object and passes it to every feature module's `init(studio)`. Feature modules live
in `static/<feature>.js` and export `init(studio)`.

```js
studio = {
  S,                                    // app state (read-only for features)
  project(),                            // the project this tab is on
  api: { get(path, params), post(path, body), put(path, blob, params), del(path, params) },  // adds ?project= and X-Morph-Client
  on(type, fn),                         // SSE subscription by event type; returns an unsubscribe function
  toast(text), addMsg(role, text, opts), esc, I,          // existing helpers
  modal: { open(el | html, {wide?}), close() },          // one shared dialog layer (reuse keys.js styles)
  openProject(name), reload(),
  mount: { header, projMenu, chatTools, inspectorTabs, viewerTools },  // DOM slots features may append to
}
```

- `api` calls resolve to the parsed JSON (text for other answers) and reject with an Error carrying
  `status` and `body`. `post(path, body)`; `put(path, blob, params)` sends the blob raw; `del(path, params)`
  (a `project` in params wins over this tab's). `on('*', fn)` gets every event. `modal.open()` returns the
  dialog element and takes `{wide, onClose}`. `mount.projMenu` is where "+ New robot" goes (F1); every slot
  is hidden while empty. `mount.chatTools` sits inside the chat form, so its buttons need `type="button"`.
  `window.morph.studio` is the same object, for tests.
- `init(studio)` runs after the first state load (and also when the server can't be reached).
- Features never edit another feature's file.
- Edits to `app.js`, `index.html` and `style.css` are limited to one import line and one entry in the
  `FEATURES` array at the top of app.js, and an appended CSS section headed `/* ---- <feature> ---- */`.

## 5. SubQ client (`morph/studio/subq.py`)

Standard library only. It never raises and never logs the key or the user's text.

```python
enabled() -> bool                                   # key set and not disabled by 401/402/403
decide(state, questions, images=None, *, timeout=8.0, retries=1, cache=True, purpose="") -> dict | None
    # {"answers", "usage", "ms", "source": "subq" | "cache"}; None means "use the fallback"
noul(instructions, true=None, false=None); choice(instructions, options: dict); score(instructions, levels: list)
p_yes(res, name) -> float | None; pick(res, name, min_conf=0.6) -> str | None; level(res, name) -> (float, str) | (None, None)
check() -> {"ok", "detail"}; status() -> {"configured", "disabled", "last_error", "cooling"}; reset()
lint(body) -> (bool, str)                           # dev/CI only: no key, 401 means valid, 422 returns the message
```

- **Endpoint:** `POST https://api.subq.ai/v1/systemone`, `model: "subq-snap-preview"`,
  `Authorization: Bearer <SUBQ_API_KEY>`. Questions are `noul`, `choice` (2 to 255 options) or `score`
  (2 to 10 levels).
- **Images:** at most 4, `{"mediaType", "base64"}` with no `data:` prefix, ≤4 MiB each and ≤8 MiB in total.
  SubQ takes no video.
- **Retries:** retry 429/503/timeouts once with backoff. 401/402/403 disable SubQ until the key changes.
  422 is never retried. Any other failure that remains after the retry (503, 429, a timeout, an unreachable
  server, an unexpected answer or redirect) pauses SubQ for 60 s.
- **Redirects:** requests carrying the key go through `security.open_keyed()`, which refuses redirects
  (urllib would resend `Authorization` to the new address). Keewano's requests do the same.
- **Cache:** SHA-256 of the request in `~/.cache/morph/subq/` for 7 days. Store answers only. Never cache
  images.
- **Thresholds:** act at `confidence >= 0.6`, treat `noul >= 0.5` as yes; otherwise ask the user. Label
  results "SubQ Snap (AI)".
- **Keys entry:** `SUBQ_API_KEY`, service "SubQ", where to get it: console.subq.ai.
- **Also:** `enabled()` is also false while network lookups are off. `timeout` is the budget for all tries
  together. Builders never raise: `decide()` checks the questions and images and returns None for an invalid
  request. `health()` (GET /health, for `morph doctor`), `LABEL` ("SubQ Snap (AI)"), `CONFIDENCE` (0.6) and
  `YES` (0.5). `p_yes`/`pick`/`level` take decide()'s result or its `answers`. `morph subq "<text>"` runs a
  sample triage with your key (`--lint` checks the request without one).
- **Disclosure copy:** "Requests go to SubQ (Subquadratic Inc.) under your own account. SubQ keeps
  requests per your organisation's retention setting (zero retention on request) and does not train on
  API content."

## 6. Capability envelope (`morph/studio/capability.py`, F1 owns; everyone reads)

The single source of truth for what the generator can build. It is generated from the code (arm.py,
analysis.py, PARAMS), not hand-copied, and it feeds the SubQ state text, chip ranges, scope checks and
clamping.

- **Robot:** one stationary tabletop arm, 4/5/6 DOF.
- **Grasp:** a two-finger parallel gripper, top-down pick and place on the table plane.
- **Object width:** about 26 to 75 mm.
- **Payload at 2× torque margin:**

  | | 4-DOF | 5-DOF | 6-DOF |
  |---|---|---|---|
  | hobby_pwm | 200 g | 100 g | none |
  | smart_bus | 300 g | 200 g | 100 g |
  | dynamixel | 500 g | 300 g | 200 g |

- **Reach:** the effective arm is 417 to 617 mm for top-down picks. PARAMS limits `arm.reach_mm` to
  200 to 500.
- **Firmware:** joint-level only. No task program and no vision code.
- **Out of scope:** mobile robots, drones, humanoids, suction grippers, pouring, folding, cooking, placing
  above table height.

API:
- `envelope() -> dict`
- `describe_for_models() -> str`
- `check(design | changes) -> {status: "ok"|"adjusted"|"out_of_scope", notes, nearest}`
- `clamp(changes) -> (changes, notes)`

## 7. Canonical brief (`morph/studio/brief.py`, F1 owns)

SubQ triage, Claude extraction, the offline rules and video analysis all fill this one schema:

```
Brief{id, request, status: asking|ready|building|built|out_of_scope|refused, round 0-3, turns[],
      route{value, confidence, source}, archetype{key, confidence, source},
      slots{name: Slot}, assumptions[], scope{status, notes, nearest}, preflight{issues, ms},
      changes{dotted: value}, project, rev}
Slot{value, source: user|inferred|default|memory|video|photo, confidence, evidence}
```

- **Slots:** `task`, `object` (name, shape, diameter_mm, length_mm, mass_g, friction), `fragility`,
  `layout` (pick/place), `throughput`, `environment`, `people_nearby`, `precision`, `budget`,
  `perception`, `connectivity`, `quantity`, `existing_hardware`.
- **Mapping:** a deterministic `brief_to_changes()` maps slots to design keys. Object presets in
  `intent.OBJECTS` win over model guesses: an egg is 25 N.
- **Questions:** at most 3 rounds, each with ≤3 questions of 2 to 4 chips. "Use sensible defaults" and
  "Build now" are always available.
- **Harm:** a local keyword pre-filter plus SubQ's `harm` noul. Refuse when either fires; build nothing
  and send nothing further.
- **Video and photos** (F3) produce slot values with `source: video|photo`. They go through the same
  brief and confirm card, or through `/api/video/apply` when editing an existing robot.

## 8. Teams ("GitHub for physical things", F4)

| GitHub concept | Morph Teams |
|---|---|
| commit | revision `rNNN` (immutable, attributed: `by`) |
| branch | variant line: a named branch of revisions from a base revision (`branches.json`); `main` is the default |
| pull request | proposal (change request): a set of design changes from a branch or a member, with its preview diff |
| CODEOWNERS | **stage owners by discipline**, below |
| review / approve | review of a proposal or revision by the owners of the affected stages |
| release / tag | release of a revision ("Pilot v1"), gated by required approvals per `build.config` |
| issues / comments | comments anchored to `{rev, design_sha256, kind: rev\|stage\|bom\|param\|file, key}` |
| fork | fork a project (W0b `POST /api/projects/fork`) |

**Disciplines and the stages they own** (stage names from `morph.robot.pipeline2.ORDER`:
sizing, gripper, electronics, cad, sim, wiring, manufacturing, bom, manual):
- `mechanical`: sizing, gripper, cad
- `simulation`: sim
- `electronics`: electronics, wiring
- `manufacturing`: manufacturing
- `sourcing`: bom and Darwin sourcing
- `documentation`: manual

**Routing.** A proposal's affected stages are `morph.robot.pipeline2.dirty_nodes(changed_keys)` (the
same graph the pipeline uses). Reviews are requested automatically from the owners of those disciplines. Example: a
servo swap touches mechanical, electronics, manufacturing and sourcing, so it asks the electronics,
manufacturing and sourcing owners.

**Generalist** (the discipline tag `generalist`, for people who work on every stage, like a founder or
a solo builder):
- **Ownership:** a generalist owns no stage exclusively. They are the **fallback owner** of every
  discipline that has no specialist on the team. A change touching a discipline with no specialist asks
  the generalists; one touching a discipline with a specialist asks that specialist.
- **Watching:** generalists aren't auto-requested on everything (that would flood them). Each member has
  a "Watch all changes" setting, on by default for generalists, which puts every proposal and review in
  their inbox without making them a required reviewer.
- **Approvals:** a generalist's approval counts for any affected discipline, and one approval can cover
  several. The team policy can list disciplines that need a specialist, `specialist_required`, e.g.
  electronics for production. The approve button names what it covers ("Approve as Electronics and
  Manufacturing").
- **Distinct people:** the gate also counts distinct approvers, so one generalist can't ship alone above
  prototype:
  - prototype: 1 approval, and the author may approve
  - pilot: at least 1 approver who isn't the author
  - production: at least 2 distinct approvers who aren't the author, including the owner
- **Defaults:** the team creator starts as owner + generalist. The Share dialog offers "Generalist (all
  stages)" next to the specialist disciplines. Routing chips read like
  "Electronics: Ana · Manufacturing: Jaron (generalist)".

**Roles** (team-wide): owner, editor, viewer. Viewers can comment and propose. Disciplines are tags on
members; a member can hold several.

**Release gate** (by `build.config`):

| Build | Approvals |
|---|---|
| prototype | 1, and the author may approve |
| pilot | every affected discipline's owner; the author never counts |
| production | every affected discipline's owner plus the owner |

**Approvals are per discipline and tied to the exact revision.**
- An approval records `{rev, design_sha256, discipline, stage_artifact_hashes}`.
- On a later revision, a discipline's approval carries forward only if every stage that discipline owns
  was reused unchanged (provenance "reused from rNNN"). Otherwise it is stale and that discipline must
  approve again. This is GitHub's "dismiss stale reviews", scoped to the affected work.

**Flagship acceptance workflow** (the release demo, and an end-to-end test):
1. An editor proposes a servo substitution.
2. The proposal previews what it affects: geometry, torques, wiring, firmware and BOM.
3. Review is routed to the owning disciplines: mechanical, electronics, manufacturing, sourcing; a
   generalist covers any discipline with no specialist.
4. Approvals are recorded against the exact revision, and the approved revision is exported.
5. A later edit makes only the affected disciplines' approvals stale and requires fresh ones.

**Identity.**
- Invite links: single-use, 7-day expiry, 128-bit code stored hashed.
- Signed session cookies: `id.issued.HMAC(secret, id|issued|epoch)`. Revoking bumps the epoch.
- The audit log is hash-chained.

## 9. Security rules for all features

- **Paths.** Canonicalize with `os.path.realpath` and check containment with `os.path.commonpath`.
  Never a string-prefix check. Reject names that start with `.`, contain separators, or fail
  `^[a-z0-9][a-z0-9-]{0,39}$` (projects) or `^[a-z0-9][a-z0-9_-]{0,47}$` (robot sources).
- **Uploads.**
  - Stream to disk in 1 MiB chunks and check `Content-Length` before reading.
  - Sniff magic bytes; never trust extensions.
  - Zips: ≤2,000 entries, ≤300 MB total, ≤100 MB per file, ≤100× compression ratio. No symlinks, no
    nested archives, no absolute or `..` paths.
  - XML: reject `<!DOCTYPE`/`<!ENTITY`; cap at 20 MB.
  - Meshes: ≤1.5 M faces.
- **URL fetch.**
  - HTTPS only. Allowlist github.com, codeload.github.com, raw.githubusercontent.com, huggingface.co
    and *.hf.co.
  - Reject private, loopback and link-local IPs on every redirect hop; at most 3 redirects.
  - ≤300 MB, 30 s timeout.
  - Off in remote/team mode unless started with `--allow-url-import`.
- **Subprocesses** (xacro, ffmpeg): an argument list (never a shell), `env={}` plus PATH, a timeout,
  and output caps.
- **Untrusted text.** Model inputs built from READMEs, comments or files are data, never instructions.
  Model outputs never set paths.
- **Design values.** A design or change from a model, a request, an import or a video passes
  `security.design_values()` (type, finite, and `DESIGN_LIMITS`, far wider than PARAMS) before it is built:
  pipeline work grows with sizes, and one absurd value (a 1 km object) would hold the shared build slot for
  hours. The capability envelope (section 6) clamps much tighter.
- **Keys.** Requests carrying an API key use `security.open_keyed()` (never follow a redirect with the key).
  Chat messages that look like a key (`looks_like_secret`) are refused before they are shown, saved or sent.
- **Served files.** `/files` and `/static` serve regular files only (never a pipe or device); generated files
  under `/files` get the sandbox policy, so they can't run script with the Studio's origin.

## 10. Keewano event id ranges

`CUSTOM_EVENTS` is append-only; ids never change type.

| Area | Ids |
|---|---|
| W0 | 2022–2099 |
| brief | 2100–2129 |
| import | 2130–2159 |
| video | 2160–2179 |
| teams | 2180–2229 (pseudonymous: hashed member ids, never names) |
| world | 2230–2259 |
| subq | 2260–2279 |

Telemetry is opt-in (W0a).

## 11. File ownership (create only your files; shared files only as section 4 allows)

| Work package | Files |
|---|---|
| W0a | `morph/studio/security.py`, `morph/settings.py` (non-secret prefs), `morph/resources.py`, `morph/studio/export.py`, release docs (SECURITY, PRIVACY, INSTALL, CHANGELOG, THIRD_PARTY_NOTICES), CI, launchers, `requirements.lock`, and the security/release tests. Server hardening edits. |
| W0b | `morph/studio/routes.py`, `morph/studio/rooms.py`, `morph/studio/projects.py`, `morph/studio/subq.py`. The rooms/queue/router refactor of `server.py` and `app.js`, plus the feature contract. |
| F1 | `morph/studio/brief.py`, `morph/studio/capability.py`, `morph/studio/agents.py` (provider chain), `morph/studio/api_brief.py`, `static/newrobot.js`, `tests/test_brief*.py` |
| F2 | `morph/importers/` (package: detect, urdf, autoprofile, mjcf, xacro, mesh_object, bom, zipsafe, fetch, project_zip), `morph/studio/api_import.py`, `static/importer.js`, `tests/test_import*.py`. Minimal edits to `urdf_import.py` and `imported.py` for containment and user robots. |
| F3 | `morph/studio/vision.py`, `morph/studio/api_video.py`, `morph/studio/ffmpeg.py`, `static/video.js`, `tests/test_video*.py` |
| F4 | `morph/studio/teams/` (package), `morph/studio/api_team.py`, `static/team.js`, `tests/test_team*.py` |
| F5 | `morph/studio/worlds.py`, `morph/studio/api_world.py`, `static/world.js`, the vendored three r160 examples (PointerLockControls, Octree, Capsule, TransformControls) in `static/vendor/`, `simulate.py` world support, `tests/test_world*.py` |

## 12. Correctness fixes from the external review (owner: W0c, after Wave 0)

1. **Protocol validation (P1).**
   - **Bug:** `electronics2.plan()` checks only the electrical interface. Feetech and Dynamixel servos are
     both `ttl_half_duplex` but speak different protocols, so `arm.actuator_overrides=j2=xm430_w350`
     passes validation and the firmware drives a Dynamixel servo with Feetech SCServo commands.
   - **Fix:** validate the protocol of every selected actuator. Reject mixed-protocol buses with a clear
     issue, or implement them. Add tests.
2. **Moving accessories (P2).**
   - **Bug:** `electronics.extras` (wrist_camera, imu) ride on the moving gripper and change the hand
     mass, but `dirty_nodes` excludes sizing and sim, so stale sizing and simulations are reused.
   - **Fix:** include moving accessories in the sizing hand mass, and make `electronics.extras` dirty
     sizing and sim. Teams routing then asks the simulation reviewers too.
3. **Refresh during a build (P2).**
   - **Bug:** `Studio.state()` reads `latest()` while a revision is still being built (design.json
     without results.json), raising FileNotFoundError. The page then shows offline and never subscribes
     to events.
   - **Fix:** publish only completed revisions, plus an explicit build status (rev in progress, current
     node). The front end subscribes to events even if loading state fails, and retries.
4. **Pending chat follows the project switch (P1).** A chat interpreted before a project switch must
   apply to the project it was sent from. Rooms capture the room at enqueue time and interpret inside
   the room's worker (W0b), with a test.
5. **Stage reuse hides failures (P1).** A failed stage is never laundered into feasible by a later
   unrelated edit (W0a-1 ports the fix), with a test.
