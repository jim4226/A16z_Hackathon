"""What happens before every tool call. Claude asks; Morph decides.

    check(...) -> Check
        "run"     run the call now
        "result"  answer it at once with Morph's own tool_result: a tool this run doesn't have, one turned off or
                  changed while the run waited, a paid tool, the per-tool limit, arguments that don't fit its schema
        "ask"     the run pauses until the person answers; `needs` lists the cards: credential (missing | rejected)
                  and approval (first_use | changed | per_call)

The rules (v0.2 beta):
- Morph's own core tools run without asking.
- A read-only tool runs once approved: "Always" on its card, or, for one of Morph's curated tools, saving its key (adding
  the key counts as approving its first use, so the person sees one card, not two). An approval is bound to the tool's
  fingerprint: a tool that changed asks again.
- Tools that write, upload or spend ask before every call; for them "Always" means this call only.
- No spending in this beta: a paid tool is refused with an honest result, never called.
- 40 Claude turns per run and at most 20 calls of one tool per run.
"""

from __future__ import annotations

import json
import os

from . import store
from .http_tool import validate_input

MAX_TURNS = 40
MAX_CALLS_PER_TOOL = 20
SPEND_CAP_USD = 0.0
DECISIONS = ("allow_once", "allow_always", "skip", "deny")
MAX_PREVIEW = 600
NO_KEYS_IN_CHAT = ("Do not ask the person to paste a key, password or token in chat; Morph collects keys only in its Keys "
                   "dialog.")


class Check:
    __slots__ = ("action", "result", "needs", "code")

    def __init__(self, action: str, result: dict | None = None, needs: list | None = None, code: str = ""):
        self.action, self.result, self.needs, self.code = action, result, needs or [], code

    def __repr__(self):
        return f"Check({self.action}, {self.code or [n['need'] for n in self.needs]})"


# ------------------------------------------------------------------------------------------------ approvals
class Approvals:
    """MORPH_HOME/.tools/approvals.json: {tool: {"fingerprint", "mode": "always" | "key_saved" | "draft", "by", "at"}}
    ("draft": granted when the owner approved the drafted tool's spec, draft.py)."""

    def __init__(self, home: str | None = None):
        self.home = store.home_dir(home)
        self.path = os.path.join(store.tools_dir(self.home), "approvals.json")

    def all(self) -> dict:
        data = store.read_json(self.path, {}) or {}
        tools = data.get("tools") if isinstance(data, dict) else None
        return {k: v for k, v in (tools or {}).items() if isinstance(k, str) and isinstance(v, dict)}

    def get(self, name: str) -> dict | None:
        return self.all().get(name)

    def approved(self, tool) -> bool:
        a = self.get(tool.name)
        return bool(a) and a.get("fingerprint") == tool.fingerprint and tool.side_effect == "read"

    def grant(self, tool, mode: str, by: str | None = None) -> dict:
        entry = {"fingerprint": tool.fingerprint, "mode": mode, "by": by, "at": store.utc()}
        with store.locked(store.tools_dir(self.home)):
            data = {"tools": self.all()}
            data["tools"][tool.name] = entry
            store.write_json(self.path, data)
        return entry

    def revoke(self, name: str) -> bool:
        with store.locked(store.tools_dir(self.home)):
            data = {"tools": self.all()}
            if data["tools"].pop(name, None) is None:
                return False
            store.write_json(self.path, data)
        return True


