# Agent tools (beta)

Morph's agents can use outside services, such as a distributor's price and stock search, as tools. They find the tool
they need, ask you for a key or an OK in the chat when they need one, and carry on by themselves once you answer. The
first agent that uses them is **Live prices** (BOM tab): Claude checks current distributor prices and stock for the
purchased parts of one revision. Nothing is ever ordered, reserved or paid for.

A tool is a **spec, not code**: a JSON manifest that says which address to call, what to send and what to keep from
the answer. One hardened runner in Morph executes every manifest the same way. Morph ships two read-only distributor
manifests (Mouser, and Farnell with element14 and Newark), the owner of a Studio can add their own, and Claude can
draft one from an API's documentation for the owner to review (*Morph writes a tool*, preview).

Everything here is beta. The distributor tools were written from the providers' documentation and have **not yet been
tried with real keys**; the Studio tags them "Not yet tried with a real key" until someone has.

## How an agent finds a tool

Each run sends Claude the agent's own core tools (Morph code: for Live prices, read the parts list, search Darwin,
record a price, ask for a missing tool) and every tool that is on, held back behind a tool search: Claude searches for
what it needs ("distributor price stock") and only the tools it finds are loaded. Claude sees each tool as Morph
describes it (the manifest's text plus Morph's line on the service, host, side effect, cost and key), only as a tool
definition, never in its instructions. What a tool answers reaches Claude as data to use, never as instructions: Morph
tells it so, keeps only the fields the manifest names, and removes every known key first.

A tool whose key isn't set can still be found. When Claude calls it, the run pauses and the chat asks for the key.
When nothing in the catalog covers what the agent needs, it says so: the card "The agent needs a tool Morph doesn't
have" lists services Darwin's public search knows for that need, and offers the owner **Draft a tool (preview)**.

## The cards in the chat

Every run gets one card in the chat of its project, with a line for each tool it used. It says what the run waits for:

