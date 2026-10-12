# Local beta privacy and data flow

R Cubed Studio runs on your computer and listens only on loopback. This beta has no R Cubed cloud
account, project upload or shared workspace. Local processes running as you may access your files;
the app's local session protection does not isolate it from a compromised operating system.

| Feature | Data leaving the computer | Trigger |
|---|---|---|
| Claude design assistant | Your prompt and the current design specification; authentication to Anthropic | A chat request with a configured key and network access enabled |
| Claude connection test | API key and model-access query; no design | Test connection |
| Darwin | Component names and fabrication/search queries; listed CAD files are not uploaded | Explicit sourcing permission plus Find agents, or an explicit CLI sourcing command |
| BoardRepo | Requested public board identifier | Explicit reference-board lookup |
| Keewano | Project/object names, component/material choices, timings, measurements and sourcing events | Configured developer credentials AND explicit telemetry consent |
| Setup | Package downloads from Python package indexes | Initial setup or deliberate version upgrade |

Your provider's terms and retention rules govern data you send to that provider. Turn network access
off in Settings, or launch with `MORPH_OFFLINE=1`, to prevent app provider requests. Setup downloads
are a separate installation step. No external fonts or analytics are loaded by the Studio interface.

## Keys

Settings stores Claude keys in a supported operating system credential store. Session-only mode
keeps a key in process memory until the app closes. No plaintext fallback is used when secure storage
is unavailable. The configuration JSON contains preferences, not keys. No endpoint returns a saved
key. Closing Settings clears the key input. Never put credentials in chat or project parameters.

Remove disconnects Claude and disables a developer environment credential from silently returning.
If the system store is locked or unavailable, the app reports that a previously saved key could not
be removed; unlock the store and use Remove again. Removing the key here does not revoke it at
Anthropic; manage provider credentials in the provider's account console.

The environment variables and optional `.env` file are developer conveniences and remain plaintext
on the developer's computer. `.env` is not loaded unless `MORPH_LOAD_DOTENV=1` is set. Do not commit
or send these files to support.

## Projects and exports

Projects live under the per-user R Cubed data directory, or `MORPH_HOME` when configured. This
contains design specifications, generated artifacts, revision metadata and a local activity ledger.
Studio chat is kept in bounded process memory; revision labels may preserve request text. Deleting
the application installation does not delete projects or saved credentials.

Export revision includes allowlisted engineering artifacts and a validation manifest. Application
preferences, credentials, chat history and ledgers are excluded. Review your design artifacts before
sharing them: design names, dimensions and requirements may still be confidential.

No telemetry is required. Automatic calibration from remote history is disabled in this beta.
Legacy revisions can still contain older calibration results; regenerate and review them.
