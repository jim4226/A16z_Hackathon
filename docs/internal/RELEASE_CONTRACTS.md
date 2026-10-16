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
| `<p>/world/world.json` + assets, `<p>/world/saved/<sha>.json` | F5 | world mode; every saved world is also kept under its hash, and a design's `world.sha` picks the one its simulation runs in (I2b) |
| `<p>/.studies/tolerances.json`, `<p>/.studies/active.json`, `<p>/.studies/<id>/study.json`, `runs.jsonl`, `checks.jsonl` | B2-S1 | robustness studies (section 16): the project's edited tolerances, the running study's lock and heartbeat, each study's record and one row per run; never inside `rNNN/` |
| `<p>/.team/comments.jsonl`, `reviews.jsonl`, `proposals.jsonl`, `branches.json`, `releases.json` | F4 | Teams |
| `.briefs/<id>.json` | F1 | brief drafts (deleted after 7 days) |
| `.imports/<id>/` | F2 | import sessions (cleared at startup after 24 h) |
| `.uploads/` | W0a | temporary uploads: `Request.upload_path(suffix)` names a new file there for `Request.save()`; move the file where it belongs or delete it. The Studio clears entries older than an hour when it starts |
| `.trash/<name>-<UTC time>/` | W0b | deleted projects, moved whole (never erased by Morph) |
| `.robots/<name>/` | F2 | user-imported robot bundles (`morph_profile.json`, URDF/MJCF, meshes, LICENSE). `arm.source` resolves the shipped `robots/` first, then `.robots/` |
| `.team/` | F4 | `team.json`, `secret` (0600), `audit.jsonl` |
| `.runs/<id>.json` + `<id>.lock` | B2-T1 | agent runs (section 15): 0600, deleted at Studio start 7 days after their last change |
| `.tools/` | B2-T1, B2-T3 | agent tools (sections 15 and 15.2): `catalog/*.json` (the owner's manifests), `state.json`, `approvals.json`, `pins.json`, `drafts/`, `requests.jsonl`, `audit.jsonl` + `audit.head` |

Folders starting with `.` are never projects: `projects()` and `reset()` must skip them.

## 2. Server routing contract (W0b implements; everyone else uses)

`morph/studio/routes.py`:

```python
route(method, path, *, role="editor", raw_body=False, max_body=1 << 20)  # decorator
# path: exact ("/api/brief") or prefix ("/api/import/*"). role: "viewer" | "editor" | "owner", or "public" (the Teams
# join form only: a team Studio answers it before sign-in, without X-Morph-Client; other Studios check it as usual).
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
- A connection has `HEADER_TIMEOUT` (20 s) in all for its request line and headers. The server serves at most
  `MAX_CONNECTIONS` (64) connections at once, `MAX_STREAMS` (48) of them `/api/events` streams; past either cap the
  answer is 503 at once. A stream notices a closed tab within a second and frees its slot. A route that holds its
  connection open for long (a stream of its own) should take a slot the same way (`req.handler.server.open_stream()` /
  `close_stream()`).
- Feature modules register their routes in `morph/studio/api_<feature>.py`. `server.py` imports every
  `api_*.py` in that folder at start, so a feature never edits server.py. Registering a method and path
  twice raises. Each module registers itself when imported, with the Studio's hooks (I2a): `api_brief` with the
  project service (`_studio_create`), `api_import`, `api_video`, `api_world`, and `api_team`, whose routes answer
  only on a team Studio (`GET /api/me` says `{team: false}` elsewhere, every other Teams route is 404).
- **Team mode** (`morph studio --team`, `Studio(team=True)`): a member's session cookie or `X-Morph-Token` script
  token signs every request in (`MORPH_TOKEN` plays no part); the server calls `api_team.role_hook(req, role)` for
  every route, which checks the Teams role table (`teams/roles.py`; a declared role can only make it stricter) and
  raises 401 or 403. The hook is per Studio, so `routes.set_role_hook` stays a global hook for tests. `/?invite=` and
  `/?link=` get the join and sign-in pages before sign-in; a page load without a session gets the team's sign-in
  page; `/files` checks gated order packages (`api_team.check_download`), and `/api/export` applies the same policy
  (`api_team.export_parts`: an unreleased revision's order files stay out of the export of a member who may not download
  them, listed in the manifest's `left_out`) and adds `team.json` (`collab.revision_record` for a revision,
  `collab.export_bundle` for a project's history; names, never member ids); the event stream feeds presence; keys,
  settings, export, reset, project deletion and imports (`api_import._audit`, the link without its query) go into the
  team's audit log.
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
| W0b rooms | Every `/api` route takes `?project=` (default: the start project). `GET /api/state` (adds `start`, `queue`, `building`); `GET /api/rev?rev=`; `GET /api/events`; `POST /api/chat {message, base_rev?, edit?}` and `POST /api/params {changes, base_rev?}` → 202 `{job}` (429 when 10 are waiting; `edit: true` skips the chat routing of section 3); `GET /api/jobs`; `POST /api/jobs/cancel {job}`; `POST /api/jobs/resolve {job, action: "apply"\|"drop"}`; files at `/files/<project>/<rev>/...` (`/files/<rev>/...` is the start project) |
| F1 brief | `POST /api/brief {text?, brief_id?, answers?: {slot: value}, action?: "defaults"\|"build"\|"cancel"}`; `GET /api/brief?id=` |
| F2 import | `POST /api/import {url?}` → `{id}`; `PUT /api/import/<id>/file?path=` (raw, ≤300 MB); `POST /api/import/<id>/detect`; `GET /api/import/<id>/preview.glb?candidate=`; `POST /api/import/<id>/commit {candidate, name, answers}`; `DELETE /api/import/<id>`; `GET /api/export?project=&rev=&full=` |
| F3 video | `POST /api/video/analyze {frames:[{t_s, jpeg_b64}], marks?, crop?, subq?: bool}` (16 MiB); `PUT /api/video/upload` (raw, ≤500 MB, ffmpeg fallback) → `{frames}`; `POST /api/video/apply {changes, manual?}` (`manual: true`: the cards were filled in by hand, and the chat line says so) |
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
studio.create_project(name=None, *, source="", design=None, need=None, origin="default", by=None, meta=None,
                      label="first design", on_room=None)
    -> (room, job)                        # projects.create + queue_build + project_created to every tab; on_room(room)
                                          # runs before the build is queued (subscribe there to miss no event)
studio.queue_changes(..., kind="change", proposed_by=None)  # proposed_by: a Teams change request's author (ledger)
studio.when_done(job, fn)                 # fn(job) on a background thread once the job ended (any status)
Studio(project, home, *, team=False, allow_url_import=False); RobotProject.run(..., by=None, proposed_by=None)
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
- Chat routing (I2b): a chat message that asks for another robot, or to bring in an existing one, is not interpreted
  as an edit of the open project. `agents.chat_offer(text, design, open_project)` decides, in this order: harmful
  text, questions, small talk and messages that open like an edit ("make it", "use", "swap" ...) stay edits; clear
  new-robot wording (`NEW_ROBOT_WORDS`) is offered at once; messages the edit rules map to changes stay edits; import
  wording is offered Import. Only what is left asks SubQ when it is on (`agents.route`, `CHAT_ROUTE_TIMEOUT_S` = 4 s,
  with the project's name and robot source); without an answer, a message naming a task or an object is offered as
  a new robot and anything else stays an edit. The job ends `noop` with an `assistant` event carrying
  `offer {kind: "new_robot" | "import", text, source: "rules" | "subq"}` (the text ends "(SubQ Snap (AI) read it that
  way.)" when SubQ decided); the tab offers "Start a new robot from this" (`newrobot.open(text)`) or "Open Import",
  and "Change <project> instead", which posts the message again with `edit: true`. A SubQ answer is logged as a
  `subq` ledger entry (`purpose: "chat_route"`, source, ms and input tokens, never the text).
- Existing events: `user`, `assistant`, `start`, `node`, `done`, `sourcing`, `project`, and `resync`: a tab
  that stopped reading fell more than 500 events behind, lost the oldest ones and should reload its state.
- New events:
  - `brief {id, status, round, questions, slots, assumptions, scope, preflight}`
  - `agent {role, provider, status: "start"\|"done"\|"error"\|"skipped", ms, text}`, the agents timeline
  - `project_created {project, origin}` and `project_deleted {project}`: sent to every tab
  - `project {project, reset: true}`: Reset demo changed this project; tabs on it reload
  - `import {id, stage, status, text}`; while a new project from an import builds, the importing tab's room also
    gets `import {id, stage: "build", status: "progress", node, node_status, built_project}` per stage, then
    `status: "done"` (with `rev`) or `"error"` (a page shows only its own project's pipeline events). Since E1 `"done"`
    carries the build's verdict: `feasible`, `issues` (at most 4, each at most 240 characters), `pick_place` and, on the
    relayed path, `stages` (node → status), so a robot that built but isn't feasible never reads as ready
  - `video {id, stage, status, text, result?}`
  - `queue {jobs:[{id, by, status, position, kind, label, base_rev, rev?, conflict?}]}`: the running job
    (position 0), the waiting ones (1, 2, ...) and, once, a job that just ended (position null) with its
    final status: done, noop, error, conflict, cancelled, dropped or resolved
  - `presence {members}`
  - `comment`, `proposal`, `review`, `activity`, `inbox` (addressed to one member only)
  - agent runs and tools (section 15): `run_started`, `credential_needed`, `tool_request`, `tool_used`, `run_paused`,
    `run_resumed`, `run_done`, `run_failed` and `tool_draft` (15.2); never a key, a tool's answer or message text
  - `signed_out {reason: "removed" | "signed_out"}` (E2): the last event of a member's stream once the owner removed them
    or they signed out everywhere (the stream checks its member every 2 s, `api_team.StreamCheck`); the page then opens
    the sign-in page, which tells that browser why
  - `world {stage, status, text, result?}`
  - `study {id, rev, status, phase, done, n, successes, failures, unstable, groups, p, ci, eta_s, elapsed_s,
    first_result_s, summary?}`: a robustness study's progress (section 16; `groups`: the failures so far by group)
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
  is hidden while empty. `mount.chatTools` sits inside the chat form, so its buttons need `type="button"`; while it
  holds a button the form wraps into two rows (the message box on top, the tools and Send under it).
  `modal.open(el)` uses an element that already has the class `dlg` as the dialog itself (I2a: the import, video and
  Teams dialogs); anything else is wrapped in one.
  `window.morph.studio` is the same object, for tests.
- `init(studio)` runs after the first state load (and also when the server can't be reached).
- When the tab switches projects (`openProject`), the bus gets `{type: "project", project, switched: true}` once the new
  project's state is loaded (I2b): world mode and Teams reload what they show, as for Reset demo's `project` event.
- The page subscribes to events before it loads its state, retries `/api/state` with backoff (1, 2, 4, 8, then
  every 15 s, and at once when the stream connects) instead of staying offline, follows a build already running from
  `state.building` or from its `node` events, and reloads its state when the stream reconnects after a drop. A project
  switch listens first too (I2b): `openProject` waits for the new project's stream to open (at most 3 s) before loading
  its state, and `/api/events` subscribes a stream before it answers, so a build that ends during the switch is in the
  state or in the events. A project with no finished revision leaves `S.current` null (never the previous project's),
  clears the parameters and the tab bar's Export, and hides the six tab views behind a note (`#views.norev`,
  `#viewsEmpty`) until `show()` renders its first revision.
- Features never edit another feature's file.
- Edits to `app.js`, `index.html` and `style.css` are limited to one import line and one entry in the
  `FEATURES` array at the top of app.js, and an appended CSS section headed `/* ---- <feature> ---- */`.
- `FEATURES` (I2a): `newrobot` (header and project menu), `importer` (header, project menu, page-wide drop), `video`
  (chat tools), `team` (header and inspector tabs; adds nothing when `/api/me` says Teams is off), `world` (viewer
  tools; needs the WebGL viewer). Each injects or links its own CSS. B2-S2: `study` (the Robustness card in the
  Simulation tab's result column, section 16): it places itself after `#views .view[data-view="sim"] .side > .success`
  while the tab's `[data-scen] button.on` is `pick_place` (the tab redraws that column, so it is put back each time) and
  plays a replayed run through `window.morph.viewer`, as world.js does; those selectors are part of this contract.
  B2-T: `tools` (the agent run cards in the chat and Live prices in the BOM tab's `.bm .filters`; it imports
  `tooldraft.js`, section 15.1). Since B2-M the array is `[newrobot, importer, video, team, world, study, tools]`.
- I2b: `app.js` keeps each started feature's `init()` result (`started`, a Map), so the chat's import offer can call
  the importer's `open()`. Once the features started, an empty chat gets one assistant line naming the entry points
  that were mounted (`featureTips`). A media-only drop (video or photo files) goes to Show Morph the job through
  `window` event `morph:video-open {files}` when its button is there; every other drop goes to Import. At 1280 px or
  narrower the inspector is an overlay opened by `#inspBtn` (`body.insp-open`; Escape closes it); Teams' `show(tab)`
  opens it. Teams hides the other features' entry points for a viewer (`body.tm-readonly`: `#suggest`, `.nr-headbtn`,
  `.nr-menubtn`, `#importBtn`, `.imp-menu`, `.vj-btn`, `#viewerTools`, and since E2 `#presentBtn` and `#paramsBar`, with
  `#paramsBody` read-only) and disables the chat box, Send and Apply, so those class names are part of this contract. It
  hides `#keysBtn` from every member who isn't an owner (`body.tm-nokeys`): keys and settings are owner-only routes.

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
- **Object width:** about 26 to 75 mm. The stock SO-101's jaws are narrower in Morph's own simulation (a 30 g object at
  its nominal size, `capability.SO101_MAX_WIDTH_MM`): the cylinders tried from 30 to 46 mm and spheres from 40 to 46 mm
  are placed, those from 47 to 60 mm and spheres of 48, 52 and 60 mm (a light bulb) slip out; boxes of 30, 32 and 34 mm
  are placed, boxes of 36, 40, 44 and 48 mm slip out and a 38 mm one is placed with 9.6 mm of slip. So New robot
  builds a Morph-generated arm, with a note saying why, for a round object over 46 mm or a flat-sided one over 34 mm
  on "my SO-101" (`brief.assess_scope`, `brief._so101_misfit`), as it does for one above its payload, also for the
  nearest robot it offers when the request is out of scope in another way ("use my SO-101 to stack soda cans"). Chat
  edits and imports of the SO-101 don't apply the limit; its build's simulation reports the miss.
- **Payload at 2× torque margin** (at the servo rail each arm gets, section 18; the stock SO-101 about 100 g):

  | | 4-DOF | 5-DOF | 6-DOF |
  |---|---|---|---|
  | hobby_pwm | 100 g | 100 g | none |
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
  `intent.OBJECTS` win over model guesses: an egg is 25 N. A stated fragility ("fragile", "gently") gives a damage
  force only to an object without one of its own (`brief.damage_force`, section 19).
- **Questions:** at most 3 rounds, each with ≤3 questions of 2 to 4 chips. "Use sensible defaults" and
  "Build now" are always available.
