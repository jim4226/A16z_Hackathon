"""Natural-language front end: need -> design spec, and component research.

Uses Claude when ANTHROPIC_API_KEY (or an `ant auth login` profile) is
available; otherwise falls back to a deterministic parser so the demo never
depends on the network.
"""

from __future__ import annotations

import os
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .materials import MATERIALS
from .spec import Design

MODEL = "claude-opus-5-5"


class NeedSpec(BaseModel):
    project_name: str = Field(description="short kebab-case name")
    object_name: str
    shape: Literal["cylinder", "box", "sphere"] = Field(
        description="cylinder = grasped lying on its side; box; sphere")
    diameter_mm: float = Field(description="diameter, or box height in the grip plane")
    length_mm: float = Field(description="extent along the finger width axis")
    mass_g: float
    friction: float = Field(description="estimated friction coefficient finger/object, 0.2-1.0")
    gripper_force_n: Optional[float] = Field(default=None, description="per-finger grip force if stated")
    material: Optional[str] = Field(default=None, description=f"one of {list(MATERIALS)} if implied")
    max_force_n: float = Field(default=0.0, description="per-finger squeeze force that damages the object; "
                                                         "0 if robust. Estimate for fragile items")
    force_sensor: bool = Field(default=False, description="true if the object is fragile or the user asks for "
                                                           "force sensing / gentle grip / feedback")
    servo: Optional[Literal["sg90", "mg90s", "mg996r", "ds3218"]] = Field(
        default=None, description="servo model if the user names one")
    process: Optional[Literal["fdm_print", "laser_cut", "cnc_router"]] = None
    rationale: str = Field(description="one or two sentences on the assumptions made")


def _client():
    """An Anthropic client, or None (with the reason in `_client.why`) when network lookups are off, the SDK is
    missing, the configured credentials can't be loaded (an ANTHROPIC_PROFILE without its config file: the SDK's
    CredentialsError) or none are configured: no key, auth token or profile, which the SDK would only report as an
    error on the first request."""
    from . import settings

    if not settings.network_allowed():  # Keys, Privacy and network: everything stays on this computer
        _client.why = "network lookups are off"
        return None
    try:
        import anthropic
    except ImportError:
        _client.why = "the Anthropic SDK isn't installed"
        return None
    try:
        client = anthropic.Anthropic()
    except Exception as exc:  # noqa: BLE001  (whatever the SDK raises, the offline parser takes over and says why)
        if exc.__class__.__name__ == "CredentialsError":
            profile = os.environ.get("ANTHROPIC_PROFILE", "").strip()
            what = f"the Anthropic profile {profile!r}" if profile else "the configured Anthropic credentials"
            detail = re.sub(r"\s*\(profile '[^']*'\)$", "", str(exc).split(". ")[0].strip())[:200]
            _client.why = f"{what} couldn't be loaded (CredentialsError{': ' + detail if detail else ''})"
        else:
            _client.why = f"the Anthropic client couldn't start ({exc.__class__.__name__})"
        return None
    if client.api_key is None and client.auth_token is None and getattr(client, "credentials", None) is None:
        _client.why = "no Anthropic key"
        return None
    _client.why = None
    return client


_client.why = None


def _to_design(n: NeedSpec) -> Design:
    d = Design()
    changes = {"name": n.project_name, "object.name": n.object_name, "object.shape": n.shape,
               "object.diameter_mm": n.diameter_mm, "object.length_mm": n.length_mm,
               "object.mass_g": n.mass_g, "object.friction": n.friction}
    if n.gripper_force_n:
        changes["gripper.force_n"] = n.gripper_force_n
    if n.material in MATERIALS:
        changes["finger.material"] = n.material
    if n.process:
        changes["fab.process"] = n.process
    if n.max_force_n:
        changes["object.max_force_n"] = n.max_force_n
    if n.force_sensor:
        changes.update({"finger.sensor": "fsr402", "finger.width_mm": 20.0, "finger.material": "PETG",
                        "fab.process": "fdm_print"})
    if n.servo:
        changes["electronics.servo"] = n.servo
    d = d.with_changes(changes)
    # size the finger so the pad centre reaches the object's centre line
    r = n.diameter_mm / 2
    return d.with_changes({"finger.length_mm": round(max(50.0, r + 30.0), 1),
                           "finger.pad_height_mm": round(min(max(r * 0.9, 15.0), 40.0), 1),
                           "finger.pad_depth_mm": round(min(max(r * 0.25, 3.0), 12.0), 1)})


