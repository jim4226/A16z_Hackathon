# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/jim4226/A16z_Hackathon/security/advisories/new) (the Security tab of
this repository). Don't open a public issue for it, and don't put API keys, exploit code or private designs in an
issue, a screenshot or a log. If one of your keys was exposed, revoke it with its provider first.

We aim to reply within a week. Morph is a beta from a small team, and there is no bug bounty.

## Supported versions

Only the latest release gets security fixes. In a git clone, `run_studio.bat --update` (Windows) or
`./run_studio.sh --update` (macOS, Linux) switches to it.

## What Morph protects

Morph Studio is a local app: a small web server on your computer, used from your browser.

- By default it listens on 127.0.0.1 only and checks the Host and Origin of every request, so other websites can't
  drive it (no DNS rebinding, no cross-site requests).
- Files are served only from the open project's revisions. Project names, robot names and request bodies (size,
  type, shape) are checked before anything is created or read. Generated files open sandboxed, so they can't run
  scripts with the Studio's rights. The page loads nothing from other sites.
- A connection gets 20 seconds in all to send its request line and headers, and the Studio serves at most 64
  connections at once, 48 of them live event streams. One address may hold at most 16 connections that haven't signed
  in (with the access link or a team session; this computer's own addresses aside), and signed-in pages don't count
  towards that. More get a "busy" answer (503) straight away, so one client without the access link can't tie the
  server up. Many addresses together still can fill every slot, which is one more reason to use remote mode only on
  a network you trust.
- API keys live in `.env`, readable by your user only on macOS and Linux. The Studio never sends a key back to the
  browser or prints it. Text you type anywhere else (chat, New robot, Show Morph the job, Teams comments and names)
  that looks like an API key or a Morph access code is refused before it is saved, shown or sent.
- Every optional service (Claude, Keewano, Darwin, SubQ) can be switched off; [docs/PRIVACY.md](docs/PRIVACY.md)
  lists what each one receives.
- Imports treat every file and link as hostile: archive limits (entries, sizes, compression ratio, no links or nested
  archives), no XML entities, meshes and paths kept inside the import, xacro expanded in a separate process with a
  timeout, and links only to github.com and Hugging Face over HTTPS, refusing private addresses on every redirect.
- Team mode (`--team`): members sign in with single-use invite links or 15-minute sign-in links; session cookies are
  signed with the team's secret, codes and script tokens are stored hashed, wrong codes are throttled, revoking a member
  ends their sessions (and, within seconds, the live updates of pages they still have open), and the role table checks
  every request. Order files of a revision nobody released follow the team's download policy on Export as well as on
  direct downloads. The audit log is hash-chained (`morph team audit --verify`) and records sign-ins, invites, member,
  role and policy changes, script tokens, releases, key and settings changes, exports and draft order downloads,
  imports, resets and deleted projects.
- Agent tools (beta, [docs/TOOLS.md](docs/TOOLS.md)): a tool is a spec, not code. A JSON manifest says which address to
  call, what to send and what to keep, and one hardened runner executes it; Morph never runs code that a service or
  Claude wrote.
  - **Keys are bound to hosts.** A tool key (`MORPH_TOOL_*`) goes only to its tool's own hosts, in the header or query
    parameter its manifest names. It never goes to Claude and never appears in a card, event, log, run file, API
    answer or export: every known key is removed from what a service answers before anything is kept or shown, even
    when the service echoes it back, and a request that would carry a known key anywhere else is refused.
  - **Network rules for every call:** HTTPS only, to the manifest's hosts, which must be public DNS names. Every address
    a name resolves to must be public (no private, loopback, link-local, cloud metadata, shared, reserved or multicast
    address, nor an IPv6 form that wraps one), and the connection goes to the address that was checked, so a second
    lookup can't move it. No redirects. One deadline per call, the name lookup included; JSON answers only, capped in
    size and cut to the fields the manifest names.
  - **Approvals:** Morph's own read-only tools run once you save their key in Keys; other read-only tools ask once,
    bound to the exact tool (a changed tool asks again); tools that write or upload ask before every call; tools that
    spend money or have a fee are refused. A run makes at most 20 calls of one tool and 40 requests to Claude. Turning
    off *Let agents use tools* or network lookups stops a working run before its next step, and Stop ends it once the
    call under way returns. In a team only the owner adds keys, allows tools, turns them on and approves drafted tools.
  - **What a service answers is data, never instructions:** Claude is told so, and Morph decides what is kept (a price
    only from a tool that answered in that run, marked "check it" when Morph can't find it in that answer).
  - **Drafted tools (preview):** Claude fills in a form from the documentation you name; Morph checks the result (GET
    lookups only, hosts that the documentation names and that resolve to public addresses, no wording aimed at an AI)
    and installs only the spec the owner approved, bound to its sha256. A later change to the file turns its tools off.
  - **Audit log:** every tool decision and call goes into `projects/.tools/audit.jsonl`, hash-chained like the Teams
    log. It records names, hosts, outcomes and timings, never a key, a tool's arguments or an answer.

## What it doesn't protect

- **Remote mode (`--remote`) is plain HTTP.** Morph then listens on every network interface. Anyone with the access
  link (it contains the access code) can do everything you can: edit and reset projects, change keys, and spend your
  API credits. The code, the session cookie and any key you save in the Keys dialog cross the network unencrypted, so
  anyone who can watch that network can copy them. Use remote mode only on a network you trust or over a VPN such as
  Tailscale. From anywhere else, use an SSH tunnel instead (`ssh -L 8765:127.0.0.1:8765 you@server`, no `--remote`).
  Never open port 8765 to the internet. Restarting Morph changes the code, unless `MORPH_TOKEN` in `.env` fixes it.
  After 10 different wrong codes from one address, Morph stops checking that address's codes for 15 minutes. Still
  keep the generated code, or make `MORPH_TOKEN` long (20 or more characters).
- **Team mode is plain HTTP too.** Invite and sign-in links and session cookies can be copied by anyone who can watch
  the network: use it on a network you trust, or behind HTTPS (a tunnel or VPN). An approval records who reviewed a
  revision; it doesn't certify that a robot is safe.
- **A provider misusing data you approved.** Once you add a tool's key or allow a call, that service receives what the
  card says it sends. What it does with it afterwards is up to its own terms; Morph can't see or stop it.
- **Documentation that is malicious from end to end.** When Claude drafts a tool, Morph's checks can't tell such
  documentation from honest documentation; the owner's review is the last control. An owner's own manifest is trusted
  as written too (the runner's host, address, key and size rules still apply).
- **Answers that mislead.** A tool's answer can be wrong or written to mislead Claude about a price or stock level.
  Morph marks a price it can't find in the tool's answer "check it", and nothing is ever ordered.
- xacro files run in a separate process as your user; their memory limit applies on macOS and Linux only.
- Programs running as your user can read your projects and `.env`.
- Generated designs, wiring and firmware are not bench-validated. Check them before you power real hardware.

## In scope

The Studio server and its API, file and path handling, the access link and cookie, `.env` handling, the launchers
and `--update`, robot and file imports, the agent tool runner, manifests and drafted tools (a key reaching anything
but its own hosts is always in scope), and any case where Morph sends data that [docs/PRIVACY.md](docs/PRIVACY.md)
doesn't mention. Out of scope: attacks that already need code running as your user, denial of service by someone who
has the remote link, and the third-party services themselves.