# ------------------------------------------------------------------------------------------------ results Claude sees
def tool_result(tool_use_id: str, payload, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    out = {"type": "tool_result", "tool_use_id": tool_use_id, "content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def error_result(tool_use_id: str, code: str, tool: str, message: str, **extra) -> dict:
    return tool_result(tool_use_id, {"error": code, "tool": tool, **extra, "message": message}, True)


def skipped_result(tool_use_id: str, tool: str, needs: list, decision: str) -> dict:
    """What Claude sees when the person skipped a call or said no (Morph's words, never a vendor's)."""
    cred = next((n for n in needs if n.get("need") == "credential"), None)
    if decision == "skip" and cred:
        return error_result(
            tool_use_id, "needs_credential", tool,
            f"Morph asked the person for {cred.get('label') or 'a key'} for {cred.get('service')}; they chose to continue "
            "without it. Carry on without this tool: use other tools, or say in your answer what is missing. " + NO_KEYS_IN_CHAT,
            status="skipped_by_user", service=cred.get("service"))
    if decision == "skip":
        return error_result(tool_use_id, "not_approved", tool, "The person skipped this call. Carry on without it and say "
                            "in your answer what is missing.", status="skipped_by_user")
    return error_result(tool_use_id, "not_approved", tool, "The person did not allow this call. Don't call it again in "
                        "this run; carry on without it.", status="denied_by_user")


def interrupted_result(tool_use_id: str, tool: str) -> dict:
    return error_result(tool_use_id, "interrupted", tool,
                        "Morph restarted while this call was running, so it may or may not have happened. Morph did not "
                        "repeat it. Don't call it again in this run; tell the person to check with the service.")


# ------------------------------------------------------------------------------------------------ cards
def preview(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= MAX_PREVIEW else text[:MAX_PREVIEW] + "..."


def approval_need(tool, reason: str, tu: dict) -> dict:
    return {"need": "approval", "reason": reason, **tool.card(), "input": preview(tu.get("input") or {})}


def credential_need(tool, reason: str) -> dict:
    c = tool.credential
    return {"need": "credential", "reason": reason, "tool": tool.name, "title": tool.title, "service": c.service,
            "label": c.label, "what": c.what, "where": c.where, "url": c.url, "cost": c.cost, "privacy": c.privacy,
            "env": c.env, "hosts": list(c.hosts)}


# ------------------------------------------------------------------------------------------------ the check
def check(run: dict, tu: dict, *, tool, frozen: dict | None, enabled: bool, approvals: Approvals, env,
          max_calls: int = MAX_CALLS_PER_TOOL, network: bool = True, planned: int = 0) -> Check:
    """Decide one catalog tool call (`tool`: the live catalog entry, None when it is gone; `frozen`: the run's record
    of the tool as it was sent to Claude; `planned`: earlier calls of the same tool in this turn that will run). Core
    tools never come here."""
    name, tid = tu.get("name", ""), tu.get("id", "")

    def answer(code, message, **extra):
        return Check("result", error_result(tid, code, name, message, **extra), code=code)

    if frozen is None:
        return answer("unknown_tool", f"There is no tool called {name[:60]} in this run.")
    if tool is None or not enabled:
        return answer("tool_unavailable", f"{name} was turned off in Morph while this run waited. Carry on without it.")
    if tool.fingerprint != frozen.get("fingerprint"):
        return answer("tool_changed", f"{tool.title} changed since this run started, so Morph won't run the old version in "
                      "this run. Carry on without it; the next run uses the new version.")
    if tool.paid:
        cost = f" (about ${tool.cost_usd:g} a call)" if tool.cost_usd else ""
        return answer("paid_tool_not_supported", f"{tool.title} costs money{cost}, and paid tools aren't supported in this "
                      "beta, so Morph didn't call it. Carry on without it and say in your answer what it would have added.")
    n = int((run.get("calls") or {}).get(name, 0)) + planned
    if n >= max_calls:
        return answer("call_limit", f"{tool.title} was already called {n} times in this run, the most Morph allows. Carry "
                      "on with what you have.")
    try:
        validate_input(tool.spec["input_schema"], tu.get("input") if tu.get("input") is not None else {})
    except ValueError as exc:
        return answer("invalid_arguments", f"The arguments don't fit this tool: {str(exc)[:300]}. Fix them and call it again.")
    if not network:
        return answer("offline", f"Network lookups are off in Morph, so {tool.title} can't run. Carry on without it.")
    needs = []
    once = tid in ((run.get("pending") or {}).get("once") or [])
    key_missing = tool.credential is not None and not env.get(tool.credential.env)
    if tool.side_effect == "read":
        if not once and not approvals.approved(tool) and not (tool.curated and key_missing):
            needs.append(approval_need(tool, "changed" if approvals.get(name) else "first_use", tu))
    elif not once:
        needs.append(approval_need(tool, "per_call", tu))
    if key_missing:
        needs.append(credential_need(tool, "missing"))
    return Check("ask", needs=needs) if needs else Check("run")