def heuristic(need: str) -> NeedSpec:
    text = need.lower()
    nums_mm = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*mm", text)]
    mass = re.search(r"(\d+(?:\.\d+)?)\s*(kg|g)\b", text)
    mass_g = (float(mass.group(1)) * (1000 if mass.group(2) == "kg" else 1)) if mass else 200.0
    shape = "box" if any(w in text for w in ("box", "block", "cube", "brick")) else (
        "sphere" if any(w in text for w in ("ball", "sphere", "orange", "apple")) else "cylinder")
    material = process = None
    if "laser" in text:
        material, process = "Delrin", "laser_cut"
    elif "cnc" in text or "alumin" in text:
        material, process = "Al6061", "cnc_router"
    elif "print" in text:
        material, process = "PETG", "fdm_print"
    friction = 0.35 if any(w in text for w in ("glass", "metal", "can", "smooth", "wet", "egg")) else 0.6
    # rough damage thresholds per finger (estimates; check with a kitchen scale)
    fragile = {"egg": 25.0, "strawberry": 3.0, "tomato": 8.0, "chip": 1.0, "paper cup": 4.0, "grape": 2.0,
               "bulb": 15.0, "banana": 6.0}
    max_force = next((v for k, v in fragile.items() if k in text), 0.0)
    sensor = bool(max_force) or any(w in text for w in ("sensor", "force feedback", "gentle", "electronics"))
    servo = next((k for k in ("sg90", "mg90s", "mg996r", "ds3218") if k in text.replace(" ", "")), None)
    obj = re.search(r"(?:pick up|grab|grip|grasp|hold)\s+(?:an?\s+|the\s+)?([a-z0-9 .\-]+?)(?:,|\(|\.\s|\s+(?:that|which|with|weighing|of|about|lying|standing|on|in)\b|$)", text)
    name = re.sub(r"\d+(?:\.\d+)?\s*(?:mm|cm|kg|g)\b", "", obj.group(1)).strip() if obj else "object"
    name = re.sub(r"\s+", " ", name) or "object"
    return NeedSpec(project_name=re.sub(r"[^a-z0-9]+", "-", name)[:24].strip("-") or "project",
                    object_name=name, shape=shape,
                    diameter_mm=nums_mm[0] if nums_mm else 60.0,
                    length_mm=nums_mm[1] if len(nums_mm) > 1 else 100.0,
                    mass_g=mass_g, friction=friction, material=material, process=process,
                    max_force_n=max_force, force_sensor=sensor, servo=servo,
                    rationale="parsed offline (no Claude credentials): numbers and keywords only")


def propose(need: str) -> tuple[Design, NeedSpec, str]:
    client = _client()
    if client is not None:
        try:
            resp = client.messages.parse(
                model=MODEL,
                max_tokens=16000,
                output_config={"effort": "medium"},
                system=("You turn a robotics engineer's request into a grasp-design spec for custom "
                        "parallel-gripper fingers, optionally with a force sensor and servo electronics. "
                        "Use stated numbers exactly; estimate anything missing (including how much squeeze "
                        "damages a fragile object) from typical real-world values and say so in the rationale."),
                messages=[{"role": "user", "content": need}],
                output_format=NeedSpec,
            )
            if resp.stop_reason != "refusal" and resp.parsed_output is not None:
                return _to_design(resp.parsed_output), resp.parsed_output, "claude"
        except Exception as exc:
            print(f"  (Claude unavailable: {exc.__class__.__name__}; using the offline parser)")
    else:
        print(f"  ({_client.why or 'Claude unavailable'}: using the offline parser)")
    spec = heuristic(need)
    return _to_design(spec), spec, "heuristic"


def research(query: str, max_searches: int = 5) -> str:
    """Web research for components, tooling or suppliers, returned with source URLs."""
    client = _client()
    if client is None:
        return ("Research needs Claude credentials (ANTHROPIC_API_KEY or `ant auth login`) and network lookups "
                "turned on (Keys, Privacy and network).")
    messages = [{"role": "user", "content": (
        "Research this for a robotics hardware build and answer concisely with specific part numbers, "
        "specs, prices and links where available:\n\n" + query)}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    for _ in range(4):
        resp = client.messages.create(model=MODEL, max_tokens=16000, tools=tools, messages=messages,
                                      output_config={"effort": "medium"})
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    if resp.stop_reason == "refusal":
        return "Request declined by the model."
    text, sources = [], []
    for block in resp.content:
        if block.type == "text":
            text.append(block.text)
            for c in getattr(block, "citations", None) or []:
                url = getattr(c, "url", None)
                if url and url not in sources:
                    sources.append(url)
    out = "".join(text).strip()
    if sources:
        out += "\n\nSources:\n" + "\n".join(f"- {u}" for u in sources)
    return out