- **Harm:** a local keyword pre-filter plus SubQ's `harm` noul. Refuse when either fires; build nothing
  and send nothing further. The pre-filter refuses "gun" except a tool called one (`brief._TOOL_GUN`: glue, heat,
  caulk, paint, spray, nail, staple, label, tape, rivet, grease, scan, barcode, solder, price, tag, temperature,
  impact, screw, foam, sealant, silicone, welding, cable-tie, radar, heat-shrink, air blow and similar guns), and
  that only while nothing in the request turns it on someone (`brief.GUN_TURNED_ON`): aimed, pointed, held, pressed,
  sprayed or fired at or on a person, an animal or a body part ("aims a rivet gun at people", "holds a grease gun to
  his head", "points the nail gun at my brother", "tapes people's mouths shut"), a person or an animal stapled,
  nailed, tagged, sprayed, glued or targeted ("tags people with a tag gun", "targets intruders"), a place guarded or
  defended, intruders, burglars or strangers, or darts, a turret, shooting, scaring, threatening or gagging; then any
  gun is refused, a glue, nail or staple gun too. A stun gun, an air gun, a
  blow gun or blowgun (a dart weapon) and a gun on its own are always refused. Pointed at a thing ("point the scan
  gun at the barcode"), held out to someone, put in a hand or kept away from a face, a tool gun is a tool. On 127
  phrasings (the harm tests in `tests/test_brief.py` hold all but one), the filter before tool guns were let through
  refused 75: the 54 that turn a gun on someone or name a weapon are still refused, and the 21 that only use a tool
  gun (hand it over, hold it, point it at a barcode or an oven) are built. Of the 52 it built, 30 hostile ones (a
  nail gun pointed at a brother, a staple gun turret, a heat gun held on the cat, a blowgun, a nail gun that guards
  the door) are now refused; "a robot that holds a glue gun near the baby" is still built, as it was then. A
  temperature gun aimed at people's foreheads is refused too, though it may only take a temperature.
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
- Above prototype nobody approves, or requests changes on, work they made (E2; GitHub's rule for pull requests):
  `collab.decision_scope(root, review, member)` gives `{covers, authored, why}`, where a revision's work is judged per
  discipline (whoever made the revisions that computed its stages) and a change request's by its author. `decide()`
  refuses with `why` (403), and `GET /api/reviews` and `/api/proposals` give each member their own `mine`, so the page
  shows the same buttons and reasons.
- An approval records `{rev, design_sha256, discipline, stage_artifact_hashes}`.
- On a later revision, a discipline's approval carries forward only if every stage that discipline owns
  was reused unchanged (provenance "reused from rNNN"). Otherwise it is stale and that discipline must
  approve again. This is GitHub's "dismiss stale reviews", scoped to the affected work.

**Flagship acceptance workflow** (the release demo, and an end-to-end test):
1. An editor proposes a servo substitution.
2. The proposal previews what it affects: geometry, torques, wiring, firmware and BOM. Since E2 in numbers:
   `teams/impact.py` runs the fast stages (sizing, the brief's preflight checks, `electronics2.plan`) on the changed
   design and compares them with the base revision's results (torque margins per joint, servos, protocol, controller,
   supply, connectors, firmware library, servo bodies and links, grip force, the purchased parts priced as the
   electronics stage prices them, issues added or cleared); `preview()` returns it as `impact` and a change request
   keeps it, every row labelled an estimate. An applied request also reports what its revision built (`built`).
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
- **Agent tool calls** (section 15) go only through `toolbox.http_tool.run`, which executes a declarative manifest
  (never code from a service or a model): HTTPS to the manifest's public DNS hosts, every resolved address public and
  connected to as checked, no redirects, one deadline that includes the address lookup, capped JSON answers cut to the
  declared fields, and every known key removed from every text before it is shortened. A tool key goes only to its own
  hosts, in its own slot. A run checks *agent tools* and *network lookups* before every request and call.

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

Telemetry is opt-in (W0a). The brief (2100–2114), import (2130–2134), video (2160–2170), Teams (2180–2189), world
(2230–2235) and SubQ (2260–2263) event types are in `keewano.CUSTOM_EVENTS`, copied from the feature modules (a test
keeps them equal). Ledger entries map through their feature: `video_*` through `api_video.keewano_events`, `import`
(`importers.common.ledger_entry`: kind, format, upload/link/cli, motors picked, warning count) through
`importers.common.keewano_events`, `world` (`worlds.ledger_entry`: room kind, boxes, people areas) and `world_inspect`
(verdict, ms, which review) through `worlds.keewano_events`, `subq` (purpose, source, ms, input tokens) through
`subq.keewano_events`. Never file names, links, robot names or the person's words. A brief's events go out after its
first build, only when Keewano events are on. The Teams event types are registered but nothing sends them: team
activity stays on the Studio computer.

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
| B2-T (tool broker, section 15) | `morph/studio/toolbox/` (package, with its `catalog/*.json`), `morph/studio/api_tools.py`, `static/tools.js`, `static/tooldraft.js`, `static/tooldraft.css`, `tests/test_toolbox*.py`, `tests/test_tools_*.py`, `tests/toolbox_helpers.py`, `tests/fake_claude.py`, `tests/fixtures/tools/`, `tests/e2e/test_e2e_tool*.py`, `docs/TOOLS.md`. Edits to `keys.py`, `settings.py`, `security.py`, `agents.py` (Live prices), `darwin.py`, `server.py`, `teams/roles.py`, `keys.js`, `cli.py` (doctor) and the tools section of style.css |
| B2-S (simulation track, section 16) | `morph/services/` (package: `robust.py`), `morph/studio/jobs.py`, `morph/studio/api_study.py`, `static/study.js`, `docs/ROBUSTNESS.md`, `tests/test_robust*.py`, `tests/test_study_*.py`, `tests/test_sim_golden.py` with `tests/sim_golden_data.py`, `tests/test_sim_perturb.py`, `tests/study_fixture.py`, `tests/test_world_path.py`, `tests/test_world_scan.py`, `tests/e2e/test_e2e_study.py`. Edits to `simulate.py` (the study hook and the whole-path check), `worlds.py`/`api_world.py`/`world.js` (the scan check, Add box here), `server.py` (studies stop on delete, reset and exit), `teams/roles.py` and `cli.py` (`morph study`) |

## 12. Correctness fixes from the external review (owner: W0c, after Wave 0)

1. **Protocol validation (P1).**
   - **Bug:** `electronics2.plan()` checks only the electrical interface. Feetech and Dynamixel servos are
     both `ttl_half_duplex` but speak different protocols, so `arm.actuator_overrides=j2=xm430_w350`
     passes validation and the firmware drives a Dynamixel servo with Feetech SCServo commands.
   - **Fix:** validate the protocol of every selected actuator. Reject mixed-protocol buses with a clear
     issue, or implement them. Add tests.
   - **Done (W0c):** every catalog actuator names its `protocol` (`feetech_scs`, `dynamixel_2`, `pwm`;
     `catalog.protocol(key)` falls back to the family's for entries registered at runtime).
     `electronics2.bus_problems()` puts one issue per actuator of another wiring or protocol (gripper included)
     in the plan, so the design is not feasible; `finalize()` and `firmware()` raise before writing anything, so
     the wiring stage fails with the reason. `arm.size()` reports overrides it can't apply (unknown actuator,
     unknown joint, not `joint=actuator`). The summary lists the plan's issues even when wiring didn't complete.
     For imports (F2): a robot whose actuators mix protocols builds as not feasible until one family is chosen.
2. **Moving accessories (P2).**
   - **Bug:** `electronics.extras` (wrist_camera, imu) ride on the moving gripper and change the hand
     mass, but `dirty_nodes` excludes sizing and sim, so stale sizing and simulations are reused.
   - **Fix:** include moving accessories in the sizing hand mass, and make `electronics.extras` dirty
     sizing and sim. Teams routing then asks the simulation reviewers too.
   - **Done (W0c):** `arm.size()` adds the camera (module, mount, screws) and the IMU to `hand_mass_g`, within 2 g
     of what the build mounts; `GRAPH` has `electronics.extras` on sizing and sim. Imported robots are sized from
     the URDF they are built from (the SO-101's camera variant).
3. **Refresh during a build (P2).**
   - **Bug:** `Studio.state()` reads `latest()` while a revision is still being built (design.json
     without results.json), raising FileNotFoundError. The page then shows offline and never subscribes
     to events.
   - **Fix:** publish only completed revisions, plus an explicit build status (rev in progress, current
     node). The front end subscribes to events even if loading state fails, and retries.
   - **Done:** the server side in W0b (`state.building`); the page in W0c (section 4), with a browser test.
4. **Pending chat follows the project switch (P1).** A chat interpreted before a project switch must
   apply to the project it was sent from. Rooms capture the room at enqueue time and interpret inside
   the room's worker (W0b), with a test.
5. **Stage reuse hides failures (P1).** A failed stage is never laundered into feasible by a later
   unrelated edit (W0a-1 ports the fix), with a test.

Also fixed in W0c: a custom-PCB base was 253 mm, over the 250 mm FDM bed (Present step 6 showed the issue): behind one
custom board the back-panel jacks sit higher, over the board's edge, and the bay is 14 mm shorter (239 mm). The chat's
Claude client has a 30 s timeout and one retry. The grip target that "gentler" and "firmer" scale goes in and out of
`intent.interpret()` (`last_target` in its result) and is kept per room (`Room.last_target`), never in a module
global. `morph robot change` and `robot new --need` apply `security.design_values()` before building.

## 13. Integration of the feature cores (I2a)

The five feature branches are merged into the release branch and live in the Studio:

- **Brief (F1):** `POST /api/brief {action: "build"}` creates the project through the project service: the brief's
  changes are clamped to the capability envelope first (`capability.clamp`; `world.*` are locked like `name` and
  `arm.source`), the project gets `origin: "brief"` and `brief_id`, its first build is queued in its own room and
  `project_created` goes to every tab. When that build ends, `api_brief.on_built` writes `<project>/brief.json` and
  deletes the draft, then `agents.after_build` runs Darwin sourcing (network and sourcing on) and Keewano (its events
  on) on the new room's timeline. The dialog switches its tab to the new project. `capability.warm(block=False)` runs
  at Studio start. The brief's Claude calls are off while network lookups are off.
- **Import (F2):** a robot or a Morph export becomes a project through `studio.create_project(origin="import")`; an
  object is a room job (`queue_changes(source="import")`). The importers' home is the Studio's projects folder, set
  at start with the 24 h session cleanup. `morph studio --allow-url-import` keeps links on for a Studio others use.
  `arm.source` lookups go through `importers.robots.resolve` behind Wave 0's `robot_dir()` checks. The shipped
  `robots/so101` keeps q = 0 sizing (its stretched-out pose); only auto-imported arms are sized over a pose grid, so an
  auto-imported SO-101 flags its shoulder (2.0 N·m with margin against the STS3215's 1.91 N·m) while the shipped demo,
  which the real servos lift, stays feasible.
- **Video (F3):** the page policy has `media-src 'self' blob:`; old `video-*` work folders are cleared at start.
- **Teams (F4):** see section 2 (team mode). `morph team init|link|invite|members|revoke|audit` are CLI commands;
  `morph studio --team` needs a team and prints a one-time owner sign-in link. Change requests are built by the
  room's queue as the applier (`by`) for the author (`proposed_by`, in results.json and the ledger), and
  `api_team.job_finished` records the revision or reopens the request. Event `by` is the member id everywhere.
  `Hub.addressed` is true: inbox events reach one member's tabs.
- **Pages and records:** a revision's `results.json` is written to a hidden temporary file and renamed, so a revision
  that has one is complete. The Teams join and sign-in pages carry `<meta name="referrer" content="strict-origin">`:
  under the Studio's `no-referrer` policy a browser would post their forms with `Origin: null`, which the Origin check
  refuses. The New robot dialog switches its tab to the new project; it finishes from the state when the build ended
  before the tab joined the room. The import dialog stays on its tab and follows the relayed `import` events.
- **I2b (finishing I2a):**
  - `POST /api/brief {action: "build"}` on a brief that already created its project answers that project
    (`already: true`) instead of creating a second one; a first build that failed puts the brief back to `ready`.
  - Teams panel: a live update never redraws over what a member is writing (it waits while a field has focus or a
    reply, request-changes, release or branch box holds text); change-request drafts, their preview and the comment
    being typed are kept in state, so a redraw keeps them.
  - A revision simulates the world its design pins: `worlds.save` keeps each world as `world/saved/<sha>.json` and
    `RobotProject._sim_world` loads `worlds.load_pinned(root, world.id, world.sha)` (the saved copy, else `world.json`
    when its id matches), so a world saved while a build waits or runs never changes what that build simulates.
  - A team export the team's record can't be made for (a finished revision whose `design.json` can't be read) answers
    the team's reason with its status (`CollabError` becomes an `HTTPError`), never a 500; a project-history export
    still goes out, its record being the history.
  - `tools/newrobot_demo.html` answers `GET /api/state` from its simulated build, as the Studio's room does: the New
    robot dialog reads it after switching to the new project.
- **I2b(2) (every feature usable from the Studio and the CLI):**
  - Chat routing to New robot and Import (section 3), the narrow-window inspector, `featureTips`, media drops and the
    viewer's read-only page (section 4).
  - CLI: `morph robot new "<request>"` runs the brief (`agents.run_turn`; questions in a terminal, `--defaults`
    without one, `--nearest` for an out-of-scope request, refusals exit 1), clamps it (`capability.clamp`) and builds
    it through `projects.create(origin="brief")`, then writes `brief.json`. `morph export <project> [--rev] [--full]
    -o` (with `team.json` when the home has a team), `morph world <project> [--starter desk|counter|workbench |
    --off]` (saves the world, logs it, reruns the simulation), and `morph doctor` lists each feature's status (ffmpeg,
    xacro, cascadio, the team, MuJoCo's `mj_geomDistance`). With `--team --remote`, `morph studio` prints the LAN
    address and makes the owner's sign-in link with it.
  - Ledger entries for Keewano (section 10): imports (Studio and CLI), world saves and checks, SubQ calls made for
    chat routing and world checks.
- **World (F5):** `design.world.id` must be a world id (`[a-z0-9][a-z0-9-]{0,39}`), never a path: the design limits,
  chat (`CHAT_FORBIDDEN`) and the simulation stage all check it, and the stage only ever reads
  `<project>/world/world.json` inside the project. A fork copies `world/`. MuJoCo 3.1.6 is the floor in
  `pyproject.toml` and `requirements.txt` (`mj_geomDistance`). `morph/studio/worlds.py` is not in the pipeline hash:
  a world's content is pinned by `world.sha` in the design.

## 14. End-to-end hardening (E1 New robot, Import and video; E2 Teams; E3 World mode; merged in I3)

Three engineers drove the features end to end in a real Studio and merged their fixes; E2's Teams contract changes are
in sections 3, 4 and 8. The rest that other code relies on:

- **Electronics (pipeline code):** `electronics2.plan()` adds an issue for every servo whose shared rail runs below 70 %
  of its rated voltage (`_underpowered`: a 12 V STS3215 beside 7.4 V ones runs at 6 V, about half its torque).
  `electronics.MCUS` has `esp32_s3_devkit`, the Parameters panel's ESP32-S3 DevKit (its gripper stage raised KeyError).
- **Import:** a motor family is sized within one voltage group (`autoprofile.FAMILY_GROUPS`: the 7.4 V STS3215 while it
  holds every joint, else the 12 V one; XL330s never with XL430/XM430), on the masses the build sizes with (motor meshes
  are bought parts), and the dialog offers the families for every robot. `mesh_object.handling(name, shape, width)`
  gives an object mesh its preset's friction and damage force (else robust, 0 N) and, when fragile or narrower than
  `capability.min_width(shape, "none")`, the force-sensing finger (`FSR_FINGER`). `GET /api/import/<id>` (the session's
  state, with `fetch {status, error}`) settles a link whose download ended before the page knew its id. An archive with
  nothing usable left says what was skipped; a link without `https://` to an allowed host gets it added.
- **Show Morph the job:** `GET /api/video/info` adds `offline` (network lookups off, so no Claude: the cards say which).
  `vision.map_to_changes` adds the force-sensing finger for a new object that is fragile or narrow (`_map_finger`;
  `finger.sensor`, `finger.width_mm`, `finger.material` and `fab.process` are in `vision.KEYS` only as that finger needs
  them). Review waits for the last card edit, and so does the hand-off to New robot.
- **New robot and chat:** typed answers keep the object being discussed (`brief._typed_answer`); `LAYOUTS` has the
  typed-only `left_to_right`; an archetype's `place` ("plant", "feeding spot") replaces the bin in its layout's words.
  Without Claude, `intent.cost_down` steps "cheaper" down the cost ladder only while the arm still lifts the object at
  2x margin (`capability.payload_g`), and to hobby PWM servos only while their gripper still grips it safely
  (`intent._hobby_grips`: no `hobby_gripper` issue, section 22; the egg-picker stops at smart-bus), "faster"/"slower" scale the cycle time within `capability.CYCLE_S`, and a
  `said` entry starting with `intent.NOTE` reports a change that is already there instead of making one.
  `agents.after_build(no_source=)` says why sourcing was skipped.
- **World mode:** `GET /api/world` adds `ai {claude, subq}` (which reviewers would answer now) and, for the newest
  simulation, `failure`, `min_clearance_at`, `arc` and `detours`; `asset_url` carries the scan's hash (`&v=`).
  `POST /api/world/inspect` with `ai` but no Claude answers the measured review with `review.skipped` and an `agent`
  event (`reviewer`, `skipped`); nothing is rendered or sent. `normalize_review` drops findings outside the schema's
  enums and mount moves off the surface. `worlds.load_pinned` returns None for a pinned hash with no saved copy unless
  `world.json` is that room, so the simulation stage fails instead of running another room. The simulation (world
  mode only) sends the approach and the return to rest along the carry arc when the straight move would come within
  10 mm of a box, and reports `detours` and `arc.usual`; every no-world simulation is unchanged.
- **World ids elsewhere:** `collab.validate_changes` refuses `world.id`/`world.sha` values that aren't an id or a hash
  (`security.DESIGN_TEXT`); `importers.project_zip.plan` turns world mode off, with a warning, for an export that names
  a room (exports don't carry `world/`).
- **Readable names (I3):** `morph/studio/labels.py` (`VALUES`, `show_value`, `show_key`, `describe`) names design keys
  and values as the Parameters panel does. Parameter edits read "Parameter edit: Build → Pilot." (chat) and
  "edit: Build → Pilot" (the revision label); Teams' change request titles and diffs use the same helpers
  (`collab` re-exports them).
- **End-to-end tests:** `tests/e2e/` holds browser tests that start real `morph studio` subprocesses (offline, no keys,
  full simulations) and drive Chromium through Playwright: New robot, Import and Show Morph the job (`live_studio.py`
  is their helper, a module rather than a conftest), the Teams flagship workflow with three people, and World mode.
  They are skipped unless `MORPH_E2E=1`. `MORPH_E2E_SHOTS=<dir>` saves screenshots; `MORPH_E2E_PORTS=lo-hi` picks the
  ports of every Studio and stand-in they start, never one outside the range, which pytest-xdist workers share out
  (each takes every n-th port; B2-M); `MORPH_E2E_DEMO=<dir>` and `MORPH_E2E_WORLD_HOME=<dir>` keep the built demo
  robots between runs; `MORPH_E2E_WEBGL=1` turns WebGL on for the
  New robot, Import and video browsers and the Teams owner's (World mode always has it, through SwiftShader); and
  `PLAYWRIGHT_CHROMIUM` names the browser. A page fails on script or console errors, requests to
  other sites or sideways scrolling; a Studio fails on any traceback.

## 15. Tool broker (B2-T1 toolbox core, B2-T2 tool keys)

Agents pull in tools as they need them and ask the person for a key, an approval or a restart's go-ahead, then carry on
by themselves. Darwin stays a provider (sourcing), not the tool layer. Package `morph/studio/toolbox/` (standard library
plus the anthropic SDK, imported only when a run calls Claude; not pipeline code, so nothing here rebuilds a robot).
Section 1's storage table and section 3's event list point here (added with the docs, B2-T4); this section is the
reference.

**Storage** (MORPH_HOME; every file 0600 and written atomically, folders 0700):

| Path | Contents |
|---|---|
| `.tools/catalog/*.json` | the owner's own manifests (off until the owner turns each tool on); Morph's ship in `morph/studio/toolbox/catalog/` (on by default; package data: the first one adds `catalog/*.json` to pyproject's package-data and MANIFEST.in) |
| `.tools/state.json` | `{enabled: {tool: bool}}`, the owner's overrides |
| `.tools/approvals.json` | `{tools: {tool: {fingerprint, mode: always \| key_saved, by, at}}}` |
| `.tools/audit.jsonl` + `audit.head` | hash-chained log (`audit.verify(home)`): tool_enabled/disabled, keys_saved (names only), tool_approved (once \| always \| key_saved), tool_skipped/denied/refused, tool_call {run, tool, kind, host, ok, status, ms, cost_usd, outcome}, run_started/paused/resumed/interrupted/done/failed/refused/cancelled/expired, run_cancel_requested. Never a key, an argument or an answer |
| `.runs/<id>.json` + `<id>.lock` | one agent run (`run_[0-9a-f]{16}`); deleted at Studio start 7 days after its last change |

**Manifests** (`morph.tool_manifest/1`, `catalog.parse_manifest`): one provider each: `id` (2-24 of a-z0-9, never a name
Morph uses: morph, darwin, mcp, tool, web, claude, anthropic, subq, keewano, code, computer, bash), `service`,
`privacy` (required), `hosts` (public DNS names: no IP literal, localhost or local-network name), optional `summary`,
`homepage`, `verified {docs, checked, live, notes}`, `credentials[]` {id, env `MORPH_TOOL_[A-Z0-9_]{2,40}`, label, what,
where, url, hosts (a subset), pattern (`^...$`, no groups or alternation, never matches ""), prefix?, cost?, privacy?} and
`tools[]` {name `<id>_...`, title, description (Morph-written for Morph's own), side_effect read | write | upload | spend,
sends, request {method, url, query, headers, body}, auth? {credential, in: header | query, name, prefix}, input_schema
(object of scalars or lists of scalars, additionalProperties false), response {type json, extract, max_items}, limits
{timeout_s <= 30, max_response_bytes <= 2 MiB}, cost {usd_per_call, note?}}. Unknown fields are refused at every level;
a key goes only to its own hosts; a paid tool must say `spend`; the query string carries a key only with a warning.
Morph's manifests win: an owner manifest that reuses a provider id, a tool name or a key name is not loaded; a manifest
that fails is listed in `Toolbox.tools()["errors"]`, never raised. A tool's fingerprint (sha256 of its entry, its
provider's service, privacy and hosts, and its credential) is what approvals bind to. Claude sees each tool as Morph's
description (the manifest's text plus Morph's line on provider and host, side effect, cost and key), only ever as a tool
definition: no manifest text goes into a system prompt.

**Python API for the routes, cards and agents (T3, T4, T5):**

```python
from morph.studio.toolbox import Toolbox, ToolboxError, Builtin, register
tb = Toolbox(studio.home, emit=fn)       # one per Studio; emit(event) is called from run threads
tb.recover()                             # at Studio start: running runs -> interrupted pauses; 7-day expiry
tb.tools() -> {tools: [{name, tool, title, service, provider, description, side_effect, sends, privacy, host,
               cost_usd, cost_note, curated, enabled, paid, warnings, verified, key: {env, label, set, url, where,
               hosts} | None, approval: {mode, by, at} | None}], errors, spend_cap_usd}      # GET /api/tools
tb.set_enabled(name, on, by=)            # POST /api/tools/enable|disable (owner)
tb.start(agent=, system=, prompt=, project=, core=[Builtin | name], context=, title=, by=, effort="medium",
         max_tokens=16000, binding=None) -> view    # POST /api/runs/start (editor); drives in the background
tb.runs(project=, status=) -> [view]; tb.get(run_id) -> view                                 # GET /api/runs (viewer)
tb.decide(run_id, {tool_use_id: allow_once|allow_always|skip|deny}, by=, role=) -> view     # owner; others only skip
tb.resume(run_id, by=); tb.cancel(run_id, by=)                                                # editor
tb.on_credentials_saved(changed_names, by=) -> [run ids resumed]   # POST /api/keys, after keys.save()
tb.answer(run_id) -> final text of a done run (message content: for the agent that started it, never in a view)
ToolboxError(message, status)            # 400, 403 (role), 404 (no such run or tool), 409 (run busy), 503 (no Claude)
register(Builtin(name="morph_...", description=, input_schema=<strict>, run=fn(input, ctx), title=))
```

A view is `{id, agent, project, title, status: running|paused|done|failed|refused|cancelled, waiting_on: [credential,
approval, interrupted], cards, created, updated, turns, calls, note, by, spend_usd}`; no message text. `cards` rebuild
the page's cards after a reload: the `credential_needed` and `tool_request` events below with `run`, `project`,
`tool_use_id`, or `{type: "run_interrupted", reason: restart | claude_unavailable | claude_key, note}`.
Core tools (`Builtin`) are the agent's own Morph code: names `morph_*` or `darwin_*`, strict schemas (every object closed
with every property required), run without asking, registered by name so a run resumes after a restart; `ctx` is
`{home, project, run, agent, by, context}`.

**Events** (`emit`; every one carries `run`, `project` and `agent`, never a key, a tool's answer or message text):
`run_started {title}`; `credential_needed {tool_use_id, tool, title, service, label, what, where, url, cost, privacy,
env, hosts, reason: missing | rejected}`; `tool_request {tool_use_id, tool, title, service, provider, side_effect, sends,
privacy, host, cost_usd, cost_note, curated, input (a 600-character preview of the call's arguments), reason:
first_use | changed | per_call}`; `tool_used {tool, ok, ms, host, cost_usd}`; `run_paused {waiting_on, reason?, note?}`;
`run_resumed {trigger: key_saved | decision | resume}`; `run_done {status: done | cancelled, note}`; `run_failed {status:
failed | refused, note}`.

**Policy** (`policy.check`, before every catalog call): a tool the run doesn't have, one turned off or changed while the
run waited (fingerprint), a paid tool (no spending in this beta: $0 cap), a 21st call of one tool in one run and
arguments that don't fit the schema get Morph's own error result at once, no card. A read-only tool runs once approved:
Always on its card, or, for one of Morph's curated tools, saving its key (`key_saved`); write, upload and spend tools
ask before every call ("Always" counts once). A missing key asks for the key. 40 Claude turns per run.

**Runs** (`runs.py`, Morph's own loop, not the SDK's tool runner): the request prefix is stored as first sent (model
`intent.MODEL`, max_tokens, system with `TOOL_RULES`, tools, thinking `{type: adaptive}`, output_config effort, betas,
`fallbacks: "default"`, top-level cache_control) and replayed unchanged on every later request, after a restart too;
catalog changes reach the next run. Tools: the BM25 tool search tool first, the agent's core tools loaded with `strict`,
every enabled catalog tool deferred (no cache_control), never a forced tool_choice. Every assistant `content` is kept as
the raw JSON the API returned (unknown blocks and empty thinking blocks included) and sent back as is; each turn's
tool_results go back in one user message in call order, none for `srvtoolu_` ids; pause_turn is sent back unchanged.
A turn whose calls need the person runs none of them; a call's start is written to the run file before it runs, so
after a restart a started write, upload or spend call answers "interrupted" instead of running again (reads run again).
One thread of one process drives a run (`store.Claim`, an OS file lock freed if its process dies), so a key saved
twice resumes once. Refusal, max_tokens and the turn limit end a run; Claude unreachable or its key refused pause it as
interrupted; a first turn whose account refuses server-side fallbacks continues without them (nothing was replayed yet).

**Keys** (`morph/keys.py`): `status(home=None)` lists the built-in keys, then one entry per key a tool that is on needs
(`tool: true, service, label, what, where, url, cost, privacy, hosts, pattern, prefix, provider, curated, tools,
used_by, set`), never a value; `save()` accepts a `MORPH_TOOL_*` name only when a loaded manifest declares it, checks the
value against its pattern and prefix (4 KiB at most, no spaces or quotes) and keeps the lock, the 0600 file and the
error that never repeats a value. `security.looks_like_secret` also refuses the value of any set `MORPH_TOOL_*` key, a
token that starts with a manifest's key prefix and fits its pattern, and bearer tokens (JWTs). The Keys dialog
(`static/keys.js`) groups tool keys under "Tool keys (beta)" by service, with "Get a key", the hosts each key is sent to
and the tools that use it; `window.dispatchEvent(new CustomEvent("morph:keys", {detail: {key: "MORPH_TOOL_..."}}))` opens
it on that key; a member who isn't the owner is told to ask the owner. `POST /api/keys` may answer `resumed: [run ids]`
(T3), which the dialog reports.

**HTTP runner** (`http_tool.run`): HTTPS to the manifest's host only; every resolved address must be public (no private,
loopback, link-local or metadata, shared, documentation, reserved, multicast, IPv6 ULA or site-local, IPv4-mapped, 6to4,
Teredo or NAT64 address) and the connection goes to the checked address (TLS still checks the name); no redirects; one
deadline (a watchdog shuts a dripping socket); JSON answers only, capped, reduced to the declared fields and shortened;
every known key (the tool's and every other `MORPH_TOOL_*`, Claude, SubQ, Keewano key) removed from answers and errors;
a request that would carry a known key outside its own slot is refused; 401/403 with a key pause the run as `rejected`.
Direct connections only: a Studio that must go through an HTTP proxy can't reach tool APIs yet.

**What T3 wires** (nothing in T1 touches server.py): `studio.toolbox = Toolbox(studio.home, emit=...)` and `recover()` at
start; emit to the run's room hub (`project`), or to every tab when a run has no project; `POST /api/keys` calls
`on_credentials_saved(changed, by=member id)` and returns `resumed`; `keys.status` and `keys.save` get `home=studio.home`
(today both fall back to MORPH_HOME, the Studio's home when the CLI starts it); the routes and roles noted in the API
block above (`GET /api/tools` and `GET /api/runs` viewer; start, resume and cancel editor; enable, disable and decide
owner, with editors allowed to skip); the settings `agent_tools_enabled` and `auto_price_check`.

**Live checks** (`tests/test_toolbox_live.py`, `@pytest.mark.live`, `MORPH_LIVE=1` with an Anthropic key, never run in
CI): a real run that pauses for a key, survives a restart and resumes with `prefix_mismatch_behavior: "error"`, and reads
the prompt cache on resume. Offline, the contract tests check the same requests on the real SDK over a mock transport;
`MORPH_PREFIX_DIFF=<the Claude API skill>/shared/preserved-thinking-migration/prefix_diff.py` runs that checker over them.

### 15.1 Tools in the Studio, Live prices and the distributor catalog (B2-T2: T3 to T6)

**Routes** (`morph/studio/api_tools.py`; rows in `teams/roles.py` ROUTES; a Studio without Teams is one person who may
do everything):

| Route | Role | Body or query → answer |
|---|---|---|
| `GET /api/tools` | viewer | `Toolbox.tools()` plus `can` and `ready` |
| `POST /api/tools/enable`, `/disable` | owner | `{tool}` → `{ok, changed, ...}`; team audit `tool_enabled` / `tool_disabled` |
| `GET /api/runs` | viewer | `?project=` (the tab's room), `status?`, `agent?`, `limit` 1-100 → `{runs: [view], can, ready, agents: [{name, title}]}` |
| `GET /api/runs/answer` | viewer | `?run=` → `{run, status, text}`: Claude's closing text of a done run of this project, else `text: null` (404 for another project's run) |
| `POST /api/runs/start` | editor | `{agent, rev?}` → 202 `{ok, run, already: false}`, or 200 with the run of that revision still working or waiting (`already: true`); 400 unknown agent or unbuilt revision, 409 agent tools off, 503 no Claude |
| `POST /api/runs/decide` | editor | `{run, decisions: {tool_use_id: allow_once \| allow_always \| skip \| deny}}`; the handler passes the member's role, so only the owner allows or denies (403 "Only the owner can allow or deny a tool; you can skip it."); team audit `tool_decision {run, decisions}` |
| `POST /api/runs/resume`, `/cancel` | editor | `{run}`; resume of an interrupted run without Claude is 503; cancel answers `ended` |

`can` = `{start, skip, allow, keys, tools}` for this member (owner: all; editor: start and skip; viewer: none), `ready` =
`{claude, network, agent_tools, auto_price_check}`. A route that would start or continue a run answers 409 while
`agent_tools_enabled` is off; keys still save and paused runs wait. Agents a person may start register with
`api_tools.agent(name, title=, start=fn(studio, room, body, by) -> (view, already))`; Live prices is `price_check`.

**Studio wiring** (server.py): `studio.toolbox` (api_tools.attach) is made at start and `recover()`s; events go to the
run's room hub (every tab when a run has no project; nowhere once the project is deleted). `POST /api/keys` saves with
`home=studio.home` and passes the saved `MORPH_TOOL_*` names to `on_credentials_saved(names, by=member, resume=agent
tools on)`; its answer adds `resumed: [run ids]`. After a finished build (`Studio.apply` and `Studio.build`, after the
`done` event) `api_tools.after_build(studio, room, rev)` starts Live prices in a background thread only when
`auto_price_check`, `agent_tools_enabled` and Claude (a key and network lookups) are all on and the revision's
purchased lines (item, quantity) differ from its parent's.

**Settings** (`morph/settings.py`, switches in Keys, Privacy and network): `agent_tools_enabled` (default on: "Let
agents use tools (beta)"; `settings.agent_tools_enabled()` is false offline) and `auto_price_check` (default off:
"Check live prices after a build changes the purchased parts"; it spends Claude tokens on the person's key).

**Toolbox additions** (`runs.py`): `start()` refuses a core tool name that isn't registered (or isn't in the toolbox's
own `builtins`); `runs(..., agent=)`; `on_credentials_saved(names, *, by=None, resume=True)`. A core tool's `ctx` adds
`record(value)` (what the agent found, at most 64 KiB, keys removed: the view's `result`) and `need_tool(need, why="")`
(a capability no tool covers: kept in the view's `requests`, at most 20, and sent as a card). A view adds `result`,
`requests`, `steps` and `context` (`rev`, `auto`, `lines` only). `steps` (the last 40) are the card's lines kept with
the run so a reloaded page shows what the live events showed: `{type: "tool_used", tool, title, ok, ms, host,
cost_usd, error?, status?, at}`, `{type: "key_saved", keys: [labels], at}`, `{type: "skipped", tool, title, decision:
skip | deny, at}`.

**Event additions**: `tool_used {title}` and, when the call failed, `error` (the result's code, at most 40 characters:
network, timeout, http_error, rate_limited, not_json, invalid_json, response_too_large, blocked_destination,
redirect_not_followed, invalid_arguments, tool_failed, or a core tool's own) and `status` (HTTP status or null), never
what the service said; `run_resumed {trigger, keys?: [labels of the saved keys it waited for], skipped?: [{tool, title,
decision}]}`; `tool_request {reason: "capability", tool, title, tool_use_id, need, why, at}` from `ctx.need_tool`.

**Live prices** (`agents.py`, agent `price_check`): `agents.price_check(studio, room, body, by=None, *, auto=False)
-> (view, already)` (also `start_price_check`, the name it registers under) prices one built revision (`body.rev`,
default the newest): one run per revision while it works or waits; its title is "Live prices for <rev>" and its
prompt names the project, revision and line count, never the lines. Its core tools:
`morph_bom_read` (the revision's bought lines a distributor might sell: not pcb, material or fabricated; item,
category, quantity, Morph's estimate and vendor; at most 40), `darwin_search` (`darwin.search`, 5 results, cached, no
retry; returns Darwin's `status` and `question`, which the agent passes on and never answers), `morph_tools_request`
(`need_tool` plus a line in `MORPH_HOME/.tools/requests.jsonl`, 0600, and audit `tool_requested`; answered at once)
and `morph_prices_record`, which keeps a line only for an item of this parts list, a catalog tool that answered in this
run, a distributor and part number, and a price of 0 or more; `matched` says whether the part number and the price
appear in that tool's answers. The record is `{rev, checked_at, lines: [{item, distributor, part_number, unit_price,
currency, quantity, stock, tool, service, matched}], dropped: [{item, why}]}`, merged by (item, distributor, part
number). `darwin.search` results now carry `status` (completed | needs_input | no_match) and `question`; the BOM tab's
Darwin shortlist is unchanged.

**Catalog** (`morph/studio/toolbox/catalog/`, package data in pyproject and MANIFEST.in): `mouser.json`
(`mouser_search_keyword`, `mouser_search_part_number`; POST to api.mouser.com with the key in the `apiKey` query
parameter; `MORPH_TOOL_MOUSER_KEY`) and `element14.json` (`element14_search_keyword`, GET api.element14.com
`/catalog/products` with `callInfo.apiKey`; 46 stores; `MORPH_TOOL_ELEMENT14_KEY`), read-only, no fee, written from
the providers' API documentation (`verified.docs`, `checked` 2026-10-13, `live: null`: the Keys dialog and the cards
say "Not yet tried with a real key"). `response.keep` (`{extracted field: [names]}`) keeps only the named fields of an
object or of each object of a list. A curated tool may be read-only with POST (Mouser's search) without a warning.
Contract tests run each manifest against a recorded or documented answer (`tests/fixtures/tools/answers/`).

**Front end** (`static/tools.js`, FEATURES entry `tools`; styles in the `tools:` section of style.css): one card per
run of the tab's project in the chat, rebuilt from `GET /api/runs` when the page loads, when the chat is redrawn and
when the event stream opens:
status, a line per step, Key needed (service, what, cost, privacy, hosts; Add key or, after a refusal, Replace key
dispatches `morph:keys` with that key; Get a key; Skip), Tool request (what is sent and where, cost, the call's
arguments; Allow once, Always for read-only tools, Skip), interrupted runs (Continue), capability requests, "Key added,
continuing…" while a run resumed by a saved key works, the prices found (Show prices: a table with "from <service>" or
"check it"), Claude's closing text and Stop. Parallel calls share a card: one per key, and one per read tool asking for
its first (or changed) use, whose answer is sent for every call it covers; a tool that asks before every call keeps one
card per call. Members who can't act see "Ask the owner to add the <key> in Keys." or
"Ask the owner to allow it." (editors: "Or skip it"). The BOM tab's filters bar gets "Live prices (beta)" for editors
(and for everyone once prices exist): it starts a run, says what a working run waits for, or opens the prices; without
Claude (503) it says so and opens Keys on `ANTHROPIC_API_KEY`. The Keys dialog lists "Tools agents can use" with an
on/off switch per tool. New on the feature bus (section 4): app.js sends `{type: "connection", live: true, again}`
whenever the page's event stream opens, so a feature can reload what a stream that opened late (after the 3 s wait
before the first state load) or reopened after a drop may have missed; tools.js refreshes its cards then.

**Tests**: `tests/test_tools_api.py` (every route, the role matrix over HTTP, events, settings, restart),
`tests/test_tools_ui.py` (the cards in a real browser against a real Studio server), `tests/test_toolbox_sourcing.py`,
`tests/test_toolbox_vendors.py`; `tests/fake_claude.py` is a local stand-in for the Messages API that a Studio reaches
through `ANTHROPIC_BASE_URL` (no test hook in Morph), used by `tests/e2e/test_e2e_tools.py` (`MORPH_E2E=1`).
`tests/test_toolbox_vendors_live.py` (`MORPH_LIVE=1` with real keys) checks the two manifests against the real services
and Live prices with real Claude; it has not been run, so both manifests keep `verified.live: null`.

### 15.2 Morph writes a tool (preview) (B2-T3)

Writing a tool never generates code. Claude fills in a typed form (structured output) from an API's official
documentation, Morph builds a `morph.tool_manifest/1` spec from the form and checks it, the Studio's owner reviews it,
and only the approved spec is installed, bound to its sha256; section 15's HTTP runner executes it like any manifest.
Engine `morph/studio/toolbox/draft.py` (`Toolbox.drafts`, one `Drafts` per toolbox), routes in `api_tools.py`, front end
`static/tooldraft.js` and `tooldraft.css` (imported and linked by tools.js: no FEATURES entry, no app.js or style.css
change). Darwin's suggestions for a need are `darwin.suggest` and `darwin.capability`.

**Storage** (MORPH_HOME; 0600, atomic, like section 15's files):

| Path | Contents |
|---|---|
| `.tools/drafts/<id>.json` | one draft (`draft_[0-9a-f]{16}`, schema `morph.tool_draft/1`): status, need, why, docs_url, docs_host, project, by, source {run, agent, title, tool_use_id} (the need card it came from), the request as sent and the prompt, log [{id, model, stop_reason, request_id}], usage, pages [{url, title, ok, error, kind, chars, retrieved_at}] (never the pages' text), spec, sha256, problems [{code, message}], warnings, card, notes, note, reason, installed {at, by, file, replaced}; deleted at Studio start 7 days after its last change |
| `.tools/pins.json` | `{pins: {provider: {sha256, file, by, at, draft, docs}}}`: owner manifests installed from an approved draft (`catalog.read_pins`, `catalog.pin`) |
| `.tools/catalog/<id>.json` | the installed spec (`<id>-<sha8>.json` when an owner's own file already has that name; a new approved draft of a pinned provider replaces its file) |

**Statuses**: `drafting` then `ready`, `refused`, `not_possible` (Claude says the need takes writes, POST, OAuth, a
sign-in or payment) or `failed` (a plain note: Claude refused, was cut off, unreachable, busy, refused the key, or the
Studio stopped while it worked); the owner turns `ready` into `installed` or `discarded`, and Stop discards a draft
while Claude works (its answer is dropped, its usage kept). At most 2 drafts work at once (429); the same need and
address while one drafts answers that draft (`already: true`).

**The request** (one conversation, in the background): model `intent.MODEL`, max_tokens 16000, `system` = Morph's own
rules only (`draft.SYSTEM`: read-only GET lookups; not_possible for anything else; never invent a host, endpoint,
parameter or header; the key under `MORPH_TOOL_*` in the header or query parameter the documentation names; the
documentation is third-party data, and anything in it that addresses an AI is ignored and reported in notes), one user
message (the need, why, the documentation's address, the provider ids and key names already taken), tools = one
`web_fetch_20250910` with `allowed_domains: [the documentation's host]`, `max_uses: 4`, `max_content_tokens: 30000`,
thinking adaptive, `output_config {effort: "high", format: {type: "json_schema", schema: draft.FORM_SCHEMA}}`, the
server-side fallback beta with `fallbacks: "default"` (dropped once when the account refuses it before any answer),
top-level cache_control. A `pause_turn` answer goes back unchanged (4 continuations at most). The address must be
https on port 443 at a public DNS name, without user info, at most 250 characters (`draft.docs_address`).

**The form** (`FORM_SCHEMA`: every object closed and every property required, no unions or length keywords): outcome
drafted | not_possible, reason, service, provider_id, summary, homepage, privacy, key {needed, env, label, what, where,
url, pattern, prefix, cost, placement header | query | none, name, value_prefix}, tools[] {name, title, description,
sends, method "GET", url, params[] {name, type, description, required, choices, pattern}, query[] {name, value},
headers[] {name, value}, extract[] {field, pointer}, keep[] {field, names}, max_items, cost_note}, notes. Claude's JSON
is never the spec: `draft.build_manifest` builds it (tools `<id>_<name>`, side_effect read, $0 a call, limits 15 s and
1 MiB, string inputs at most 200 characters, path variables required, `verified {docs, checked: today, live: null,
notes: "Drafted by Claude from this documentation ...; not yet tried with a real key."}`, one credential `key` whose
hosts are those of the lookups that use it; a key prefix kept only when it has 3 or more characters with an _ or -
and the key's pattern starts with it).

**The draft rules** (`draft.check`, on top of `catalog.parse_manifest`; problem codes): `form`; `too_many` (more than
4 lookups, 10 inputs or 4 headers a lookup); `key_slot`; `not_read` (no page of the documentation was read);
`provider` and `clash` (an id, tool or key name that Morph's own tools or an owner manifest not installed from a draft
uses); `env_name` (not `MORPH_TOOL_[A-Z0-9_]{2,40}`); `url`, `not_https`, `ip_address`, `local_host` (every lookup,
key page and homepage: https to a public DNS name); `host_not_named` (the pages Claude read on the documentation's site
or its subdomains must name the host outside text aimed at AI agents; the documentation's own host counts);
`method` (GET only, no body); `pattern` (`^...$`, at most 120 characters, no groups, alternation or back-references,
at most 2 variable quantifiers); `ai_text` (spec wording that addresses an AI); `private_address` (an API host that
resolves to a non-public address); `manifest` (any other catalog rule, reported only when nothing above is). "Named in
the documentation" is Morph's finding from the web_fetch results themselves, never Claude's words. Warnings, shown
under "Check before approving": the documentation has text aimed at AI agents (quoted), Morph's default key pattern
(the documentation gives none), a homepage left out, an endpoint path not found in the pages read, a DNS check that
couldn't run (the runner checks every address before each call), and the catalog's own warnings. Owner review is the
last control: documentation that is malicious from end to end can't be told apart by these rules.

**Approve** (`Drafts.approve(draft, sha256, by=)`): a ready draft, with the sha256 it holds now (409 otherwise, and a
clash that appeared since makes it refused); writes the spec, pins it, checks that the reloaded catalog has that
sha256, turns its tools on and grants each an approval in mode `draft` (bound to the tool's fingerprint as in section
15), so the next run asks only for the key. A run already working keeps the tools it was sent. `catalog.enabled_names`
keeps a pinned provider's tools off while its file's sha256 differs from the pin, `Toolbox.set_enabled` answers 409 for
them, and `Toolbox.tools()` adds `drafted {sha256, changed, by, at, draft, docs}` to each such tool and `drafts` (the
drafts drafting or ready). Each catalog provider now carries `sha256` (`catalog.fingerprint` of its whole manifest).

**Routes** (rows appended to `teams/roles.py` ROUTES):

| Route | Role | Body or query → answer |
|---|---|---|
| `POST /api/tools/draft` | owner | `{need (3-300), docs_url, why? (300), run?, tool_use_id?}` → 202 `{ok, draft, already: false}`, or 200 with the same draft still drafting; 400 bad input or a value that looks like a key, 409 agent tools off, 429 two drafting, 503 no Claude; team audit `tool_draft_started {draft, docs_host}` |
| `GET /api/tools/drafts` | viewer | `?id=` one draft, else the project's drafts, newest first (`?status=`, `?limit=` 1-50), plus `can` and `ready` |
| `POST /api/tools/drafts/approve` | owner | `{draft, sha256}` → `{ok, draft}`; team audit `tool_draft_approved {draft, provider, sha256, tools, hosts}` |
| `POST /api/tools/drafts/discard` | owner | `{draft}` → `{ok, draft}` (409 once installed); team audit `tool_draft_discarded` |
| `GET /api/tools/suggest` | editor | `?need=` (3-300, refused when it looks like a key) → `darwin.suggest(need)` |

`can` gains `draft` (owner; true without Teams). A draft's view is the stored draft without request, prompt and log.

**Darwin's suggestions** (`darwin.suggest(need, limit=4)`): Darwin's public search (6 results, no retry, 15 s) and each
agent's public capability record (`capability(id)`: owner, website as a plain https link, sign-in, payment; cached 7
days in Darwin's cache folder) → `{need, status, question, suggestions: [{name, owner, website, darwin_url, ready,
sign_in, paid, summary}], cached, error?}`. With Darwin lookups off: saved answers only. Suggestions only: Morph never
connects to a suggested service, signs in or pays; Darwin's question is shown, never answered for the person.

**Events and audit**: `tool_draft {draft, status, project, title}` on the project's room hub (known keys removed).
Audit (`.tools/audit.jsonl`): `tool_draft_started {draft, docs_host, project, run}`, `tool_draft_ready {draft, pages,
provider, tools, hosts, sha256}`, `tool_draft_refused {draft, pages, problems: [codes]}`, `tool_draft_not_possible`,
`tool_draft_failed {reason}`, `tool_draft_approved {draft, provider, tools, hosts, sha256, replaced}`,
`tool_draft_discarded`.

**Front end** (tooldraft.js): the need card (tools.js, from `morph_tools_request`) shows Darwin's suggestions to
editors and the owner (at most 3, a short note), "Draft a tool (preview)" to the owner (others: ask the owner) and,
once a draft exists, where it stands. `window.dispatchEvent(new CustomEvent("morph:tool-draft", {detail: {need, why,
run, tool_use_id}}))` opens the dialog (need, documentation address, the rules, Darwin's suggestions and question;
without Claude it says so and opens Keys on `ANTHROPIC_API_KEY`; with agent tools off it says so and opens Keys).
Keys' "Tools agents can use" gets "New tool (preview)", a "drafted" tag, and a changed drafted tool's switch disabled
with its reason. A
draft's card in the chat: Drafting (Stop), Ready for review (Review and approve, Discard), Won't install (the reasons;
Show the draft, Draft again, Discard), No tool for it, Couldn't draft, Installed (Add the key, Run Live prices again).
The review dialog shows hosts with the documentation's line and address check, each lookup's URL, inputs and the line
that documents it, where the key goes, cost, privacy, pages read, sha256, warnings, Claude's notes and the spec;
only the owner sees Approve and install.

**Tests**: `tests/test_toolbox_draft.py` (the request, every refusal from scripted answers, approval, change after
approval, restart, expiry), `tests/test_tools_draft_api.py` (routes, roles, events, Darwin from recorded answers in
`tests/fixtures/darwin/`), `tests/test_tools_draft_ui.py` (browser), `tests/e2e/test_e2e_tool_draft.py` (`MORPH_E2E=1`:
the real Studio with the stand-in's `tools` script), `tests/test_toolbox_draft_live.py` (`MORPH_LIVE=1`, real Claude
and a real documentation page; not run yet). `tests/fake_claude.py` adds the fictional Gearbase API's documentation
(clean and with a planted paragraph for AI agents), `gearbase_form()`, `drafting()` and `--script tools`.

### 15.3 Adversarial review of the tool broker (B2-T4)

A review of sections 15 to 15.2 before the merge. What changed in those contracts (each with a test that failed before):

- **Redaction before shortening** (`http_tool`): every text and object key in an answer, and an `http_error` detail, has
  the known keys removed before it is cut to `max_string` (2,000), 100 or 300 characters; cutting first could leave
  the start of a key that a service echoed back in Claude's request, the run file and the closing summary.
- **One deadline, the lookup included**: `http_tool.vet_within(host, port, timeout, policy=, resolver=) -> [ips]` runs
  `vet_addresses` within the time left (`TimeoutError` past it; an address found too late is never used), and `run()`
  passes the transport only what remains of the call's deadline. The draft's address check uses it with 10 s.
- **The switches stop a working run**: `Toolbox(..., tools_on=callable)` (default `settings.agent_tools_enabled`) and
  `network` are checked before every Claude request and every tool call; either one off pauses the run as
  `interrupted` with reason `offline` (network lookups) or `tools_off` (agent tools). Continue while it is still off
  answers 409 (agent tools) or 503 (no Claude without network lookups), and `Toolbox.resume` itself pauses the run
  again without sending anything. A drafting conversation fails before its next request instead. The interrupted
  card reasons are now `restart | claude_unavailable | claude_key | offline | tools_off`. Before, a run with network
  lookups off kept sending requests to Claude (the policy answered each call "offline");
  `tests/test_toolbox_runs.py` now expects the pause.
- **Stop during a call or Claude's answer**: `cancel()` on a run another thread drives records the request (answer
  `{..., cancelling: true}`, audit `run_cancel_requested`); the driving thread ends the run `cancelled` when the call or
  answer returns, also when that answer would have paused it (`_pause` and `_interrupt` honour it), and hands the
  claim back under the toolbox's lock, so a key saved at that moment can't resume a stopped run.
- **7 days means gone**: `_load` returns nothing for a run or draft untouched for `TTL_S` (`get`, `decide`, `resume`,
  `cancel`, approve and discard answer 404), not only at the next start; `recover()` still deletes the files.
- **Drafts**: a refused draft's review links a key page only when it is https to a public host name (`draft._link`, and
  `linkable()` in tooldraft.js); a provider id that is a Windows device name (`security.WINDOWS_RESERVED`: con, aux,
  nul, com1-9, lpt1-9) is installed as `<id>-<sha8>.json`.
- **Keys** (`keys.tool_keys`, `keys.save`): a saved `MORPH_TOOL_*` key that no tool that is on declares is listed with
  `unused: true` (`used_by: []`, and `service: "Keys no tool uses"`, `provider: null` when no loaded manifest declares
  it), so the owner can remove it; `save()` always accepts removing such a key and still refuses a value for a name no
  manifest declares. `morph doctor` marks it. Before, the key vanished from Keys but stayed in `.env`.
- **Front end**: Stop shows "Stopping" until the run ends (`cancelling`); an interrupted card offers the owner **Open
  Keys** for `offline`, `tools_off` (`morph:keys` with `network_enabled` or `agent_tools_enabled`: keys.js focuses that
  switch) and `claude_key` (`ANTHROPIC_API_KEY`), and tells others to ask the owner; a refused key's card says why it
  may have been refused; Allow and key cards tag a tool that isn't curated "drafted" or "your own manifest"; a refusal's
  note reads "Claude declined this request (safety category: <category>)." A view that says a run is working or
  paused never replaces one that says it ended (an ended run never runs again): a decide or start answer, or a list,
  that left the server before the run ended used to take a finished card back to Working, with Stop, until the next
  list arrived.

Guards added that pass before and after (`tests/test_toolbox_hardening.py`): marker keys searched for in every Claude
request and header, event, API answer, run, draft, pin and audit file, the server's output and an export; two tabs
answering the same card at once (one wins, the other gets 409 "This run is already continuing."); every tools route
for owner, editors and viewers of other disciplines and a removed member (401); remote mode with and without the
access code; a service answer written as instructions; odd run and draft ids; symlinked and hidden manifests. The UX
pass (1280x720, 1600x900, 910 px; every card state) found no console errors beyond the expected 409, no CSP
violations and no sideways scrolling.

Not changed here (other features' files): the New robot request and Teams comments don't refuse a pasted tool or
SubQ key the way chat does (`security.looks_like_secret`; Teams uses its own `collab.SECRET_RX`).

## 16. Simulation track: robustness studies (B2-S1 the engine, B2-S2 the Studio routes and the card)

One nominal simulation says a design works once, exactly as drawn. A **study** runs a revision's own nominal
pick-and-place plan (planned once, as the Studio's simulation plans it) many times in worlds drawn from the project's
tolerance spec, labels every run, and reports the success rate with its interval, the failure groups, the one rule
that best isolates each and whether it survives other solver settings. Simulation, for the tolerances shown; never a
field failure rate (section 0, honesty).

- **Code.** `morph/services/robust.py` (the engine: spec, sampling, workers, analysis, storage) and
  `morph/studio/jobs.py` (heavy jobs beside the build queue); both outside `pipeline_version()`. `simulate.py` has
  only the hook below. `tests/test_sim_golden.py` pins every nominal output (robot.xml, each `sim_<scenario>.json`
  with its frames, `run_all`'s return value, the MuJoCo state after each scenario) for the egg-picker (full and fast),
  the SO-101 and a world with a box on the carry path: with `perturb=None` the simulation stays bit-identical
  (hashes per environment; elsewhere the test is skipped). The H2 and H7 changes to `simulate.py` keep it green.
- **Hook** (`simulate.py`, pick_place only): `run(asm, design, "pick_place", perturb=...)`, or for a study
  `_plan_pick_place(rob, design)` once, then per run `with _perturbed(rob, p): _run_pick_place(rob, design, plan, at,
  p, on_step)`. Keys, all optional (absent = nominal): `off_across_mm`, `off_along_mm`, `yaw_deg` (the object's start,
  relative to its long axis), `size_scale`, `mass_g`, `friction_scale` (pads and object), `grip_scale`, `torque_scale`
  (servo torque and P gain), `deadband_deg`, `encoder_cpr`, `encoder_noise` (counts), `noise_seed`, `rate_hz` (host
  set-points, held between updates), `mount_dx_mm`, `mount_dy_mm`, `mount_yaw_deg` (the room moves the other way),
  `box_offsets_mm` ([dx, dy] per world box), `timestep`, `impratio`. The plan, the host's gains and the targets stay
  nominal; `_perturbed` puts the compiled model back bit for bit (also when a run raises). A run that moves the room
  turns MuJoCo's midphase off for that run: the compiled collision hierarchy still has the boxes where they were, and a
  moved box would otherwise be passed through.
- **Tolerance spec** (`robust.default_spec(design, world)`, `tolerances(root, rev)`): entries `{name, label, unit,
  lo, hi, nominal, source, on, edited}` in a fixed sampling order (never reorder or insert):
  `object_across_mm`, `object_along_mm`, `object_yaw_deg`, `object_size`, `object_mass_g`, `pad_friction`, `grip_force`,
  `servo_torque`, `servo_deadband_deg`, `encoder_noise`, `setpoint_hz`, then in World mode `mount_dx_mm`, `mount_dy_mm`,
  `mount_yaw_deg`, `box_error_mm`. Defaults and sources follow the research (egg mass 50-72 g from USDA weight
  classes, FSR 400 calibration, ST3215 torque, backlash and 4096-count encoder; the rest "assumed"); other objects and
  servo families get relative ranges marked "assumed". A project's edits live in `<p>/.studies/tolerances.json`
  (`save_tolerances(root, {name: {lo?, hi?, on?}} | {"world_preset": name} | {"reset": true}, rev, by)`: finite numbers
  inside `robust.LIMITS`, `lo <= hi`; anything else is a `StudyError`). A study records the spec's sha
  (`base_spec_sha`); once the project's spec changes, `load()` marks the older study `outdated`. **World presets:**
  `robust.register_world_preset(name, fn(sim_world, design) -> the four world entries)`; `standard` is mount +/-10 mm
  and +/-2 deg, each box 0-15 mm (H2 adds scan presets). Stress mode doubles every range about its nominal (floors in
  `robust.FLOOR`).
- **Runs.** Run `i` of a study draws its world from `SeedSequence([seed, i])` only (every entry draws, also when
  switched off, so editing one range never moves the others): outcomes do not depend on the number of workers or the
  batch size, a deeper study extends a shallower one, and the default seed (1) reproduces the published egg-picker
  study (150 of 200 at 1 ms; 140 before the simulation gripped at the gripper stage's 2.34 N). Outcomes: `placed`; `jam` (the object rose more than 10 mm after the release or ended
  more than 45 mm off: the jaws stayed shut and carried it), `drag`, `no_force_reading` (a hobby gripper's
  force-sensing close gave up on an object narrower than its probe: `metrics["grip_gave_up"]`, section 22),
  `stalled` (a sensorless hobby gripper's close held its servo at stall on an object wider than the gauge, the object
  intact: `metrics["grip_stalled"]`, section 22), `knocked` (the object moved more than `robust.KNOCK_MM`, 20 mm,
  before the grasp phase began: the arm hit it on the way, a real failure of the layout, so it counts; row
  `knocked_mm`; the default egg-picker's resting hand sits 1.07 mm from a 200 mm water bottle's end, so one 1.5 %
  over its size is hit and rolls off the table, which read `missed_grasp`), `missed_grasp` (not lifted half the
  hover height),
  `dropped` (more than 12 mm of slip in transit), `over_force` (the product's damage check), `unreachable`,
  `world_collision` grouped as `world_collision:<phase>` (the plan's phase at the first contact); `unstable` (MuJoCo's
  BADQPOS, BADQVEL, BADQACC or BADCTRL warnings, after which MuJoCo restarts the run from its first frame, a full
  contact or constraint buffer, or a non-finite state) and `error` runs are listed and never counted.
- **Statistics.** Wilson 95 % for the success rate and every group; Clopper-Pearson (exact) when no run, or every run,
  fails (200 runs, none failing: at least 98.17 %, shown as 98.1). Each group gets the one-parameter interval (on the
  value, or on the distance from nominal for two-sided ranges; 26 quantile cuts) with the best Youden's J, `significant`
  when above the 95th percentile of what 60 shuffles of the outcomes reach. **Model sensitivity:** every group's runs
  (at most 20) and 15 successes are re-run at half the timestep and with `impratio=1`; a group is `model_sensitive`
  when, under either, fewer than 80 % of its runs still fail the same way or at least 20 % (and 2) of the successes now
  do. Copy: section 0's honesty notes and heavy_plan's notes 1-3 ("This result changes with the simulator's friction or
  timestep settings. Test it on the real robot." is `robust.SENSITIVE`).
- **Workers.** `multiprocessing` with the spawn start method on every OS, `MORPH_STUDY_WORKERS` (default cores minus
  one) at `MORPH_STUDY_NICE` (default 5; below-normal priority on Windows). A short-lived process rebuilds the
  revision's robot the way the pipeline built it and plans once; the robot travels to the workers packed (pickle, only
  over the study's own pipes, never through a file) with a mesh-free skeleton of the assembly, so a worker is up and
  loaded in about 0.3 s on an idle machine (0.9 s at load 9-14) at about 140 MB. A rebuilt model that differs from the
  revision's `sim/robot.xml` is reported (`model.matches_revision: false`). A dead worker's runs are tried once more
  elsewhere. Study processes mute MuJoCo's warning handler (it writes `MUJOCO_LOG.TXT` into the current folder) and read
  the warning counters instead.
- **Storage** (`<p>/.studies/`, a hidden folder: never a project, never exported): `tolerances.json`; `active.json`
  (the running study's lock, its heartbeat is the file's mtime: older than 30 s means it stopped); `<id>/study.json`
  (definition, effective spec, status `queued|preparing|running|checking|done|cancelled|error`, read back as
  `interrupted` when its process stopped, `model`, `timing`, `summary`); `<id>/runs.jsonl` and `<id>/checks.jsonl`
  (one row per run, about 0.5 KB). `<id>` is 12 hex digits of the sha256 of (revision, design, pipeline version,
  seed, timestep, stress, spec). Studies never write inside `rNNN/` and never change a stage hash.
- **Heavy jobs** (`morph/studio/jobs.py`): `jobs.manager(studio)` (one per Studio; `HeavyJobs(home, emit=...)`
  elsewhere): `start_study(project, rev=None, *, runs=200, seed=1, stress=False, workers=None, timestep=0.001,
  checks=True, by=None)` -> job (`StudyError`; `StudyBusy` when one runs for that project, in any process), `status`,
  `cancel` (within 2 s; finished runs kept), `wait`, `resume(project, id)`, `interrupted()`, `result(project, id,
  rows=False)`, `studies`, `tolerances`, `save_tolerances`, `replay(project, id, run)` (a run's
  `sim_pick_place.json` document with a `robustness` block, for the viewer). A job never takes the room's lock, its job
  queue or `Studio.builds`; jobs on different projects take turns (`MORPH_HEAVY_JOBS`, default 1 at once).
- **Event** `study {id, rev, status, phase: prepare|runs|checks|done, done, n, successes, failures, unstable, groups, p,
  ci, eta_s, elapsed_s, first_result_s, stress, summary?}` to the project's room (`groups`: the counted failures so far
  by group, B2-S2), at most four a second plus every phase change and the end (`summary`: n, successes, p, wilson95,
  clopper_pearson95, groups with id, label, count, rate, model_sensitive).
- **CLI:** `morph study <project> [--rev rNNN] [--runs N] [--workers N] [--stress] [--seed S] [--timestep ms]
  [--no-checks]` prints the study as ASCII text; Ctrl+C keeps the finished runs and the same command carries on.
- **Studio routes (B2-S2, `morph/studio/api_study.py`)**, all with `?project=`; starting, cancelling, resuming and
  editing tolerances need an editor, reading and replays a viewer (Teams' role table lists all seven):
  - `GET /api/study[?rev=]`: `{job, studies, latest, latest_stress, stopped, limits, host, rate, may, notes,
    group_labels}`. `latest`/`latest_stress` are the newest plain and stress study of `rev` (default: the newest
    revision; never one whose revision now holds another design, `rev_changed`) as records with `card`; `stopped`: the
    cancelled or interrupted studies that can carry on; `limits {max_runs, max_workers, workers, timesteps_ms,
    default_runs}`; `host {name, cores, remote}` (studies run on the Studio's computer: with `--remote` that is how a
    team brings its compute); `rate.per_worker` (runs/s per worker process at 1 ms, as the shown studies or else the
    project's newest timed study measured it, or 0.7); `may.start` (false for a Teams viewer). `GET /api/study?id=`
    gives one record with its `card`.
  - `POST /api/study {runs?, stress?, rev?, seed?, timestep_ms?, workers?, checks?}` -> 202 `{job}`. Refused with a
    plain reason: `timestep_ms` 0.5, 1 or 2; at most `MAX_RUNS` (10,000) one-millisecond runs' worth (5,000 at 0.5 ms,
    20,000 at 2 ms; `morph study` takes more); at most one worker process per core; 409 while one runs for the
    project (any process), 404 for a revision that isn't there. The same settings again carry on with, or deepen, the
    saved study.
  - `POST /api/study/cancel` (409 when none runs), `POST /api/study/resume {id}` -> 202.
  - `GET|POST /api/study/tolerances {changes, rev?}`: the spec with each entry's `limit` (what an edit may use) and the
    world `presets`; `changes` as `save_tolerances`.
  - `GET /api/study/replay?id=&run=[&variant=]`: the run's `sim_pick_place.json` document, simulated again in a spawned
    study process (`robust.replay(process=True)`: MuJoCo's log and the CPU stay out of the Studio's process), one at a
    time per Studio, the last eight kept in memory; 503 (try again) after 120 s waiting for another replay, or when its
    process can't start, stops or takes over 5 minutes; `robustness` adds `recorded` (the row the study saved),
    `matches_record` (false: Morph's simulation changed since) and `label`.
  - `api_study.card(doc)` and `headline(doc)` give the card's words (heavy_plan section 6): "Succeeds in 140 of 200
    randomized runs (95% CI 63-76%); the nominal run succeeds." (interval bounds rounded away from the estimate, so the
    shown interval always holds the computed one; all or none succeeding uses the exact bound, "at least 98.1% with 95%
    confidence"), `NOTES["simulation"]`, the flags (outdated, revision gone or changed, stopped part way, unstable and
    error runs, rebuilt with other Morph code) and each group's share, rule (with "could be chance"), sensitivity
    (`robust.SENSITIVE`) and example run. `NOTES["none"]`: "One nominal run. Small differences in placement or hardware
    aren't tested."
  - The Studio stops a project's study before Delete project and Reset demo (`jobs.stop(studio, name)`, up to 5 s; its
    runs are kept, in the trash with a deleted project) and every study when it stops (`jobs.shutdown(studio)`).
  - `robust.load()` adds `rev_changed`; `studies()` reads each revision's spec once.
- **The card (`static/study.js`, feature `study`):** under the pick-and-place result in the Simulation tab (section 4):
  the note where no study exists; live progress (`study` events: phase, run k of n, the success rate so far with its
  interval, failures so far by group, time left estimated); the result with its flags, at most two failure groups
  (label, share, "Most likely when: <rule>", the model-sensitive or "Same under other solver settings" badge, Replay)
  and Details (a dialog: every group with its check counts, what matters most (Youden's J against chance), every
  tolerance with its source, editable for editors, and the project's studies); for editors a start row (50, 200 or
  1,000 runs, Stress test, "Run N more" to deepen, an estimate of the time on the Studio's computer), Cancel and Carry
  on. A replay plays in the 3D view (the viewer's title names the run) until the tab plays something else; without a
  3D view the card gives the run's numbers. A Teams viewer's page (`body.tm-readonly`, `may.start` false) keeps the
  results and replays and loses the controls (`.sd-edit`).
- **Whole-path clearance (B2-S3, H2; `simulate.py`, world mode only).** The plan is cut into moves (`simulate.MOVES`:
  approach, descent, grasp, lift, carry, set-down, release, rise, return; `worlds.MOVES` is a copy a test keeps equal),
  each checked against the boxes at every 10 ms plan sample with the object while it is carried (`CARRIED`). A move that
  comes within `CLEAR_MM` tries its alternatives and keeps the first that stays 10 mm clear, else the one with the most
  room (`_choose`): the approach `direct`, `carry path`, `from above` (`rise_mm`: 40, 80 or 120 mm, `LIFTS_MM`; the
  empty hand may tilt on the way); the release `full` or `opened less` (`open_mm`: `OPEN_LESS_MM` = 4 mm past the
  object); the way back `direct`, `direct, hand closed`, `carry path`, `carry path and approach`, `straight up first`
  (`rise_mm`); the carry arc keeps E3's family, now confirmed every 10 ms. The pick and place spots never move.
  Pick-and-place metrics add `path` (one `{move, phase, min_clearance_mm, clear, at?: {t, link, obstacle}}` per move)
  and, when a move is still planned into a box, `blocked` (that entry): the simulation fails "the <link> would pass X mm
  into <box> during <move> at T s, and no clear way around it was found" (a contact in the run reads "the <link> hits
  <box> during <move> at T s"; `world_contacts` entries add `move` and `phase`), and `clearance_ok` is false.
  `detours` gains `release` and the new `via` values; `GET /api/world` returns `sim.path` and `sim.blocked`, and the
  World panel's status line says which way the arm went. Simulations without a world are bit-identical (golden hashes
  unchanged); the world golden (`egg-picker-bin-world`) was re-recorded for the new checks. Planning in world mode
  takes at most about 0.4 s more (worst measured 0.91 s against 0.49 s, at load 7-14).
- **World-tolerance presets (B2-S3, H2).** `register_world_preset(name, fn, label=None, room_only=False)`;
  `PRESET_LABELS`, `ROOM_ONLY`. `room` ("Only the room varies: the mount and every box; the object and the hardware stay
  nominal"): the standard world ranges (mount +/-10 mm and +/-2 deg, each box 0-15 mm) with every other entry off, so
  every failure is the room's; entries still draw in the fixed order, so a room study's worlds match a standard one's.
  `GET /api/study/tolerances` adds `preset_labels`; the card's tolerance dialog shows a World tolerances select
  (`data-f="preset"`, editors only); a room-only study's card carries `api_study.NOTES["room_only"]`. Measured: a carton
  wall 12 mm from the open jaws at the place spot, 200 runs at 1 ms, seed 3: 185 of 200 succeed (Wilson 88.0-95.4 %),
  15 `world_collision:release` (7.5 %, 4.6-12.0 %), rule "mount 4.5 to 10.0 mm off in y", not model-sensitive.
- **Clearance to the room scan (B2-S3, H3; `morph/studio/worlds.py`).** `scan_clearance(glb_path, world, doc,
  robot_xml, *, near_mm=10, floor_cut_mm=10, time_limit_s=20, max_faces=MAX_FACES, per_pose=False)`: the revision's
  simulated frames (`sim_pick_place.json`, posed about every 10 ms) in its own collision model (`sim/robot.xml`, the
  object while carried) against the uploaded scan. The scan is cropped to within `SCAN_CUT_MM` (50 mm) of a geom's
  bounding sphere at some pose (64 mm cells), big triangles cut to the part near the arm, triangles rising no more
  than `floor_cut_mm` above the mount surface dropped; each triangle is covered by its own points (`_scan_samples`:
  rows along its longest edge at most `SCAN_SAMPLE_MM`/2 apart, points at most `SCAN_SAMPLE_MM` apart, so no point of
  it is further than rho <= 1.41 mm from one) in a scipy cKDTree (sliding midpoint, no compact nodes, every query
  bounded). Robot geoms (fixed ones once) are pruned by bounding sphere, then coarse hull samples (8 mm); the fine
  samples (1 mm edges, 2 mm faces) are measured against the tree, and those that could be the closest get exact
  point-triangle distances (`closest_on_triangles`) to every triangle with a point within their bound. Two passes:
  every pose coarsely (bounds), then the most promising first; found to `SCAN_TOL_MM` (0.05 mm); below `SCAN_TOUCH_MM`
  (0.5 mm) it touches. Returns `{min_clearance_mm (None beyond 50 mm), at {t, link, point (world mm)}, regions,
  stats, note}`, with `per_pose` also `poses [[t, mm|None]]`; never raises for a bad scan or model:
  `{"skipped": reason, "note"}`. `regions` (at most `SCAN_MAX_REGIONS` = 8, the closest first): the near misses
  (closer than `near_mm`) grouped by 30 mm, each `{id: "scan-k", label: "Scan near miss k", min_clearance_mm, t, link,
  point, points, box {center, half, yaw_deg}}`: the scanned surface within 30 mm of them in the world frame along the
  mount surface's axes, at least 20 mm thick (grown away from the arm), down to the surface when it or the scan under
  it nearly reaches it. Measured: within 0.05 mm of `mj_geomDistance` on box fixtures (54 and 106 poses; up to 0.21 mm
  over whole runs); a generated room of 998,664 triangles in 6.5-8.3 s at load 3-6 and 10.6 s at load 7-11
  (`MORPH_SLOW=1`).
  `scan_check(scan)` is the check (`id: "scan"`, "Clearance to the room scan", view `along_path`): pass, warn (closer
  than 10 mm, touching, or checked only in part) or skip, never fail, its text ending with `SCAN_NOTE` ("Measured
  against the scanned surface, which can be noisy or have holes. A warning, not a verdict."); `regions` ride on it.
  `checks(..., scan=)` and `inspect(..., scan=)` add it after `clearance`; `rules_review` advises on it
  (`_ADVICE["scan"]`). `POST /api/world/inspect` measures it when the world has `room.glb` and the revision was
  simulated in this room as it is now (else a skip saying so), kept in memory per (project, revision, world sha, room
  file size and mtime), the last eight; it sends `world {stage: "scan", status: "start" | "done", text}` and an `agent`
  event (`feasibility`, `code`).
- **Add box here (B2-S3, H3; `static/world.js`).** Under the scan check, each region has a row (what is near, when,
  the box size) and **Add box here**: the box joins the world's grey boxes as `{id: region id (made unique), label,
  center, half, yaw_deg, source: "auto"}`; *Save and simulate* then plans around it, and the next check measures the new
  path. Browser test: `tests/e2e/test_e2e_world.py::test_add_box_here_from_the_room_scan`.
- **Under attack (B2-S4).** An adversarial review of the track; what other code relies on since:
  - `robust.StudyConflict` (a `StudyError`; the routes answer 409) for what can't run as asked: a revision with nothing
    to study (`robust.studyable(p, rev)`: the simulation stage failed or never ran, a preview-only build, no pick and
    place; `new_study` checks it before a job is queued, so `POST /api/study` answers at once instead of 202 and a
    failed job), a gripper-only project (`meta.json` whose kind isn't `robot`), carrying on a study whose revision now
    holds another design (`robust.REPLACED`) or whose pipeline code changed since (`robust.CODE_CHANGED`), and replaying
    a study whose revision was replaced.
  - Locks and restarts: `active()` treats a lock whose `pid` is gone from this computer (its `host`) as stale at once
    (`robust._alive`; elsewhere the 30 s heartbeat decides, as before). The heartbeat also saves the study's `done`.
    `load()` reads an interrupted or failed study's `done` and `summary` from its saved runs (kept per `runs.jsonl` size
    and mtime) and adds `code_changed`; `studies()` lists it. `GET /api/study`'s `stopped` and `HeavyJobs.interrupted()`
    leave out studies that can't carry on (revision missing or replaced, code changed). A job's `id` follows its
    events (a study whose tolerances were edited while it waited is another study).
  - Sizes: `robust.cores()` (the cores this process may use: `os.process_cpu_count()`, else the affinity mask),
    `default_workers()` never above it (also with `MORPH_STUDY_WORKERS`), `run_study(workers=)` 1 to
    `robust.MAX_WORKERS` (256) or `StudyError` before anything is locked, `morph study --workers` 1 to `cores()`, and
    `limits.max_workers`/`host.cores` report `cores()`. Tolerance edits take `on` only as true or false and `reset`
    only as `true`; a change that isn't an object is refused by name.
  - Replays: `GET /api/study/replay` checks the study, run, check and revision (`_replayable`) before it waits for the
    one replay slot, and at most `api_study.REPLAY_QUEUE` (3) wait; the next is answered 503 at once.
  - Numbers: a group that holds every counted run has `rule: null` (nothing left to separate); rule text reads "X% fail
    this way vs Y% otherwise"; `summary.successes_sensitive` is true when, under either solver setting, at least 20 %
    (and 2) of the counted re-runs of successful runs fail (null when nothing was re-run), shown as the card's flag
    `api_study.NOTES["successes_sensitive"]` (`robust.SUCCESSES_SENSITIVE`) and a line of `report()`; a study whose runs
    were all unstable or errors has the headline `robust.uncounted(k)` ("No run could be counted: ...") instead of none.
  - The scan check's memory (`api_world._SCANS`) also keys on the revision's `sim/sim_pick_place.json` (size, mtime) and
    the sha256 of its `sim/robot.xml`: Reset demo reuses revision names.
  - Study processes are spawned on every OS, so each imports the main module again: scripts that call `robust` guard
    their work with `if __name__ == "__main__":` (`morph`, `python -m morph.cli` and pytest already do).
- **Ownership.** The simulation track owns the study hook in `simulate.py` (the `perturb` keys, `_perturbed`,
  `_plan_pick_place`, `_run_pick_place`, `_scenario_doc`) and the world-path names (`MOVES`, `CARRIED`, `CLEAR_MM`,
  `PLAN_DT`), `morph/services/robust.py`, `morph/studio/jobs.py`, `api_study.py`, `static/study.js`, and the scan check
  in `worlds.py`/`api_world.py`. Other packages call `simulate.run(...)` without `perturb`. A change that moves a
  nominal output re-records `tests/test_sim_golden.py` on purpose and says so in its report. Changing what a
  perturbation key means, the tolerance order (`robust.BASE`, `robust.WORLD`) or the draw order changes every saved
  study: bump `robust.VERSION`, so the same settings start a new study and older ones can't be carried on.
- **Docs:** `docs/ROBUSTNESS.md` (what a study is, its tolerances and their sources, how to read the card,
  model-sensitive results, restarts, `--remote`, limits); `docs/WORLD.md` covers the whole-path check, the scan warning
  and Add box here.

## 17. Integration of the B2 tracks (B2-M)

The simulation track (section 16) and the tool broker (section 15) are merged into the release branch, in that order
(`--no-ff`), on top of three release fixes made after their base. What other code relies on since:

- **Shared files.** `FEATURES` is `[newrobot, importer, video, team, world, study, tools]` (section 4); this file has
  section 15 before 16; `teams/roles.py` has the study routes after world mode's and the agent tools' at the end; the
  release checklist's manual QA rows 16-17 are the simulation track's and 18-23 the tools'.
- **The Studio's lifecycle (server.py), in order:**
  - start: the features load (`_start_features`), then the toolbox (`api_tools.attach`, which `recover()`s); the study
    manager is made on first use (`jobs.manager`).
  - a finished build (`Studio.apply` and `Studio.build`, after `done`): `api_tools.after_build`.
  - Delete project: `jobs.stop(studio, name)` (the study stops; its runs go to the trash with the project), then
    `api_tools.project_gone(studio, name)`, then `projects.delete`.
  - Reset demo, per demo project: `jobs.stop`, then the revisions after the kept build are removed and
    `project_gone(studio, name, revs)` gets the ones that were.
  - stop (`serve()`'s `finally`): `jobs.shutdown` (studies stop and keep their runs); agent runs left working become
    interrupted pauses at the next start.
- **`api_tools.project_gone(studio, project, revs=None) -> [run ids]`**: the project's agent runs that are running or
  paused end as `cancelled` (`Toolbox.cancel(run_id, by=, note=)`, whose `note` defaults to "Stopped by the person."):
  all of them when the project is deleted ("Stopped: <project> was deleted."), and only those whose `context.rev` is in
  `revs` on Reset demo ("Stopped: Reset demo removed the revision it was for."). Finished runs are left as they are.
  Runs live in `MORPH_HOME/.runs` under the project's *name* and revision, and both come back (a new robot from the same
  request gets the same name; Reset demo reuses revision names), so a run waiting for a key could otherwise continue,
  once the key was saved, in a project or on a revision it was never started for (`tests/test_tools_lifecycle.py`).
- **End-to-end tests:** `MORPH_E2E_PORTS` covers every Studio and stand-in the browser tests start and is shared out
  between pytest-xdist workers (`live_studio.ports()`, section 14); the World mode and Teams flagship tests take their
  ports from `live_studio.free_port()` and give them back with `release_port()`.
- **Known gaps:** a run that ended in a deleted project still lists in a new project of the same name, so its card
  (stopped, with no actions) shows there for half an hour after it ended, as every recent card does; deleting a
  project leaves the drafts of tools started from its cards (they belong to the owner's catalog, not the project).

## 18. Grip force, servo rail and stock grippers (release review fixes, track E1)

Fixes of the release review's engineering findings. What other code relies on since:

- **The gripper actuator (ENG-15).** `pipeline2.gripper_actuator(design)` is `arm.gripper_actuator(design)`: the
  actuator sizing picks and the CAD hand is built around, a `gripper=` entry in `arm.actuator_overrides` included. The
  gripper stage (force plan, grasp simulation, the stall force shown) used the family's default servo while the CAD
  hand and the electronics used the override. `morph.electronics.servo_spec(key)` gives any robot catalog actuator in
  the shape of the gripper-only `SERVOS` table, so the gripper stage runs with an XL330 or a 7.4 V STS3215 too.
- **One grip force (ENG-1, ENG-7).** The gripper stage's force plan (`results.json` `gripper.force`:
  `window_n`, `target_force_n`, `limit_n`, `hold_force_n`, `sensor`) is the only place a grip force is decided.
  `pipeline2.gripper_stage(design, sizing=None, n_trials=4, simulate=True)` computes it: for a generated arm the
  gripper analysis, grasp simulation and `morph.electronics.plan`; for an imported robot `imported.gripper_summary`,
  whose stock-jaw window uses the simulation's closed form for flat jaws (it slips below 1.5 x m g / 2 mu, is damaged
  above `max_force_n` / 1.5; the target is the Grip force target when set, else the window's own with a 0.5 N floor,
  at most 80 % of the servo's squeeze at the grasp lever, section 20). `morph.electronics.force_window(o, hold, sensor, target,
  floor)` is that formula; nothing else computes a target. `electronics2.plan(design, sizing, grip=force)` copies the
  plan into `eplan.grip` (a plan's own grip passes through; without one it runs `gripper_stage(simulate=False)`,
  keeping a partial plan's or `sizing["grip_target_n"]`'s target) and adds an issue when the target is outside its
  window or above its hard stop. The robot builders put `eplan.grip.target_n` in `asm.meta["grip_target_n"]`, so the
  simulation grips with the firmware's target. The firmware comment, the summary and the manual (`manual.json`
  calibration step: `grip_target_n`, `grip_window_n`, `grip_limit_n`; the text names the firmware's F and Z commands)
  show the same numbers. The pipeline, `robust._assembly`, Teams' change-request preview and New robot's preflight all
  pass the force plan; `brief.preflight` returns it as `grip`.
- **The servo rail (ENG-6, ENG-9).** One voltage per arm, decided with the sizing by
  `electronics2.servo_rail(acts, requested)` (`acts`: one `{key, joint, torque_with_margin_nm}` per servo,
  `electronics2.rail_acts(sizing)` builds them): `design.electronics.servo_v` (new, default 0 = automatic; the
  standalone gripper's `supply_v` does nothing on a robot) when every servo's range takes it; else the servos' rated
  voltage when a catalogued supply gives it with the current; else the catalogued supply voltage nearest to it inside
  every servo's range that keeps each at `RAIL_MIN_TORQUE` (80 %) of its stall torque; else the rated voltage from a
  12 V supply through the adjustable servo buck. It doesn't depend on the controller choice (a custom PCB can't take
  the buck and says so), so `electronics.config` still reruns only the electronics. `sizing.servo_rail` holds it;
  `catalog.torque_at(key, volts)` (datasheet points in `torque_v`, in proportion below them, capped at the rating)
  gives every `torque_avail_nm`, margin, actuator pick and sizing issue, the robot builders' joint efforts (so the
  simulation's servos), and `summary.servo_rail_v`. `electronics2.plan` takes the rail from the sizing, picks the
  supply for it, warns which servos it derates (below 70 % with the advice to use one voltage class), and warns when a
  requested rail was not taken. `GRAPH`: `electronics.servo_v` reruns sizing (and so everything). The chat's "use a 5 V
  supply" sets `electronics.servo_v` when every servo takes it and otherwise changes nothing and says which servo's
  range rules it out; "use the rated supply voltage" sets it back to 0. A hand-written imported profile is still sized
  at q = 0, now labelled: `sizing.basis` = "q = 0" (the Studio's margin row reads "at q = 0"), and `sizing.warnings`
  names a joint whose margin over the whole joint range (the auto-import pose grid) falls below the payload margin;
  the inspector lists sizing warnings.
- **One voltage class per arm (ENG-5).** `catalog.FAMILY_GROUPS` (moved from `importers/autoprofile.py`) lists each
  family's voltage groups; `catalog.pick_in_groups(need, family)` gives each joint the weakest actuator that holds
  `need[joint]` (N·m with margin) on its group's servo rail, from the first group where every joint has one.
  `imported.actuator_plan(design, prof, need)` swaps an imported robot to another family this way (the hard-coded
  `FAMILY_SWAPS` list, which put XL430s and XL330s on one 6 V rail, is gone; `imported.size` passes the need), and so
  does `autoprofile.size_for_family`. The SO-101 swapped to Dynamixel gets six XL430-W250s on 12 V (2.27x shoulder
  margin, the six holder issues by design). `electronics2.rail_problems(acts, supply, config)` names every servo whose
  voltage range leaves out the rail it really gets (the supply itself on a custom PCB, which passes its input to the
  servos); those are plan issues, and `firmware()` and `finalize()` raise before writing anything, as for a mixed
  protocol, so the wiring stage fails with the reason. Present step 12's caption and `docs/DEMO_SCRIPT.md` row 12 say
  what the swap does (new supply, controller, wiring and firmware; six orange holders; not feasible until redesigned).
- **Stock jaws and the gripper torque limit (ENG-2).** A robot that keeps its own jaws (`arm.source` set) has no
  force sensors: `imported.gripper_summary` returns `sensor` "none" whatever `finger.sensor` says (a warning when it
  asks for FSRs), so no FSR is bought, wired, programmed (`PIN_FSR_L/R` -1) or calibrated, and the templates, New
  robot's SO-101 brief, the import session and the chat keep `finger.sensor` "none" for them. Force plans carry
  `jaw` ("rack" or "revolute") and `lever_mm` (pinion radius, or the stock jaw's lever at the simulated grasp,
  `imported.grasp_lever`; `JAW_LEVER_MM` / a profile's `jaw_lever_mm` only when that can't be worked out).
  `electronics2.grip_torque_limit(grip, gkey, servo_v, family)` gives `eplan.grip.torque_limit` (`force_n`, `nm`,
  `fraction` of the stall torque at the rail, `per_n`, `register`, `short`), None for hobby PWM: without force sensors
  it is the grip (the target at the jaw's lever; a rack jaw per finger F = torque x 0.6 / 2r), with them a cap at the
  firmware's hard stop. A sensorless grip the servo can't reach is an issue, and so is a hobby PWM gripper whose stall
  force exceeds the object's limit. The firmware writes it at start-up and when the e-stop clears (`gripTorque()`:
  Feetech register 48 in 0.1 %, DYNAMIXEL Goal PWM in percent) and F re-sets it on a sensorless gripper, up to
  `GRIP_LIMIT_N`: for an object with no damage force that is the jaws' stall force, so F 1000 writes the whole stall
  torque (register 1000 on the sensorless egg-picker hand with a soda can) and the bus servo holds it for the carry; F is the
  person's choice there, and the servo's own torque register is what stops it. A hobby force-sensing gripper's F
  raises the loop's target up to `GRIP_LIMIT_N` too (19 N on the egg-picker hand's egg), above the 10 N its MG996R
  holds continuously: the plan's warning and the manual say it then runs warm on long carries. A sensorless
  revolute jaw is simulated with that torque (`asm.meta["grip_torque_nm"]`). Firmware with force sensors that read
  under `GRIP_SENSE_N` through a whole close probes to `GRIP_MIN` (`GRIP_PROBE` is the whole overtravel past
  `GRIP_CLOSED`, section 20), goes back to `GRIP_CLOSED` and prints `!GRIP no force reading down to GRIP_MIN`. `tests/fw_stubs/grip_harness.cpp` runs the
  generated sketch's control loop on the host for the tests.

## 19. Brief, inspector and pipeline (release review fixes, track E2)

Fixes of the release review's brief, inspector and pipeline findings. What other code relies on since:

- **Damage force (UX-1).** `brief.damage_force(b) -> (N, where)` is the object's damage force and where it comes from:
  a measured value wins (Morph's preset or catalog object, or the object a video, a photo or a model described), and a
  stated category (`FRAGILITY_N`: "fragile" 3 N, "care" 10 N) gives one only to an object without it. Saying an egg is
  fragile once made it a 3 N strawberry while the card showed 25 N, and no grip both holds a 60 g egg and stays under
  2 N, so the README's example built a robot that failed its own pick and place. `brief_to_changes`, the card's object
  row and a new fragility assumption (its options are the other categories, as answers to the `fragility` slot) all use
  it; `parse()` returns the words a slot came from in `said`, kept in the slot's evidence ("request: without cracking").
  A grip issue on the confirm card ends with where the damage force came from (`brief.explain_issues`).
  `morph.electronics.no_safe_grip(lo, hi, o, sensor, jaw)` is the one sentence for a window with no force in it (the
  gripper stage's and the electronics plan's were two), and names what sets its top (`limit_source`: the object's
  damage force / 1.5, or the force sensor's range).
- **Build now (UX-1).** `POST /api/brief {action: "build", stop_on_issues: true}` on a brief that is still asking or out
  of scope fills the defaults and, when the preflight then has issues, answers 200 `{ok, brief_id, busy: false, brief,
  stopped: "preflight"}` without building; on a brief that is already ready it builds as before. New robot's Build now
  sends it, so the confirm card shows the issues first ("Not built yet"), with Build anyway.
- **Jobs inside a pick and place the gripper can't do (UX-3).** `brief.ADJUSTED` (`twisting`, `opening`, `assembly`)
  and `brief.adjusted_kinds(text)` read unscrewing and twisting, opening jars, bottles, lids and packaging ("open" as a
  verb only: "an open box" and "open-top bins" are not), and assembling, inserting and pressing; `parse()` returns them
  in `adjusted`, `merge()` keeps them in `flags["adjusted"]`, and `assess_scope` puts their plain note first, so the
  scope reads "Adjusted" and the robot is built for the moving part. Unscrewing says it all for an opening that
  twists. An assembly request moves the parts (task `pick_place`, no sorting camera). `CATALOG` has a glass jar
  (65 mm, 300 g) and a pill bottle (35 mm, 40 g), offered by the containers and small-parts archetypes only when the
  request names them (`Archetype.more`).
- **People the robot works for (UX-3).** `brief.parse` reads `people_nearby` from the person signals first
  (`brief.person_signals(people_text, text)`: children, someone beside it, someone it hands things to, someone it
  helps, serves or works for, "no one else"; the person patterns run on `brief.people_text(request)`, E-N8), apart
  from the task; someone who collects from it, checks on, calibrates, services or empties it is beside it ("customers
  collect their orders from the tray", "a technician calibrates it daily", "a nurse checks on it"), and someone else
  given a robot works with it ("give the operator a system that sorts parts", "get my grandma a robot arm"; only the
  user asking for one, "give me a robot that sorts screws", is no person). Any of them puts a person next to it; "Nobody nearby", "no one works next to it"
  (`brief.NOBODY_NEAR`; not "no one else", nor followed by except, but, apart from, other than, besides, just or only:
  "no one near it but me", "no kids near it, just me") says "no" only when the request has none of them and gives
  nothing to an animal ("no one is around, it helps my grandma eat her soup", "nobody nearby, the nurse takes the tray
  from its gripper" and "no people around, students will use it" keep the person). A person word right after "no",
  "not" or "without" is no person ("no humans next to it", "no workers near it"). `merge()` keeps the signals in
  `flags["person"]`: a later message's "nobody near it" doesn't take that person away, and `brief_to_changes` keeps the
  emergency stop beside them whatever the people answer is (an answer "It works on its own" gets the assumption "Your
  request puts a person next to it (...), so it has an emergency stop even though no one else works next to it." and
  the card row "Only the person your request names"). Kids, children, toddlers, babies,
  infants, twins, teens, tweens, youngsters, grandkids, schoolkids, "my son", "my daughter", "the girl" and a child's
  age ("my 5yo", "my 3 year old": 1 to 12, not before an animal, a robot, a car, a house or a tree) (not "no kids")
  say "children". People
  beside it in so many words (`brief.PEOPLE_BESIDE`: "people work next to it", "next to the operator", "between two
  packers", "a worker stands next to it", "I'm right next to it", "we'll be working beside it", "customers stand in
  front of it", "where customers pick up their coffee", "the table I work at", "shared with two workers", "a bin I fill
  by hand", "the operator takes the part from its gripper", "an operator feeds it parts", "an operator loads the
  fixture", "students will use it", "no one near it but me") say "yes". A hand-over (`HAND_TO_PERSON`), a person the
  robot works for, helps or gives something to (`brief.PERSON_NEAR`: a grandparent, "my mum", "my colleague", "help
  me", "assists the surgeon", "help a blind person", "for him", "for the nurse", "give the operator a break" and the
  like), a request that leaves it unsure whether someone is there ("pours me a coffee", "makes me a sandwich", "pass
  the salt over here", "bring that here", "pass the butter" as the whole request, "a busy cafe", "serves drinks", "a
  robot barista") or "no one else" infer "yes" for every archetype (a toy archetype keeps its children; a pet
  archetype's person is a person: "hand the dog walker the dog food"), so the robot gets an emergency stop and the
  card says it was assumed. An animal it gives something to (`brief.ANIMAL_GETS`: "hand the dog a bone", "give
  my parrot seeds", "toss balls to my dog", "bring a toy for the puppy"; not "bring the hamster cage to the table")
  infers "pets", with the same emergency stop; an animal is never the person of a hand-over ("to my dog" is no "to my
  cousin"), while "my dog walker" and "my dog's sitter" are people. Only then does "nobody nearby" say "no", and only
  then do "on its own", "by itself", "unattended", "enclosed", "fenced" and the
  robot working alone (`brief.ROBOT_ALONE`; "she lives alone" and "while he is alone" say nothing about the robot)
  infer "no", shown as an assumption ("assumed"), never as "you said". A designer model's "no" never takes away a
  person the words put next to it (`agents.apply_update`). `HAND_TO_PERSON` ("pass my dad", "bring her") and the word hand-over ("does
  hand-overs to nurses") set the task to hand-over. A hand-over has people next to it (its default is "yes" on an archetype whose default is "no" or "pets") and
  always an emergency stop (`brief_to_changes`): the people
  question isn't asked on one, and an answer or a model that says it works on its own keeps the e-stop, with the
  assumption "It hands things to a person, who takes them from the gripper within its reach, so it has an emergency
  stop even though no one else works next to it." and the card row "Only the person it hands things to". A sorting
  archetype asked to hand things over gets no camera. Every
  width in a note is `capability.min_width` for the shape (26 mm round, 21.5 mm flat-sided), and a preset outside the
  band says which limit it breaks (`brief.unfit_note`: "too heavy", "too narrow", "too wide").
- **Payload and reach say where they were measured (ENG-3).** The payload sweep's `max_payload_g` is measured top-down
  at `test_reach_mm` (about 45 % of the egg-picker's reach), so the inspector's "Payload at full reach" overstated the
  arm 2.4 to 3x. The sweep now also reports the arm stretched straight out (`simulate._stretch_pose`: the tool point at
  shoulder height, as far along the pick direction as it reaches, position only): `stretch_reach_mm`,
  `stretch_payload_g` (`_static_capacity` there: what holds with a servo at stall, statically) and
  `stretch_limited_by`; nothing more is simulated and every other output is bit-identical. The summary adds
  `payload_test_reach_mm`, `stretch_payload_g`, `stretch_reach_mm` and `top_down_reach_mm` (the top-down band's outer
  edge; `workspace_reach_mm` stays the any-pose maximum). The inspector labels them "Max payload, top-down at
  <r> mm", "Payload stretched out at <r> mm", "Top-down reach" and "Reach, any pose", with a note that payloads are at
  stall, and Quick facts' "Payload" (the object's mass) is "Object mass"; New robot's done card says "Max payload" with
  where it was measured. The egg-picker: 1,729 g top-down at 216 mm, 720 g stretched out at 476 mm (elbow-limited); the
  SO-101: 732 g at 252 mm, 216 g at 465 mm (shoulder).
- **Stage reuse follows every key a stage reads (ENG-4).** `GRAPH` adds `electronics.pinion_radius_mm` to sizing (the
  hand's rack-and-pinion drive), `task.*` to the manual (it prints the target cycle time) and `build.config` to the
  gripper stage, and drops `electronics.supply_v` from it. The gripper stage of a generated arm analyses
  `pipeline2.gripper_design(design)`: the gripper actuator (its stall on the servo rail since section 21) and the
  fingers the robot's CAD makes for its build config (`arm.LOOK`: printed PETG, machined Al6061, molded PC, the last in
  `materials.MOLDED`), with no controller; it no longer reads `finger.material`, `fab.process` or `electronics.mcu`,
  which the robot doesn't build with, and its force plan has no `mcu`, `wiring`, `parts` or `sensing` (the electronics
  plan's, for a robot). So a controller change no longer reruns the gripper stage (the electronics, the CAD whose base bay holds the board, and
  what follows them still rerun), a fabrication-process change reruns manufacturing, the BOM and the manual only (no
  Mechanical review in Teams), and a cycle-time change reruns the manual and asks Documentation. New robot's preflight
  analyses the same design.
  `tests/test_robot_stage_reads.py` computes every stage on a design that records each key it reads and fails on any
  key outside the stage's patterns and its upstream stages' (for the egg-picker and the SO-101); the simulation's only
  allowed exception is the project name, robot.xml's model name.
- **Print orientation fits the bed (ENG-10).** `Geo.pose_for(process)` picks the lowest-scoring print pose among those
  whose height fits the process's bed (`manufacturing.BEDS`: FDM 250 x 210 x 220 mm, SLA, MJF) and whose footprint fits
  either way round, and turns the part 90 degrees about z when its footprint only fits that way; the STL, 3MF, print
  height and the FDM cost model use that pose. When no pose fits, the DFM check says "<x>x<y>x<z> mm (its best way up)
  exceeds the ... build volume: split the part". The egg-picker's base shell prints "+y down", 244 x 38 x 144 mm (it
  stood 244 mm tall).
- **A study's model check compares what moves (ENG-8).** `robust.prepare` compares the rebuilt robot.xml with the
  revision's after `_norm_xml`: the option time step and the inertial, geom and site elements of every world body with
  no joint (the base welded to the table), with the mesh assets only they used, are left out. A controller or
  `electronics.config` revision reuses its parent's simulation (`GRAPH`); its rebuilt base differs in mass and shapes
  only, so `matches_revision` stays true. Any change to a moving body (a servo's torque, a link) still makes it false.
- **What sizing changed is shown (ENG-11).** `summarize` adds `notes`: sizing's notes ("reach raised from 320 to 475
  mm: top-down pick/place at 234 / 233 mm with a 175 mm wrist + hand", "shoulder height raised to 76 mm (...)"). The
  inspector lists each under Issues & warnings as "Morph adjusted <note>" (an older revision's `sizing.notes` when its
  summary has none) without counting it as an issue; the Parameters rows for Reach and Shoulder height say "built at
  N mm" when the sized value differs from the asked one by 1 mm or more; New robot's done card and `morph robot`'s
  summary ("adjusted: ...") list them.
- **Software checks, not validation (ENG-12).** New robot's done view is titled "Your robot is designed"; a robot
  whose summary is feasible (every simulated scenario included, section 20) gets the tag "Software checks pass" (never
  "Feasible") and "Its software checks pass:
  simulation, torque margins, DRC and DFM. Not bench-validated: build and test it before you rely on it."; the pipeline
  agent line says "every software check passed". The Firmware pane opens with "Generated firmware, not bench-tested: it
  hasn't been compiled for this board or run on the arm ..." (`electronics.firmware_validation` is unverified); the
  Export button's title and toast say "not bench-validated", as the export manifest's `note` does.
- **Manual, make files and BOM list the same parts (ENG-13).** `manufacturing.fabricated(part)` (a fabrication
  process, and not a fastener, cable or "visual" part) decides both what gets make files and what the manual's parts
  tables list as made: the upper arm's logo inlay is decoration in the CAD only (no print file, BOM line or manual row).
  Every bought fastener and standoff in the BOM is in a step (the M3x6 brass standoffs in the Electronics bay step's
  parts table). The first pick-and-place test names the project's object ("a stand-in for the strawberry (or a 30 mm
  ball) ... then repeat with a real strawberry"); only an egg project uses a hard-boiled egg first.
- **Mold tooling is shown apart from the unit cost (ENG-14).** The BOM tab's per-config panel is "Per unit at qty N":
  each config's parts + purchased cost per unit at the build quantity (`manufacturing.configs[k].parts_cost +
  purchased_cost`), with "+ $X tooling, once" under a config that needs molds; the unit-cost stat says "incl. $X tooling
  over N units" when the current config has tooling. The Manufacturing tab's config cards show the same per-unit cost,
  "+ tooling $X once" and "$Y each at qty N with tooling" (`unit_cost`, which adds tooling / quantity). The inspector's
  Unit cost row reads "Unit cost (incl. $X tooling over N units)" when `summary.tooling_cost` is set. The data are
  unchanged: `bom.totals.per_config` and `configs[k].unit_cost` still include tooling / quantity. The egg-picker in
  production at quantity 1: $4,771 per unit + $72,971 tooling ($77,742 each with tooling).

## 20. Grip physics and honest checks (release review fixes, track E3)

Round 2 of the grip fixes: what round 1's single grip source got wrong, found by re-checking it. What other code
relies on since:

- **One jaw model (E-N7).** `morph.electronics.jaw_force(torque_nm, lever_mm, jaw)` is the per-finger squeeze of each
  gripper type: a rack-and-pinion jaw's one pinion drives both racks, so each finger gets torque x `GEAR_EFF` (0.6) /
  (2 x pitch radius); a stock revolute jaw on the servo's horn, torque / lever. `jaw_torque(force_n, lever_mm, jaw,
  losses=True)` inverts it (`losses=False`: the torque that can never squeeze harder, the force-sensing gripper's cap).
  Everything uses them: `servo_max_force` (the force plan's `servo_stall_force_n`, its `limit_n` for an object without
  a damage force, so the firmware's `GRIP_LIMIT_N` and the F command's cap, and its "reaches only" issue), the
  standalone gripper's `choose_servo` (the first hobby servo its printed mechanism mounts whose stall force is at
  least twice the target), `sizing.gripper.stall_force_n` (at the rail), the generated arm's gripper joint `effort` (the
  stall force per finger at the rail: the URDF, and the simulation's cap on the squeeze; the grip-limited payload uses
  the firmware's hard stop, section 22),
  `electronics2.grip_torque_limit`, the gripper's current estimate, `eplan.grip.stall_force_n` (new: per finger at
  stall on the rail) and the stock jaw's `servo_stall_force_n`. The STS3215 12 V gripper on a 10.5 mm pinion gives
  82.9 N per finger (the force plan said 165.9 N, sizing 221 N).
- **The hold and the target (E-N1).** The force plan's `hold_force_n` is never below `static_hold_n`, the static
  friction hold m g / (2 mu gain) (`morph.electronics.static_hold(o, gain)`, `gain` the pad's wedge gain, 1 for flat
  stock jaws); the grasp simulation's bisection (`sim_hold_n`) only ever raises it. Its contour pads cradle a can at
  2.34 N per finger that the robot's own fingers drop below 4.65 N. `force_window` gives an object without a damage
  force twice its lower bound as the target (it gave 1.3x): three times the static hold, a 1.5x margin over the robot's
  shake test, which loads the grip with about 1.9 g. An object with a damage force keeps min(sqrt(lo x hi), 2 x lo)
  when a force sensor regulates the squeeze (`sensor` given); without one the squeeze is open loop (a torque limit, so
  it varies with where the object sits, the gearbox's friction and the supply) and the target is the window's
  geometric mean sqrt(lo x hi), as far from both ends as the window allows: the SO-101's egg 4.58 N (2.52 N before; its
  shake test crept 5.01 mm with the squeeze exactly 2.52 N), its strawberry 0.7 N (0.5 N, 2 % of the servo's torque).
  The simulation's closed form (`simulate._grip_force_target` without a shipped grip) and New robot's Keewano
  preflight use `static_hold` too. The 370 g can: 4.56 N per finger before (its shake test dropped it), 13.94 N now.
- **What G does (E-N2, E-N3).** `grip_motion()["probe"]` is the whole overtravel (`GRIP_CLOSED - GRIP_MIN`): a
  force-sensing close looks for a reading as far as the max-squeeze stop, so an egg 8 % narrower than the calibration
  gauge (1.76 mm per jaw) gets gripped; the torque limit, written at start-up as a cap at the force limit, bounds the
  squeeze meanwhile; with no reading by `GRIP_MIN` it goes back to `GRIP_CLOSED` and says so. Without force sensors G
  commands `GRIP_MIN`, past the object (it commanded `GRIP_CLOSED`, the gauge contact, where a torque limit has no
  position error to act on): the torque limit sets the squeeze. The simulation models exactly that: its jaw is
  commanded past contact, force-limited at the torque limit's force. The firmware header, the plan's warning ("G closes
  the jaws past the object and the gripper servo's torque limit sets the squeeze") and the manual
  (`manual._grip_sentence`, with `GRIP_MIN` in the joint's unit) say the same. A hobby PWM servo has no torque limit:
  section 22 says how far its firmware closes it instead. `tests/fw_stubs/grip_harness.cpp` takes the object's contact
  offset as `argv[2]`.
- **The stock jaw's lever (E-N6).** `imported.grasp_lever(design, sizing)` is the lever where the simulation grasps the
  object (`simulate.grasp_lever(asm, design)`: `_describe(..., grasp_only=True)`, the hand's and the jaws' pieces
  only), worked out on the robot's own jaws with sizing's servos and the object's shape and size, on the camera-free
  URDF, memoized (about 2 s). `imported.gripper_summary` puts it in `lever_mm`, so the torque limit is set for the
  target there and the simulated squeeze is the target: the SO-101 grasps the egg 62.2 mm from its pivot (it squeezed
  2.83 N, 3.41 N peak, for a 2.52 N target with the limit set at 70 mm) and a strawberry 70.0 mm.
- **Every simulated scenario counts (ENG-12).** `pipeline2.sim_issue(name, scenario)` is the summary's issue for a
  scenario that failed, saying why ("shake test failed in simulation (object dropped (213 mm slip))"; `SIM_CHECKS`
  names the four); `summarize` adds one for every failed scenario, not only the pick and place, and lists their names
  in `summary.sim_failed`. So `feasible`, New robot's "Software checks pass" and "every software check passed", the
  inspector's tag (which no longer lists a scenario the summary already counts), Teams' tag and "Built r002: software
  checks pass" (it said "feasible"), Import's "Imported X: software checks pass" / "its software checks found issues"
  (it said "isn't feasible yet") and `morph import`'s line all follow every check. `tests/test_grip_sweep.py`
  (MORPH_SLOW) builds the two demo robots and 22 one-liners: every scenario passes or the build names it, and no
  scenario that passed on 6c49545 fails.

## 21. Stage dependencies, wording and supplies (release review fixes, track E4)

Round 2 of the pipeline and wording fixes. What other code relies on since:

- **A stage reruns when an upstream result it reads changes (E-N4).** `GRAPH["gripper"]` has `sizing` upstream: the
  imported robot's gripper stage reads sizing's servo rail and servos (`imported.gripper_summary`), and a generated
  arm's force plan works the gripper servo's stall torque out on sizing's rail (`pipeline2.gripper_stall_nm(ga,
  sizing)`: `catalog.torque_at`, the catalog every other stage uses; `morph.electronics.plan(..., stall_nm=)`), so
  `servo_stall_force_n`, `limit_n` (the firmware's `GRIP_LIMIT_N` for an object without a damage force) and the
  "reaches only" issue are at the rail. An `electronics.servo_v` edit on the SO-101 used to keep the old grip limit
  (GRIP_LIMIT_N 20.83 N against 19.29 N fresh). `pipeline2.gripper_inputs(design, sizing)` is the only part of sizing
  the stage reads (the rail's voltage; for an imported robot also the servos), and `pipeline2.PARTIAL` declares it:
  `RobotProject.run` reuses a stage that is dirty only because an upstream reran, when that part is unchanged, no key
  of its own changed and the parent's own result for it is usable (an early cutoff; a failed one is computed again), so a wrist camera or a longer reach re-sizes the arm without rerunning the
  grasp simulation (`provenance` "reused from rNNN"). `dirty_nodes` and Teams' `affected_stages` stay static: they
  list the gripper stage for every sizing edit. Teams' change-request preview applies the same cutoff to its grip: it
  re-plans the grip, and shows a "Grip" row, only when a gripper key or `gripper_inputs` changes (section 22: and then
  keeps the base's grip when the grasp is the same). New robot's preflight passes the same rail stall.
- **The arm is laid out the same before and after the electronics plan.** `arm.BAY_CLEAR_MM` (22 mm) is the electronics
  bay's clear height under the deck, fixed: the layout no longer takes it from the plan's boards (sizing laid the arm out
  without them, the build with them, and with a short turret the deck sets the shoulder's height). A board taller than
  that is a CAD issue. So the simulation, which grips with `eplan.grip` and welds the plan's boards into the base, still
  doesn't depend on a controller or electronics-config edit, and reuses its result across them (section 19, ENG-8).
- **The guard.** `tests/test_robot_stage_reads.py` also computes every stage on recording results that log each
  upstream result (and key in it) a stage reads, and fails when it reads a stage that isn't upstream of it; the
  simulation's reads of the electronics plan are the one declared exception (`FOREIGN`), checked by swapping every
  design key the plan reads that the simulation doesn't rerun for (`OTHER`: controller, electronics config) and
  comparing the simulated robot (`robust._norm_xml` of `simulate.build_mjcf`, and `asm.meta`'s grip). A stage in
  `PARTIAL` must compute the same from that part alone.
- **Wording that names a thing is not a job (E-N8).** `brief.ADJUSTED` reads verbs, not things: "open X" whose X is the
  subject of a verb ("open boxes arrive on the left", "open jars are on the shelf") opens nothing; "twist" is a verb
  only when not after a moving verb or determiner (`_DET`) and not "twist ties" or "twist-off"; "screw caps" after
  `_DET` are caps; "insert" after `_DET` and plural "inserts" or "plugs" not done by a doer ("it", "that", "which",
  "robot", "arm") are parts; "assembly line/station/area/..." is a place and "pre-assembled" an adjective.
  `brief.HAND_TO_PERSON` needs a receiver (see "Who receives a hand-over" below): "them" or "it" alone are the things,
  so "pass them to the packing station" is a pick and place with no emergency stop. `PERSON_NEAR` uses it (help, assist
  and serve keep "them"). `tests/test_brief_answers.py` holds both ways' tables.
- **No negative payload (E-N9).** The payload sweep's `stretch_payload_g` is the static capacity stretched straight
  out, and stays as computed in the simulation's metrics (below zero: the deficit). The summary clamps it at 0 and adds
  `stretch_holds_itself` (False when a servo can't hold even the arm's own weight out there; None without a sweep) and
  `stretch_limited_by` (the joint). The inspector shows "none" for it with a note ("Stretched out at 465 mm the arm
  can't hold even its own weight: its shoulder lift servo stalls. It carries payloads only closer in.") and `morph
  robot`'s summary prints `stretch_payload_g=none` and the same sentence; both also read a revision saved with the
  negative number. The SO-101 on Dynamixel servos at a 7.4 V rail is such an arm (-13 g, shoulder_lift).
- **A battery request says what is built (E-N10).** Morph plans mains power only (no battery in the supply catalog).
  `intent.BATTERY` (battery, LiPo, Li-ion, 18650, cordless, "2S pack") adds `intent.battery_note(design, volts)` to the
  chat's reply, from the rules or after Claude's: the mains supply the build picks for that servo rail ("a 12 V 10 A
  mains supply and the servo buck set to 7.4 V", or "... straight to its servos") and "no battery, charger or
  low-voltage cutoff"; for a lithium pack's voltage (3.7 V a cell, `intent.CELLS` reads "2S") it names every servo whose
  maximum is below the pack's full charge (4.2 V a cell: the SO-101's 7.4 V STS3215s and a charged 2S pack's 8.4 V).
  The voltage is still the servo rail (`electronics.servo_v`) when every servo takes it; the chat tool's description
  tells Claude the same.
- **Teams prices mold tooling apart (ENG-14).** The change-request preview's cost (`teams/impact.py`) is per unit
  without mold tooling, as the BOM tab's "Per unit at qty N": the base's unit cost and the per-level costs
  (`bom.totals.per_config`) spread tooling over the build quantity, so `_base` also reads each level's
  `manufacturing.configs[k].tooling_cost` and takes it out (tooling / the base's quantity). The tooling is a note, paid
  once ("plus $72,971 of mold tooling, paid once", "and no mold tooling (it had $X, paid once)", or "plus the $X of
  mold tooling" for a build that keeps its molds); `impact.cost` has `tooling: [before, after]` and `before`, `delta`,
  `after` without it. Switching the default egg-picker to production at quantity 1 read "about +$77,438 per robot"; it
  reads "about +$4,466 per robot (parts made +$4,466): $305 → about $4,771; plus $72,971 of mold tooling, paid once". `collab.built_summary` adds `tooling_cost` and `quantity` ([base,
  built]) and `release_gate`'s checklist `tooling_cost` and `quantity`; Teams' "Built rNNN: ... per unit" line and the
  checklist's unit cost say "incl. $X tooling over N units", as the inspector does.
- **No "add force sensors" issue where a torque limit sets the squeeze.** `electronics2.has_torque_limit(gkey, family)`
  (the firmware can set the gripper servo's torque limit on the arm's bus) is the one rule `grip_torque_limit` and the
  gripper stage use: `pipeline2.gripper_drive(design, ga, sizing)` passes it with the rail stall to
  `morph.electronics.plan(..., torque_limited=)`, which then raises no "can reach X N > object limit ... add
  finger.sensor=fsr402" issue (the standalone gripper's rule for a servo with nothing to limit it). A generated arm set
  to Force sensor: none was not feasible for that alone; the egg-picker now builds with its checks passing (torque
  limit 5.3 % of stall for 4.42 N, the egg intact at 4.53 N peak). A hobby PWM gripper still gets the issue, as an
  imported one does. New robot's preflight uses the same helper.

## 22. Hobby grippers, preflight speed and honest records (release review fixes, track E5)

Round 3 of the grip and pipeline fixes. What other code relies on since:

- **No hobby servo is commanded to stall.** `morph.electronics.SERVO_BAND_MM` (1.5 mm) is how far past contact a
  position-controlled jaw servo closes before it squeezes with its whole stall force (its position loop's proportional
  band plus the gear's and the jaw's give); `simulate.GRIP_SAT_MM` is the same number. A gripper servo with no torque
  limit (`electronics2.has_torque_limit` false: hobby PWM) gets `eplan.grip.open_loop` from
  `electronics2.open_loop_squeeze(grip, gkey, family)`:
  - without force sensors `squeeze_mm` = band x min(target, `rated_n`) / stall, where `rated_n` is the servo's rated
    (continuous) torque as a force at the jaw (stall x `rated_nm` / `torque_nm`: 10 N on an MG996R egg-picker hand), at
    least two position commands (`step_mm` each: a PCA9685 count, 4.88 us, at the jaw), and `squeeze_n` what that
    squeezes a gauge-sized object with: the target, more when floored, `rated_n` when the target is above it
    (`capped`; a 370 g can's 13.94 N target squeezes 10 N, 0.49 mm past contact; a 900-1400 g can, whose target is
    above the servo's stall, no longer has it held at stall 1.5 mm past contact, and is an issue: 10 N is below the
    16.95-26.37 N it needs not to slip). The jaws are calibrated on a gauge of the object's nominal size and the object
    strays from it by `OBJECT_SIZE_SPREAD` (below) each way, so one at the large end of its size is held `spread_mm`
    (that share of its half-width for a rack's two jaws; of its whole width for a stock jaw, whose one moving jaw
    closes on the object against the fixed jaw: `open_loop.per` "at the moving jaw", and the manual then calibrates
    the stock jaw's zero on a gauge held against the fixed jaw instead of with its faces touching, and the firmware's
    `GRIP_CLOSED` comment says "jaw on the gauge") further past its own contact. `room_mm` = min(`PROBE_STALL_SHARE` x band, band x
    the object's damage force / stall) is how far past its contact that object may be held; `covers` is whether two
    position commands fit in `room_mm - spread_mm`, and then `squeeze_mm` is at most that (`band_capped` when this
    lowers it; `band_n`, stall x (room - spread) / band, is what that squeezes a gauge-sized object with). `large_n`
    is what the large end gets. Without `covers` the squeeze stays as above and the plan has an issue
    (`grip_problems`: "... closes by position only, so a <object> at the large end of its size, <spread> mm per jaw
    wider than the calibration gauge ... is held <squeeze + spread> mm past its contact, with <its whole stall force
    | N, x % of it> ..."); with `band_capped` a squeeze below the window's low end is an issue too. On an MG996R
    egg-picker hand (1.2 mm room): a 370 g soda can (2.64 mm spread) and a water bottle (2.60 mm) get the issue, an
    egg too (1.76 mm; it also has the gripper stage's "add finger.sensor=fsr402"), an 18 mm marker (0.72 mm) covers
    (0.16 mm squeeze, 18.13 N at its large end, above the 10 N rated torque: a warning) but is too small for the
    fingers (a `too_small` issue): without force sensors no object both covers and fits them (below). The firmware's
    `GRIP_SQUEEZE` (in the joint's unit) is that distance and G commands `GRIP_CLOSED - GRIP_SQUEEZE`, never
    `GRIP_MIN`; F sets it (`GRIP_SQUEEZE_PER_N` = band / stall, at least `GRIP_SQUEEZE_LEAST`).
    `eplan.grip.limit_n` (`GRIP_LIMIT_N`, F's cap) is min(the object's limit, `rated_n`, and `band_n` when `covers`): F
    raises a squeeze planned below that up to it (a servo softer than the model squeezes less than F says, and F can't
    take it further), never holds a gauge-sized object past the servo's rated torque unless two position commands
    already squeeze more (below), and with `covers` never holds the large end of the band past `room_mm`. A capped grip writes the squeeze as `GRIP_TARGET_N` and is a plan issue only
    when `rated_n` is below the window's low end, the force the object needs not to slip (`grip_problems`). The
    simulation (`simulate._open_loop_stalls`, `_Info.grip_squeeze_mm`) fails a pick-and-place trial whose object
    (`size_scale`) is wider than the gauge by `GRIP_SAT_MM - grip_squeeze_mm` per jaw (at a stock jaw's moving jaw,
    twice its radius' growth) or more, where the servo's own
    position loop holds it at stall: `metrics["grip_stalled"]` {wider_mm, squeeze_mm} and `metrics["failure"]`; a
    robustness study counts it as `stalled` ("Held the gripper servo at stall ..."), unless the object was also
    crushed (`over_force`). It also crushes a trial (`simulate._open_loop_crushes`: `intact` false,
    `metrics["grip_crushed"]` {force_n, wider_mm, squeeze_mm}, a named failure, `over_force` in a study) whose servo
    model squeeze, `grip_stall_n` x min(`grip_squeeze_mm` + wider, `GRIP_SAT_MM`) / `GRIP_SAT_MM`, is above the
    object's damage force, at its nominal size too: MuJoCo's soft contact measures far less on a light or rigid
    object (a light bulb 2 % over its size: 10.35 N against the model's 15.66 N; a ping pong ball's least close, 3.31 N
    against 0.28 N), and both are labels from that model, not from the measured grip (`docs/ROBUSTNESS.md`). The plan keeps a margin the simulation doesn't (80 % of the stall against the stall
    itself), as its hold margin does. A floored squeeze (two position commands) above `limit_n` is a plan issue of
    the `hobby_gripper` kind that names the lowest of the three ("... the least it can close past the <object> (two
    position commands, <mm> at the jaw) squeezes about <N> N per finger, above <the object's limit | the <N> N it
    holds continuously (its rated torque) | the band's>"): the SO-101's stock jaw on hobby servos has one PCA9685
    count 0.48 mm at its 62 mm egg lever, so an egg gets 11.04 N, twice the 5.62 N the MG996R holds continuously
    there, and a soda can 11.05 N against 4.38 N. The firmware's `GRIP_TARGET_N` comment then says "two position
    commands, the least G can close past it, more than Morph's <target> N target", and the manual says F can't lower
    it. Torque-limited and force-sensing grips
    keep `GRIP_SQUEEZE` = the whole overtravel (G still commands `GRIP_MIN` without sensors);
  - with force sensors (`open_loop_squeeze(grip, gkey, family, width_mm)`) the firmware's `GRIP_PROBE` is `probe_mm`:
    the close goes no further than that past the gauge without a force reading, reads there too, and then opens to
    `GRIP_MAX` with "!GRIP no force reading down to GRIP_PROBE past GRIP_CLOSED: opened to GRIP_MAX ..." (it went
    back to `GRIP_CLOSED`, where an object larger than the gauge stayed squeezed with no reading; a torque-limited
    gripper still does that, its torque limit caps it). `spread_mm` = `morph.electronics.OBJECT_SIZE_SPREAD` (0.08, the
    robustness study's default size band, which `robust.default_spec` reads) x the object's half-width;
    `reach_mm` = spread + two position commands (a reading). When reach is at most `PROBE_STALL_SHARE` (0.8) x band,
    and with a dead sensor a gauge-sized object at the probe below gets no more than its damage force (stall x probe /
    band) and one at the large end of its size, held `spread_mm` further past its own contact there, no more than its
    damage force either (stall x min(probe + spread, band) / band), `covers` and `probe_mm` = min(max(reach, band x
    `limit_n` / stall), `PROBE_STALL_SHARE` x band) (an SG90 hand's 18 mm marker: band x `limit_n` / stall is 1.50 mm,
    the probe 1.20 mm); otherwise `covers` is false, `probe_mm` = min(max(band x `limit_n` / stall, two commands),
    `PROBE_STALL_SHARE` x band), an object narrower than it per jaw isn't gripped, and the plan has an issue naming
    both distances, what a dead sensor would put on a gauge-sized object at the reach (and its damage force when that
    is above it, or else what the large end of its size would get when that is above it) and recommending a smart-bus
    or Dynamixel gripper servo (for a hobby PWM gripper servo; a bus servo on a bus that can't set its torque limit has
    its own issue and gets no firmware). On an MG996R egg-picker hand an 18 mm marker covers (0.92 mm probe, 0.72 mm
    spread); an egg (1.92 mm needed), a strawberry (1.36 mm), a ping pong ball (1.76 mm) and a tomato (2.76 mm)
    don't (0.92, 0.16, 0.16 and 0.32 mm probes), nor does a 20 mm grape: its 0.96 mm reach is under the 1.2 mm share,
    but a dead sensor there would put 19.77 N on a gauge-sized grape, ten times its 2 N damage force, so its probe is
    two commands (0.16 mm), where a dead sensor still puts 3.31 N on it, the least the servo can be sent (the plan's
    warning says so). Nor does a 22 mm flat-sided part with a 25 N damage force: a dead sensor at its 1.04 mm reach
    leaves a gauge-sized one 21.42 N, but one at the large end of its size (0.88 mm per jaw wider) gets the whole
    30.86 N stall (it read covered, and the compiled firmware put 25.94 N on one 2 % over its size); its probe is
    0.92 mm. Without a damage force the same part covers (1.04 mm probe), and its large end can reach the stall for the
    moment before the jaw opens: the plan's warning, the firmware's comment and the manual say so. So on this hand the
    only hobby grips without an issue are force-sensing ones on flat-sided parts from 21.5 mm (`capability.MIN_WIDTH_MM`:
    the force-sensing fingers close on 21.5 mm flat, 26 mm round; the plan itself raises no issue from 21.01 mm) up to
    25.97 mm wide (from 25.98 mm, reaching the small end of its size would let a dead sensor put 80 % of the stall on
    a gauge-sized one, the probe's cap), with no damage force or one at or above the MG996R's 30.86 N stall (below it
    a dead sensor can put the stall on one at the large end of its size), measured on parts of 5 to 132 g (heavier
    ones overload the hobby shoulder); the marker covers but is too small for the fingers (a `too_small` issue), and without force sensors no object both covers and fits the
    fingers (31.5 mm flat, 38 mm round). `probe_n` (stall x probe / band) and `large_n` (stall x min(probe + spread,
    band) / band) are what a gauge-sized object and one at the large end of its size get at the probe with a dead
    sensor until the jaw opens (`reach_large_n` the latter at `reach_mm`); the plan's warning, the firmware's comment
    and the manual state both, and the warning adds the damage force when either is above it.
    `arm.grip_meta` puts `probe_mm` in `asm.meta["grip_probe_mm"]`
    (`simulate._Info.grip_probe_mm`), and a pick-and-place trial (`simulate._run_pick_place`) whose object
    (`size_scale`) is narrower than the gauge by more than that per jaw closes only to the probe and opens
    (`_given_up`), fails, and names it: `metrics["grip_gave_up"]` {narrower_mm, probe_mm} and `metrics["failure"]`; a
    robustness study counts it as `no_force_reading` ("Let go with !GRIP ..."). The firmware's first reading needs
    `GRIP_SENSE_N` (0.05 N) of squeeze, a few hundredths of a millimetre, so the two agree to within that. New
    robot's repair (`brief.repair`, issue kind `hobby_gripper`, which also takes the capped-hold and size-band issues
    above) switches
    a hobby PWM arm with such an issue to smart-bus servos and says so, also when the person asked for hobby servos
    ("a 4 dof arm with hobby servos to pick up ping pong balls"), as it does when they can't hold the load.
    `tests/fw_stubs/grip_harness.cpp` prints `max` (`GRIP_MAX`) too.
  `eplan.grip.squeeze_n` (every grip: the target, or the open-loop squeeze) is what `_actuator_currents` budgets the
  gripper's current from. A hold above the servo's rated (continuous) torque share is a plan warning. `arm.grip_meta`
  puts `grip_squeeze_mm`, `grip_squeeze_n` and `grip_stall_n` in `asm.meta`; `simulate._describe` then commands the jaw
  `grip_squeeze_mm` past contact with the stall force as its limit (`info.grip_open_loop`), and a robustness study's
  `grip_scale` scales that servo's gain too. `tests/fw_stubs/grip_harness.cpp` also runs a PCA9685 sketch (`pulse`, the
  gripper channel's last pulse) and takes an F before G as `argv[3]`.
- **New robot's preflight and Teams' preview never run the stock jaw's seat search.** `imported.grasp_lever(design,
  sizing, quick=True)` and `imported.gripper_summary(..., quick=True)` (what `brief.preflight`, and through it Teams'
  `impact`, call) take the lever the build worked out for this object (`_LEVERS`, keyed by `_lever_key`), or for one of
  its shape within `NEAR_MM` (2 mm) of its diameter and length, else the profile's `jaw_lever_mm` (`JAW_LEVER_MM`, 70 mm
  mid-jaw), and mark the force plan `lever_estimated`. The build's gripper stage (`pipeline2.gripper_stage`) always
  searches. The search puts the SO-101's objects 20-66 mm across 62.2-79.8 mm from the pivot, so the estimate's stall
  force and stall-capped limit are within 15 % of the build's. A target the object's window sets and a damage-capped
  limit are the build's; a target capped at 0.8 x the stall (`imported.gripper_summary`) moves with the stall and so
  can differ from the build's within the same 15 % (the SO-101's water bottle: 18.51 N quick, 16.23 N built). Measured on the SO-101 (load 5-15, wall): preflight of a soda can 28.3 s -> 4.1 s (CPU 24.9 ->
  2.7 s, the rest is sizing for its new mass), each new egg diameter 2.8-8.9 s -> 0.0 s; Teams' preview of a soda-can
  change 39.5 s -> 11.5 s, of a 42 mm egg 15.9 s -> 3.2 s.
- **Teams' preview keeps the built grip when the grasp is the same.** A generated arm's gripper stage gets its hold
  from its grasp simulation, which New robot's closed form (the preflight) doesn't have: the egg-picker builds 2.34 N
  where the closed form says 2.17 N. `impact._regrip` plans the new grip with the base's `hold_force_n` and
  `target_force_n` on the new servo and rail (`morph.electronics.plan` with the target pinned and the hold as the
  simulated one), so only the stall, the force limit and "reaches only" move, whenever `impact._grasp_inputs` (the
  gripper_design's object.*, finger.*, gripper.*, safety_factor and the Grip force target; the finger material only
  while Keewano calibration is on) is the same for base and edit: a servo rail, an actuator family or a build level.
  The preview then shows no "Grip" row and plans the electronics with the base's target, as the build does. Any other
  gripper edit on a generated arm (the object, the fingers, the pads, a force sensor) gets the plan its gripper stage
  will make: `impact._stage_grip` runs the grasp simulation's hold search (`morph.sim.min_hold_force`, the one number
  of the simulation the plan reads; about 3 s, kept per gripper design in `impact._HOLDS`) and plans it on the new
  servo and rail, so a 6 mm pad depth on the egg-picker keeps 2.34 N as its build does (it read 2.34 -> 2.17 N). A
  stock jaw takes the preflight's grip, as its gripper stage is that closed form.
- **Who receives a hand-over (E-N8); unsure is a hand-over.** `brief.HAND_TO_PERSON` reads a receiver after a giving
  verb, in `brief.people_text(request)`: the request in lower case with a capitalised name, or a role or title and a
  name ("Nurse Jones", "Doctor Patel", "Dr Smith"), written "someone"; a few misspellings mended ("teh", "brng",
  "brign", "pas", "nures", "docter"); and a word that names someone by what they do and isn't on the lists below
  written "morphagent" (`brief._agents`: an -er, -or, -ist, -ian, -man/-men, -person/-people, -folk, -mate or -ee word,
  "the riveter", "the anaesthetist", "the librarian", "the postman", "the salesperson", "the dog trainer", inside a
  noun phrase, after a determiner, a possessive or a preposition through up to three describing words, or a plural at
  the start), unless it is a machine, a container, a tool, a place, a part of the robot, an animal, a verb, a name or
  another word that ends the same way (`_NOT_AGENT`, `_AGENT_VERBS`, `_machine_reader`: "the capper", "the dryer", "the
  coffee maker", "the sequencer", "the card reader", "the plate reader", "the clicker", "the other", "delivers",
  "offer"; "the reader" on its own stays a person); such a word must end its noun phrase or have a second one follow
  it (`_AGENT_END`: "to the fitter", "hand the fitter the bolts", but "the trainer shoes" are shoes). Hand, pass, give,
  bring and feed (any form, "spoon-feeds") take "me", "him", "her", "us" and `_PERSON` (a family word, "my"/"our"/
  "your" + up to three words + a person word, "elderly"/"disabled" + a word) with or without a thing after them
  ("hand me", "bring her the cup", "a robot that feeds me"). They and fetch, get, grab, toss, throw, chuck, lob, offer,
  serve, lend, loan and send (not a message: "send me an alert" is none) take every other receiver only with a thing
  after it (`_THING`: a determiner, a possessive or a noun; never a job done to them, a singular after a/an/some ("give
  them a shake", "give the parts a quick check"; "the wipes" and "the labels" are things), nor a robot asked for
  (`_NOT_A_ROBOT`: a robot word after a/an/some or none said to the user, a robot or cobot after a/an to anyone, or a
  robot word before that, which, to and a verb: "get me a robot arm that sorts screws", "give me robot arms", "get my
  grandma a robot arm"; "hand the nurse the device", "hand the nurse a device", "hand the operator the gripper" and
  "the machine parts" are things), nor a request (`_REQUEST`: a quote, an option, a way, an idea, a plan, a list and the
  like only when a robot, its design, its cost or a robot's job (`_JOB`: build, make, design, move, sort, stack, pick,
  place, lift, grip, transfer) follows within six words: "give me a cheap way to move parts", "get me a quote for an
  arm", "a list of parts for a robot", "a breakdown of the cost"; "get me started", "get me going"; "something
  that/to/for" and a robot's job: "give me something that stacks cups"; but "bring me my list", "give me the shopping
  list", "pass me the list", "bring me something for my headache" and "get me a quote" on its own hand a thing to
  someone)): "you" ("u", "ya", "y'all"), "the two of us"; "them" ("'em") and someone,
  anyone or everyone before a thing ("bring them their drinks", "bring them water"; not "bring them all to the bin",
  "two at a time", "get them done"), whoever and a clause ("give whoever asks a pen"); a title and a name; a person
  word (`_ROLE`: nurse, surgeon, orderly, operator, worker, packer, welder, fitter, stacker, chef, barista, cashier,
  customer, guest, diner, patron, bride, human, participant, the morning shift, the line lead, neighbour, friend,
  bestie, hubby, cousin, baby, teen, man, lady, people and some 230 more, and the family words `_KIN`: grandma,
  granma, grampa, nan, mum, mother, dad, father, auntie, uncle, baby, ...) after a determiner or a possessive ("the
  surgeon", "our packers", "my colleague", "the old man", "their nanny", "my 90 year old mum") or in the plural, with
  up to two describing words ("customers", "wheelchair users"); and any other noun phrase right after the verb when a
  second one follows it that starts with a determiner or a possessive (`_ANY_NP1`: "hand the bride her bouquet", "give
  the morning shift their parts", "hand bob the parts", "give grampa his pills", "hand the person in the wheelchair
  their cup"), unless it is a thing, a place, the robot, an animal, a plant, a function word, a verb compound ("hand
  pick the strawberries") or a way ("bring the tray the long way"). Those verbs and deliver, take, carry, present,
  transfer, return, dispense, distribute, dish out, hold, slide, drop, move, push, roll, wheel and flick, and the
  passive ("parts are handed to the operator"), reach a receiver after up to seven words, hyphenated ones too, and
  "to" (not "next to", "close to", "due to"; no "then", "while", "so" or robot word in between: "hand the x-ray films
  to the doctor", "pass the freshly washed and dried coffee mugs to the barista", "hold the cup out to her", "return
  the cup to my grandma"): any of those, a family word on its own ("pass the remote to mum"), a short family or casual
  word ("to ma", "to sis") or a common first name in lower case ("give the pills to mary"; `_NAMES`, without names
  that are also words, such as "will", "mark", "rose" or "max") at the end of the phrase, "them", "myself", "all of
  them", a plural word for someone by what they do ("to runners at the marathon"), or her, his, my, our, your or
  their before anything that isn't their place or an animal (`_POSS`: "to my grandma", "to their nanny"). Fetch, get,
  grab and bring also reach one after up to seven words and "for" ("fetch the remote for my dad"). And: "supply the
  welder with rods", "provide the operator with screws", "keep the operator stocked with screws"; "passes parts back
  and forth with a human"; "the person it hands the cup to", "the operator it passes parts to"; "drops the part into
  my hand", "puts the part in the operator's hand"; "take" with two objects ("take her a cup of tea", "take the nurse
  the tray"; not "it takes me 10 minutes"); handing things out (`HANDS_OUT`: "hand out the gloves", "pass
  around the snacks", "hand round the biscuits", "a robot that gives out flyers", "give the gloves out"; not round an
  obstacle or a place, out of something, nor a part that gives out); passers-by, bystanders, pedestrians, spectators,
  commuters and tourists are people. A capitalised word is no name at the start of a sentence, after "the" or "a", for a
  place, person or product word, a department or a brand ("Reception", "Assembly", "Pathology", "Recycling", "Goods
  In", "Finance", "Starbucks", "Haas"), a month or a day, and when the request is in Title Case or capitals. Baby and
  infant products are things (`brief._baby_goods`: "baby carrots", "baby food jars", "baby wipes", "infant formula"),
  except right after a giving or feeding verb with no one else to get them ("feed the baby food", "give the baby
  carrots": the baby may be the one fed). A tray denester and a bottle corker are machines; a grader, a candler, a
  stacker or a deburrer may be a person and stays one (a safe error: "move the eggs to the grader" is a hand-over). The patterns are compiled once (`brief._rx`).
  A person's place is no person. A possessive, a family word or a person word before 's or s' owns the place the things
  go to when a place, a piece of furniture, a container or a machine (`_PLACE`: desk, room, bedroom, office, workshop,
  kitchen, table, car, door, shed, shelf, drawer, bin, tray, station, line, machine, cell, panel, fridge, printer,
  garden bed and the like), after at most three describing words ("her bedside table", "my mum's kitchen table", "her
  old wooden kitchen table"), ends the noun phrase: the text ends there, or punctuation, a number, a function word
  (`_NOT_DESCRIBING`: and, at, so, every, please, ...), an adverb (`_ADVERB`: twice, daily, tonight, upstairs,
  quickly, ...) or a preposition (`_PREPOSITION`: near, next, over, up, without, ...) follows (`_PHRASE_END`): "bring
  the cup to her kitchen table near the window" is a pick and place. So "bring the mail to her desk", "bring them to
  her room", "bring the cups to his office", "give the parts to my dad's workshop", "bring the pills to her nurses'
  station", "bring the meds to her bedroom twice a day" and "give the bolts to his assembly line" are no hand-over. A
  person word followed by a place word names the place ("pass the parts to the operator station", "the worker cell",
  "the guest room"), and "her" or a person word whose next words go to, into or onto a place things are put away in
  or on is the thing's (`_PUT_AWAY`: furniture, a container, a machine or a station, not a room or an area: "bring her
  cup to the sink", "give her toys to the toy box", "bring her keys to the hook", "pass the guest towels to the
  shelf"). "In" or "on" a place says where the person is or how the thing comes, and a room or an area may be where
  the person is, so "bring her tea in the garden", "bring her medicine on a tray", "pass her pills on the tray",
  "bring her tea to the garden", "bring them tea in the garden" and "give the operator parts in the tray" are
  hand-overs (an animal's thing keeps the older guard, `_THEN_ANY_PLACE`: "bring the dog bowl to her porch"). The tool hand-over archetype's things going to a place are a pick
  and place (`_TO_A_PLACE`: "bring the tools to his workbench"; its workshop people keep the e-stop). Otherwise the
  person is the receiver, because a missed hand-over drops the emergency stop beside a person: after "her" or 's comes
  no place ("bring the meds to her twice a day", "gives the medicine to her nurse", "bring breakfast to her bedside
  every day", "give it to her husband", "bring the remote to my dad's friend"), or the place word only describes the
  person who follows it ("brings the medicine to her home nurse", "to her care home nurse", "to my dad's room mate",
  "to her office manager", "to his lab partner"). These are hand-overs with people nearby and an e-stop, also when
  the request says the person is alone or the robot works on its own ("bring the meds to her twice a day, she lives
  alone"). The safe errors: "to her sofa", "to her bed" and "to her armchair" (someone sits or lies there), "bring the
  cup to my left" and "to her side", a capitalised word that is no name and on no list ("bring the tea to Costa"), and
  "give the baby bottles to the sink" (children nearby, no hand-over). Known misses: a person word on no list, with
  none of the endings above, after "to" ("pass the bricks to the mason": "the mason jars" are jars), a word for
  someone by what they do with a bare thing after it ("hand the shelf painter cans"), and misspelt person words other
  than the few above. A place named after people
  infers none ("a robot for the operator station"). `PERSON_NEAR` applies the same
  place guard to its person mention and its "for/to/with" person, so "pass my dad's pills to the tray" has people
  nearby and "to her nurses' station" has none. The tables (`tests/fixtures/brief/handover_phrases.json`: 1,589
  requests from the release review's two hunts and the fixers' rounds, each with the task, people and e-stop it must get,
  run through `agents.run_turn`; and the regex tables) and the whole-brief tests are in `tests/test_brief_answers.py`.
- **The ledger records what ran.** `RobotProject.run`'s revision entry has `recomputed`: the stages it computed (in
  order, failures included; not the ones it carried over or that were blocked), and `reused`: the stages whose
  `stage_status` is "reused", early cutoffs included. Keewano's `StageRecomputed` events (`keewano.events_for`) and
  Teams' activity read `recomputed`, so a wrist-camera or reach edit no longer counts the gripper stage it reused. The
  start event's `dirty` stays `dirty_nodes(changes)`: the stages the edit may rerun (the Studio's "N of 9 stages
  affected"); each stage's "node" event then says whether it was reused.
- **The grip-limited payload is the firmware's.** `arm.grip_meta` puts the plan's `limit_n` (the firmware's
  `GRIP_LIMIT_N`: its hard stop and the F command's cap) in `asm.meta["grip_limit_n"]`, and the payload sweep's
  `grip_limited_payload_g` is 2 mu x min(the jaws' stall force, that limit) / g, with `grip_limit_n` in its metrics
  (the jaws' stall force without a shipped plan). The egg-picker: 1,356 g at 19 N (it said 5,913 g at 82.9 N, a squeeze
  its firmware never makes). Recorded in the simulation goldens (only `sim_payload_sweep.json` and `run_all` change).
- **The SO-101's shake margin is thin, and stays so.** The stock SO-101 holds its egg through the shake test with 4.74 mm
  of slip (4.83 mm on Dynamixel servos, DEMO_SCRIPT step 12) of the 5 mm allowed. A firmer grip doesn't buy margin: the
  egg creeps along the stock jaw as it seats (4.67 mm at 6 N, 4.53 mm at 11 N, 4.47 mm at 14 N, near the top of its
  1.26-16.67 N window; 4.56 mm at 14 N on Dynamixel), so the open-loop grip stays at the window's geometric mean,
  4.58 N. `tests/test_robot_grip_force.py::test_the_so101_demo_keeps_its_egg_through_the_shake_test_with_a_thin_margin`
  pins both slips (within 0.05 mm) and fails, saying so, when the margin drops under 0.1 mm; DEMO_SCRIPT says what the
  demo shows if it tips over (step 12: 7 issues, not 6).