| Card | When | What you can do |
|---|---|---|
| **Key needed: *service*** | a tool needs a key that isn't set | **Add key** (opens Keys on that key), **Get a key** (the provider's page), **Skip** (the run carries on without it). The card says what the key is for, what it costs and exactly which host gets it. Once the key is saved the run continues by itself ("Key added, continuing...") |
| ***service* refused the key** | the service answered 401 or 403 to the saved key, or an answer its manifest says means a refused key (Mouser answers a wrong key with 200 and an error for the API Key) | **Replace key**: it may be mistyped, expired or not enabled for that service |
| **Allow *tool*?** | the first use of a read-only tool nobody has allowed yet, or of one that changed since it was allowed (Morph's own tools skip it once you save their key in Keys) | **Allow once**, **Always**, **Skip**. It shows what is sent, to which host, the cost and the call's arguments |
| **Allow this call to *tool*?** | every call of a tool that writes or uploads | **Allow once** or **Skip**, every time |
| **The agent needs a tool Morph doesn't have** | no tool covers the need | Darwin's suggestions; the owner can draft a tool |
| **Paused** (the Studio restarted, Claude was unavailable or refused the Anthropic key, network lookups or agent tools were turned off) | the run stopped before its next step | **Continue** once the cause is fixed; the owner also gets **Open Keys** on the switch or key that stopped it |

The status tag reads Working, Stopping (Stop waits for the call or Claude's answer under way, then the run ends),
Needs a key, Needs your OK, Paused, Done, Stopped (it failed, and says why), Declined (Claude declined the request) or
Cancelled. Tags on a tool say where it came from: "drafted" (Claude drafted it, the owner approved it) or "your own
manifest"; Morph's own tools have neither.

Cards are rebuilt from the Studio when the page loads, so a reload or another tab shows the same thing.

**Teams**: only the owner adds keys, allows tools, turns tools on or off and drafts tools. Editors start, continue and
stop runs and may skip a key or a call. Viewers see the cards and are told whom to ask.

## Keys

A tool's key is saved in the Keys dialog under its service ("Tool keys"), in `.env` as `MORPH_TOOL_<NAME>`, like
every other key: readable by your user only on macOS and Linux, never shown again, never sent back to the browser.
Morph checks it against the pattern the manifest gives before saving it, sends it **only to that tool's own hosts**,
in the header or query parameter the manifest names, and **never to Claude**. A chat message that contains it is
refused. Saving a key that a paused run waits for resumes that run once, however often it is saved.

A key stays listed while it is saved, even when no tool that is on uses it any more: "not used now" (its tools are
off), or under "Keys no tool uses" (its manifest is gone), where it can only be removed. `morph doctor` says so too.

## Approvals and limits

- Morph's own read-only tools: saving their key in Keys (from the card or the dialog) counts as allowing them. With a
  key put in `.env` by hand, they ask on first use like other tools.
- Other read-only tools ask on first use: **Allow once** (this time) or **Always**. Always is bound to the tool exactly
  as it was: a tool whose manifest changes asks again.
- Tools that write or upload ask before every call. A tool that spends money or has a fee is refused in this beta: the
  spending cap is $0.
- One run makes at most 20 calls of one tool and 40 requests to Claude.
- **Let agents use tools (beta)** (Keys, Privacy and network) off: no run starts, and a run that is working stops before
  its next request or call and waits. **Allow network lookups** off does the same, and nothing at all leaves the
  computer.
- **Check live prices after a build changes the purchased parts** (off by default) starts Live prices by itself; it
  uses Claude on your Anthropic key.
- A run or draft untouched for 7 days is gone: it can't be continued or approved, and the next start deletes its file.
- Deleting a project ends its runs that still wait or work, and Reset demo ends those of the revisions it removes:
  their cards say "Stopped" and why, and a new project that takes the name starts with nothing waiting.

## Where things are kept

| What | Where (MORPH_HOME, by default `projects/` in the Morph folder) |
|---|---|
| Agent runs: the conversation with Claude, the tools' answers (reduced and without keys), what is pending | `.runs/<run id>.json`, private to your user, deleted 7 days after the last change |
| The owner's own manifests, and drafted tools once approved | `.tools/catalog/*.json` |
| Which tools are on, approvals, drafted tools' pins (sha256) | `.tools/state.json`, `.tools/approvals.json`, `.tools/pins.json` |
| Drafts of a tool (never the documentation's text) | `.tools/drafts/`, deleted 7 days after the last change |
| Needs no tool covered | `.tools/requests.jsonl` |
| The log of every tool decision and call | `.tools/audit.jsonl` |

The log records names, hosts, outcomes, timings and costs, never a key, a tool's arguments or an answer. Each line
carries the hash of the one before it, so an edited, removed or reordered line shows up; in a terminal set up as in
[Commands in a terminal](../README.md#commands-in-a-terminal),
`python -c "from morph.studio.toolbox import audit; print(audit.verify())"` checks the chain.

## The manifest format

A manifest (`morph.tool_manifest/1`) describes one service: who it is, what it does with the data, the hosts its
keys and calls may go to, its keys and its tools. This one is the test fixture's (`tests/fixtures/tools/fixparts.json`,
a made-up distributor), cut to one tool:

```json
{
  "schema": "morph.tool_manifest/1",
  "id": "fixparts",
  "service": "FixParts (test fixture)",
  "homepage": "https://www.fixparts-morph.com",
  "privacy": "Manufacturer part numbers and quantities from this project's parts list go to FixParts.",
  "hosts": ["api.fixparts-morph.com"],
  "credentials": [
    {
      "id": "fixparts",
      "env": "MORPH_TOOL_FIXPARTS_KEY",
      "label": "FixParts API key",
      "what": "Live price and stock for the purchased parts in a robot's parts list.",
      "where": "FixParts dashboard, Developers, API keys",
      "url": "https://www.fixparts-morph.com/developers/keys",
      "hosts": ["api.fixparts-morph.com"],
      "pattern": "^fxp_[A-Za-z0-9]{24,64}$",
      "prefix": "fxp_",
      "cost": "Free: 1,000 lookups a month"
    }
  ],
  "tools": [
    {
      "name": "fixparts_offers",
      "title": "FixParts price and stock",
      "description": "Live distributor price breaks and stock for one manufacturer part number.",
      "side_effect": "read",
      "sends": "the manufacturer part number",
      "request": {"method": "GET", "url": "https://api.fixparts-morph.com/v1/parts/{mpn}/offers",
                  "query": {"currency": "{currency}", "limit": "5"}},
      "auth": {"credential": "fixparts", "in": "header", "name": "Authorization", "prefix": "Bearer "},
      "input_schema": {"type": "object", "additionalProperties": false, "required": ["mpn"],
                       "properties": {"mpn": {"type": "string", "pattern": "^[A-Za-z0-9._/+-]{2,64}$"},
                                      "currency": {"type": "string", "enum": ["USD", "EUR"]}}},
      "response": {"type": "json", "extract": {"offers": "/data/offers", "total": "/meta/total"}, "max_items": 10},
      "limits": {"timeout_s": 10, "max_response_bytes": 1000000},
      "cost": {"usd_per_call": 0}
    }
  ]
}
```

- `id`: 2 to 24 lowercase letters and digits, never a name Morph uses itself (morph, claude, darwin, mcp and the like).
  Tool names start with it.
- `hosts`: public DNS names only, never an IP address, `localhost` or a local network name. Every request and every
  key stays on these hosts; a key's own `hosts` may narrow that further.
- `credentials[].env`: `MORPH_TOOL_` and 2 to 40 capitals, digits or underscores. `pattern` is what a key must look
  like (anchored, no groups or alternatives), so a wrong paste is caught before it is saved.
- `side_effect`: `read`, `write`, `upload` or `spend`; a tool with a fee must say `spend`.
- `request`: `{name}` placeholders take the inputs, which are checked against `input_schema` (an object of plain
  values, nothing else allowed).
- `response`: JSON only; `extract` keeps fields by JSON pointer and `max_items` caps lists. Everything else the
  service answers is dropped before Claude sees it. A service that refuses a key without a 401 or 403 can say how
  with `auth_error`, `{"pointer": "/Errors", "match": {"PropertyName": "API Key"}}`: an answer whose object at the
  pointer (or an object in the list there) has every `match` field is a refused key, and the card offers **Replace
  key**. Only a tool that sends a key may have one.
- `limits`: at most 30 seconds and 2 MiB per call.

Unknown fields are refused at every level. A manifest that breaks a rule isn't loaded; Keys lists it with the reason.
An owner manifest that reuses one of Morph's provider ids, tool names or key names is not loaded. The full rules are in
[RELEASE_CONTRACTS section 15](internal/RELEASE_CONTRACTS.md#15-tool-broker-b2-t1-toolbox-core-b2-t2-tool-keys).

To add your own: put the file in `projects/.tools/catalog/`, then turn its tools on in Keys, under "Tools agents can
use" (an owner's tools start off).

## Drafting a tool (preview)

When a run needs a tool Morph doesn't have, the owner can press **Draft a tool (preview)** on its card, or **New tool
(preview)** in Keys. You name the need and the address of the API's official documentation. Claude reads pages of that
documentation site only (at most 4) and fills in a form, never code; Morph builds a manifest from the form and checks
it:

- lookups only (GET, at most 4 of them), no writes, uploads or payments; $0 a call;
- every address HTTPS to a public DNS name that the pages Claude read name, outside any text aimed at AI agents, and
  that resolves to public addresses;
- the key under a `MORPH_TOOL_*` name, sent only to those hosts;
- no wording that addresses an AI, and no clash with Morph's own tools.

The review shows the hosts and the documentation line that names each one, every lookup's address and what it sends,
where the key goes, the cost, privacy, the pages read, warnings ("Check before approving") and the spec's sha256.
**Approve and install** installs exactly that spec, bound to its sha256: the next run can use it and asks for its key
in the chat. If the installed file changes afterwards, its tools turn off until a new draft is approved. A draft that
breaks a rule says why and can't be installed.

The checks can't tell documentation that is malicious from end to end from honest documentation; your review is the
last control. Read the hosts and lookups before you approve.

## What has been verified

| What | How it was checked | With real services |
|---|---|---|
| Mouser keyword and part number search | Written from Mouser's Swagger document (read 2026-10-13). Offline tests check what the runner sends and what it keeps from an answer that follows the documented schema (made-up values, plus a line that tries to give Claude instructions) | Not tried. `tests/test_toolbox_vendors_live.py` (`MORPH_LIVE=1` with a Mouser key) |
| Farnell, element14 and Newark keyword search | Written from the Product Search API (REST) description (read 2026-10-13). Offline tests on the documentation's own example answer | Not tried. Same file, with an element14 key |
| The requests to Claude: tool search, the catalog held back, strict core tools, fallbacks, an unchanged prefix on every resume | The real Anthropic SDK over a mock transport; the stored prefix is compared on every resume and after a restart | Not tried. `tests/test_toolbox_live.py` (`MORPH_LIVE=1` with an Anthropic key) |
| Live prices end to end | A real Studio with a local stand-in for Claude (`tests/fake_claude.py`) and a fixture distributor, in a real browser at three window sizes (`tests/e2e/test_e2e_tools.py`, `MORPH_E2E=1`) | Not tried with real Claude and a real Mouser key |
| Drafting a tool | The stand-in with a made-up API's documentation, clean and with planted instructions for AI agents (`tests/e2e/test_e2e_tool_draft.py`) | Not tried. `tests/test_toolbox_draft_live.py` (real Claude and a real documentation page) |
| The runner's address rules | Offline tests: names that resolve to private, loopback, link-local, shared or cloud metadata addresses, IPv6 forms that hide an IPv4 address, local network names, redirects, a slow name server, answers that are too large or arrive a byte at a time. The call goes to the address that was checked, so a second lookup can't redirect it | Not applicable |
| Keys stay in their slot | Marker keys searched for in every request to Claude, every event, API answer, run, draft and audit file, the server's output and an export (`tests/test_toolbox_hardening.py`) | Not applicable |
| Teams roles and remote mode | Every tools route for owner, editor, viewer, other disciplines and a removed member; remote mode with and without its access code | Not applicable |

## Not yet tried with real keys

- Whether Mouser and element14 accept the requests as written, and whether their answers have the fields the
  manifests keep, and their rate limits. A wrong key was tried with made-up keys (2026-10-15): element14 answers 403
  ("Developer Inactive") and Mouser 200 with an error for the API Key (its manifest's `auth_error` rule); both show
  **Replace key**.
- Real Claude finding the distributor tools through tool search, the prompt cache on a resumed run, the server-side
  fallback after a refusal, and Claude's web fetch staying on the documentation's site when it drafts a tool.
- Prices: Morph keeps a price only from a tool that answered in the run and marks one it can't find in that answer
  "check it", but nobody has compared Live prices with a distributor's own page yet.
- A Studio behind an HTTP proxy: the runner connects directly, so tool calls fail there for now.

[docs/RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) lists the checks to run with real keys before a release, and
[docs/PRIVACY.md](PRIVACY.md) what each service receives.
