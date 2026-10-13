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
  connections at once, 48 of them live event streams. More get a "busy" answer (503) straight away, so slow or idle
  connections can't tie the server up.
- API keys live in `.env`, readable by your user only on macOS and Linux. The Studio never sends a key back to the
  browser or prints it.
- Every optional service (Claude, Keewano, Darwin, SubQ) can be switched off; [docs/PRIVACY.md](docs/PRIVACY.md)
  lists what each one receives.
- Imports treat every file and link as hostile: archive limits (entries, sizes, compression ratio, no links or nested
  archives), no XML entities, meshes and paths kept inside the import, xacro expanded in a separate process with a
  timeout, and links only to github.com and Hugging Face over HTTPS, refusing private addresses on every redirect.
- Team mode (`--team`): members sign in with single-use invite links or 15-minute sign-in links; session cookies are
  signed with the team's secret, codes and script tokens are stored hashed, wrong codes are throttled, revoking a
  member ends their sessions, and the role table checks every request. Order files of a revision nobody released
  follow the team's download policy on Export as well as on direct downloads. The audit log is hash-chained
  (`morph team audit --verify`) and records sign-ins, invites, member, role and policy changes, script tokens,
  releases, key and settings changes, exports and draft order downloads, imports, resets and deleted projects.

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
- xacro files run in a separate process as your user; their memory limit applies on macOS and Linux only.
- Programs running as your user can read your projects and `.env`.
- Generated designs, wiring and firmware are not bench-validated. Check them before you power real hardware.

## In scope

The Studio server and its API, file and path handling, the access link and cookie, `.env` handling, the launchers
and `--update`, robot and file imports, and any case where Morph sends data that [docs/PRIVACY.md](docs/PRIVACY.md)
doesn't mention. Out of scope: attacks that already need code running as your user, denial of service by someone who
has the remote link, and the third-party services themselves.
