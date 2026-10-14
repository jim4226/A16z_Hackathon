# Morph Teams (preview)

Teams turns one Morph Studio into a shared workspace: "GitHub for physical things". Teammates sign in from their own
browsers, comment on revisions, propose changes, review the stages they own and release revisions for building. It
runs on the team owner's computer: there is no Morph server or account, and the team's data never leaves that
computer.

| GitHub | Morph Teams |
|---|---|
| commit | a revision (`r004`), with who made it |
| pull request | a change request: design changes with a preview of every stage they recompute |
| CODEOWNERS | disciplines: the people who own the stages a change touches review it |
| review | an approval, per discipline, tied to the exact revision |
| release / tag | a release of a revision ("Pilot v1"), gated by build level |
| issues | comments on a revision, a stage, a BOM line or a parameter |
| branch, fork | variant lines from a revision; a fork copies a project |

## Run a team Studio

On the computer that will do the work (the owner's):

Make the team once, with you as its owner, then start the Studio, reachable from other computers:

```shell
morph team init --owner "Jim" --name "R Cubed"
morph studio --team --remote
```

From a git checkout, run `morph team init` through the launcher's Python (`.venv/bin/python -m morph.cli team init
...`; on Windows `.venv\Scripts\python -m morph.cli team init ...`), then start the Studio with the launcher, which
passes its options on: `./run_studio.sh --team --remote` or, on Windows, `.\run_studio.bat --team --remote`.

`morph studio --team` prints a **one-time sign-in link** for the owner; open it in your browser (it works once, within
15 minutes). Without `--remote` the Studio answers only on this computer, which is what you want behind an SSH tunnel.
With `--remote` it listens on the network, and the link starts with the address teammates use. Keep using the Studio
at that address: invite links start with the address of the page that made them, so an invite made from
`http://127.0.0.1` would only work on this computer (Share warns you).

A team Studio has no access code: every request needs a member's session. `MORPH_TOKEN` plays no part.

Need a new sign-in link later (a new browser, a lost cookie)? `morph team link` prints one for the owner, `morph team
link Ana` for a member; a signed-in member can make one for another device under **Team**, *Sign in on another device*.

## Invite people

**Share** (top right; owners only) makes a single-use invite link with a role and disciplines, valid for 7 days. Send it
privately: whoever opens it first joins under the name they type. From a terminal, make an invite, list who is on
the team, sign Ana out everywhere (her name stays on past work) and print the audit log with its hash chain checked:

```shell
morph team invite --role editor --disciplines electronics --name Ana --url http://192.168.1.20:8765
morph team members
morph team revoke Ana
morph team audit --verify
```

### Roles

| Role | Can |
|---|---|
| viewer | see everything, comment and propose changes (both switchable in the team policy); no editing tools on the page |
| editor | also chat, edit parameters, build, import, apply change requests, request reviews, branch and release |
| owner | also manage the team (invites, roles, disciplines, policy), API keys, settings, Reset demo, deleting projects, the audit log |

### Disciplines

Each member can hold several. A discipline owns pipeline stages:

| Discipline | Stages |
|---|---|
| Mechanical | sizing, gripper, robot CAD |
| Simulation | simulation |
| Electronics | electronics plan, schematic, PCB and firmware |
| Manufacturing | manufacturing |
| Sourcing | BOM and Darwin sourcing |
| Documentation | assembly manual |
| Generalist | every stage that has no specialist |

A **generalist** (the team's creator starts as owner and generalist) owns no stage outright: they are asked for every
discipline that has no specialist on the team, and their approval counts for any discipline it covers. Generalists
have *Watch all changes* on by default, so every change request lands in their inbox without making them a required
reviewer.

## Change requests, reviews and releases

1. Anyone who may propose opens **Changes**, *Propose a change*: parameter values, or a stage to look at. *Preview*
   says what the change does before anything builds, in numbers: the torque margin of each joint, the servos, the bus
   protocol, controller and supply, the connectors and firmware library, the geometry the CAD is rebuilt around, the
   grip force, the purchased parts' cost and the issues it would add or clear (a Dynamixel servo on a Feetech bus, for
   example). These are estimates from the pipeline's fast stages (sizing, the gripper checks, the electronics plan) and
   catalog prices; the build decides. The preview also lists every stage the change recomputes and who must review it
   (a servo swap touches nearly every stage). A discipline's specialists are asked first; the author is never asked to
   review their own request, so when they are the only specialist the generalists are asked instead, and the team
   owners when there are none. The change request keeps its preview for its reviewers, and once built it shows what
   the build measured.
2. The owners of those disciplines get it in their inbox. *Approve as Electronics and Manufacturing* records one
   approval per discipline for that exact revision; *Request changes* sends it back with a note. Above prototype nobody
   approves (or asks for changes on) work they made: the page says who can approve it instead.
3. An editor *Apply*s it: the room's queue builds it like any edit, built by the editor and credited to the author.
4. **Review** shows the release gate of the revision on screen: which disciplines are approved, by whom, and who can
   approve the rest. A later edit keeps an approval only if every stage of that discipline was reused unchanged;
   otherwise that discipline must approve again (GitHub's "dismiss stale reviews", scoped to the work it touched).
   Switching the electronics to a custom PCB, for example, reruns everything from the electronics plan on but not
   sizing, the gripper or the simulation, so Simulation's approval carries forward and the other disciplines approve
   again. A wrist camera rides on the hand, so it re-sizes the joints and every discipline approves again.

| Build level (`build.config`) | A release needs |
|---|---|
| prototype | 1 approval; the author may approve |
| pilot | every affected discipline's owner, by at least one person who isn't the author |
| production | every affected discipline's owner plus the team owner, by at least two people who aren't the author |

The team policy can require a specialist (not a generalist) for some disciplines at pilot or production.
**Release** tags the revision and unlocks its order files (gerbers, JLC files, manufacturing zips): before a release,
editors download them marked as drafts (logged) unless the policy keeps them to owners. **Export** follows the same
rule, and a team export carries `team.json`: the revision's approvals and releases (or the whole project's comments,
change requests, reviews, branches and releases), with names, never member ids.

**Comments** anchor to a revision, a stage, a BOM line or a parameter, and `@name` notifies someone. **Activity** is
the project's feed plus your inbox; the bell in the header counts what's new. The avatars show who has the project
open and what they're looking at.

## Security notes

- **Plain HTTP.** Sign-in and invite links, session cookies and everything the team does travel unencrypted between
  browsers and the Studio. Use it on a network you trust, over a VPN such as Tailscale, or through an SSH tunnel
  (`ssh -L 8765:127.0.0.1:8765 you@server` on each laptop, with the Studio started without `--remote`).
- **Links and sessions.** Invite codes (128-bit) and script tokens are stored hashed. Invites work once for 7 days;
  sign-in links once for 15 minutes. Sessions are signed cookies valid for 30 days; removing a member (Share, *Remove*,
  or `morph team revoke`) signs them out everywhere, and their open pages stop within seconds and say why. An address
  that sends 10 wrong codes within 10 minutes is refused until the oldest of them is 10 minutes old (behind a tunnel
  everyone shares one address, so a script with a wrong token can hold up new sign-ins for up to 10 minutes; existing
  sessions are unaffected).
- **What is logged.** The audit log (`projects/.team/audit.jsonl`, hash-chained) records sign-ins with IP addresses,
  invites, role changes, API key and settings changes (names only, never values), exports, imports (with their links),
  Reset demo and deleted projects.
- **Where the data is.** Members, invites and the audit log are in `projects/.team/`; comments, change requests,
  reviews, branches and releases in each `projects/<project>/.team/`. Nothing is sent anywhere: Teams makes no Claude,
  SubQ or Keewano calls, and team activity is never sent to Keewano.
- **API keys** stay the owner's: only owners see or change them, and every member's chat uses them.
- **Approvals help your process; they don't certify that a robot is safe.**

More: [SECURITY.md](../SECURITY.md) (reporting a problem, remote mode) and [PRIVACY.md](PRIVACY.md) (what leaves the
computer, and what each optional service keeps).

## Limits of the preview

- One computer serves the team; presence is kept in memory and resets when the Studio restarts.
- A launcher rebuild ("rebuilt with latest Morph") recomputes every stage, so every approval goes stale.
- Branch revisions interleave with main in the revision strip.
- Not tested on Windows and macOS yet.
