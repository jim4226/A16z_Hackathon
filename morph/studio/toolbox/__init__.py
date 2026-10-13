"""Morph's toolbox (beta): agents pull in tools as they need them and ask the person for a key, an approval or a
restart's go-ahead, then carry on by themselves.

    catalog.py    morph.tool_manifest/1 manifests -> Claude tool definitions (Morph's descriptions), fingerprints, keys
    policy.py     approvals, keys and spending before every call; the caps
    runs.py       Toolbox: Morph's own agent loop (frozen request prefix, pause and resume, interrupted runs)
    http_tool.py  the one hardened runner every catalog tool goes through
    audit.py      the hash-chained MORPH_HOME/.tools/audit.jsonl
    store.py      private atomic files, folder locks and run claims

Standard library plus the anthropic SDK (imported only when a run calls Claude). Nothing here is pipeline code.
"""

from .catalog import Catalog, Credential, ManifestError, Tool, key_entries, key_specs, looks_like_tool_key
from .http_tool import HttpPolicy, SpecError
from .policy import Approvals
from .runs import Builtin, Toolbox, ToolboxError, register

__all__ = ["Approvals", "Builtin", "Catalog", "Credential", "HttpPolicy", "ManifestError", "SpecError", "Tool", "Toolbox",
           "ToolboxError", "key_entries", "key_specs", "looks_like_tool_key", "register"]
