from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .constants import EFFORTS, PROFILE_LEVELS
from .paths import PLUGIN_ROOT
from .yaml_compat import js_string, parse_yaml

EFFORT_ORDER = EFFORTS
MODEL_EFFORT_SUPPORT = {family: set(EFFORTS) for family in ("opus", "fable", "sonnet")}
MODEL_EFFORT_SUPPORT["haiku"] = set()
MODEL_EFFORT_CEILING = {"opus": "high", "claude-opus-5": "high", "claude-opus-4-8": "xhigh"}
LOW_PROFILE_OPUS_MODEL = "claude-opus-4-8"
MODEL_TIER_NEXT = {"fable": "opus", "opus": "sonnet", "sonnet": "haiku", "haiku": "sonnet"}
RETIRED_MODELS = {
    "claude-3-opus-20240229": ("opus", "retired"),
    "claude-3-sonnet-20240229": ("sonnet", "retired"),
    "claude-3-5-sonnet-20240620": ("sonnet", "retired"),
    "claude-3-5-sonnet-20241022": ("sonnet", "retired"),
    "claude-3-7-sonnet-20250219": ("sonnet", "retired"),
    "claude-3-haiku-20240307": ("haiku", "retired"),
    "claude-3-5-haiku-20241022": ("haiku", "retired"),
    "claude-opus-4-20250514": ("opus", "deprecated"),
    "claude-sonnet-4-20250514": ("sonnet", "deprecated"),
    "claude-opus-4-1-20250805": ("opus", "deprecated"),
}
DEFAULT_ROSTER_DIR = PLUGIN_ROOT / "agents"


class ResolveError(ValueError):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def model_family(model: str) -> str:
    value = model.strip().lower()
    if not value or value == "inherit":
        return "inherit"
    for family in ("opus", "sonnet", "haiku", "fable"):
        if f"claude-{family}" in value or value.startswith(family):
            return family
    return ""


def downmap_effort(
    family: str, effort: str, support: Mapping[str, set[str]] | None = None
) -> tuple[str, bool]:
    eff = effort.strip().lower()
    supported = (MODEL_EFFORT_SUPPORT if support is None else support).get(family)
    if not eff or supported is None:
        return eff, False
    if not supported:
        return "", True
    if eff in supported or eff not in EFFORTS:
        return eff, False
    for candidate in reversed(EFFORTS[: EFFORTS.index(eff) + 1]):
        if candidate in supported:
            return candidate, True
    return "", True


def effort_ceilings(env: Mapping[str, str] | None = None) -> dict[str, str]:
    table = dict(MODEL_EFFORT_CEILING)
    raw = (os.environ if env is None else env).get("WISE_EFFORT_CEILING", "").strip()
    if raw.lower() == "off":
        return {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        key, level = (part.strip().lower() for part in pair.split("=", 1))
        if not key:
            continue
        if level in ("off", "none", ""):
            table.pop(key, None)
        elif level in EFFORTS:
            table[key] = level
    return table


def is_snapshot_of(model: str, key: str) -> bool:
    return (
        model.startswith(key + "-") and re.fullmatch(r"[0-9]{8}", model[len(key) + 1 :]) is not None
    )


def effort_ceiling(model: str, env: Mapping[str, str] | None = None) -> str:
    value = model.strip().lower()
    if not value or value == "inherit":
        return ""
    table = effort_ceilings(env)
    if value in table:
        return table[value]
    matches = [key for key in table if key.startswith("claude-") and is_snapshot_of(value, key)]
    return table[max(matches, key=len)] if matches else ""


def cap_effort(model: str, effort: str, env: Mapping[str, str] | None = None) -> tuple[str, bool]:
    eff = effort.strip().lower()
    ceiling = effort_ceiling(model, env)
    if eff not in EFFORTS or ceiling not in EFFORTS or EFFORTS.index(eff) <= EFFORTS.index(ceiling):
        return effort, False
    return ceiling, True


def low_profile_model(model: str, family: str) -> str:
    value = model.strip().lower()
    if (
        family != "opus"
        or value == LOW_PROFILE_OPUS_MODEL
        or is_snapshot_of(value, LOW_PROFILE_OPUS_MODEL)
    ):
        return ""
    return LOW_PROFILE_OPUS_MODEL


def resolve_model_dict(
    pinned: str, effort: str = "", profile: str = "", opts: dict[str, Any] | None = None
) -> dict[str, Any]:
    options = opts or {}
    pin, eff = pinned.strip(), effort.strip()
    model = pin or "inherit"
    reasons = []
    fell_back = False
    if pin in RETIRED_MODELS:
        model, state = RETIRED_MODELS[pin]
        reasons.append(f"{pin} is {state}; using {model}")
        fell_back = True
    family = model_family(model)
    if profile.strip().lower() == "low":
        low = low_profile_model(model, family)
        if low:
            reasons.append(f"low profile: {model}→{low} (Opus 5 is never used at low)")
            model = low
    eff_out, changed = downmap_effort(family, eff, options.get("effort_support"))
    if changed:
        reasons.append(
            f"{model} has no effort control; effort '{eff}' dropped"
            if not eff_out
            else f"effort {eff}→{eff_out} ({model} capability ceiling)"
        )
    if eff_out:
        capped, lowered = cap_effort(model, eff_out, options.get("env"))
        if lowered:
            reasons.append(f"effort {eff_out}→{capped} ({model} policy ceiling)")
            eff_out = capped
    result = {
        "harness": options.get("harness", "claude"),
        "model": model,
        "effort": eff_out,
        "fell_back": fell_back,
    }
    if reasons:
        result["reason"] = "; ".join(reasons)
    if family in MODEL_TIER_NEXT:
        result["next_fallback"] = MODEL_TIER_NEXT[family]
    return result


def cmd_resolve_model(
    pinned: str, effort: str = "", profile: str = "", opts: dict[str, Any] | None = None
) -> dict[str, Any]:
    level = profile.strip().lower()
    if level and level not in PROFILE_LEVELS:
        raise ResolveError(f"INVALID:profile-level:{level}", 2)
    return resolve_model_dict(pinned, effort, level, opts)


def effort_for(harness: str, effort: str) -> str | None:
    return None if harness in ("cursor", "gemini") else effort


def parse_frontmatter(path: str | Path) -> dict[str, Any]:
    try:
        text = Path(path).read_bytes().decode("utf-8", errors="replace")
        if not text.startswith("---\n"):
            return {}
        end = text.find("\n---", 4)
        if end == -1:
            return {}
        data = parse_yaml(text[4:end])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return True if isinstance(value, (list, dict)) else bool(value)


def _str(value: Any) -> str:
    return js_string(value).strip() if _truthy(value) else ""


def roster_agents(roster_dir: str | Path = DEFAULT_ROSTER_DIR) -> list[dict[str, Any]]:
    try:
        files = sorted(path for path in Path(roster_dir).iterdir() if path.name.endswith(".md"))
    except OSError:
        return []
    items = []
    for path in files:
        fm = parse_frontmatter(path)
        tools = fm.get("tools")
        if isinstance(tools, str):
            tools = [part.strip() for part in tools.split(",") if part.strip()]
        description = fm.get("description")
        items.append(
            {
                "name": js_string(fm["name"]) if _truthy(fm.get("name")) else path.stem,
                "description": (description.strip() or None)
                if isinstance(description, str)
                else None,
                "tools": [js_string(tool) for tool in tools] if isinstance(tools, list) else [],
                "model": js_string(fm["model"]) if _truthy(fm.get("model")) else "inherit",
                "effort": None if fm.get("effort") is None else js_string(fm["effort"]),
            }
        )
    return items


def cmd_list_agents(roster_dir: str | Path = DEFAULT_ROSTER_DIR) -> list[dict[str, Any]]:
    return roster_agents(roster_dir)


def roster_names(roster_dir: str | Path = DEFAULT_ROSTER_DIR) -> set[str]:
    return {agent["name"] for agent in roster_agents(roster_dir)}


def normalize_member(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {"role": item.strip(), "lead": False, "model": "", "effort": ""}
    if isinstance(item, dict):
        return {
            "role": _str(item.get("role")),
            "lead": _truthy(item.get("lead")),
            "model": _str(item.get("model")),
            "effort": _str(item.get("effort")),
        }
    return {"role": "", "lead": False, "model": "", "effort": ""}


def resolve_team(step: dict[str, Any], opts: dict[str, Any] | None = None) -> dict[str, Any]:
    options = opts or {}
    model_override = options.get("model_override", "")
    effort_override = options.get("effort_override", "")
    team_mode = options.get("team_mode", "full")
    raw = step.get("agent")
    errors = []
    profile = options.get("profile", "").strip().lower()
    if profile and profile not in PROFILE_LEVELS:
        errors.append(f"--profile: unknown value '{profile}' (low|medium|max)")
        profile = ""
    mode, items = "unset", []
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        pass
    elif isinstance(raw, bool):
        if raw:
            errors.append(
                "agent: `on`/`yes`/`true` is not valid (use a role, a list, `auto`, or `off`)"
            )
        else:
            mode = "off"
    elif isinstance(raw, str):
        keyword = raw.strip().lower()
        if keyword in ("auto", "off"):
            mode = keyword
        else:
            mode, items = "single", [raw]
    elif isinstance(raw, list):
        mode, items = "team", raw
    else:
        kind = "number" if isinstance(raw, (int, float)) else "object"
        errors.append(f"agent: unexpected type {kind}")
    members = [normalize_member(item) for item in items]
    if mode == "team" and len(members) == 1:
        mode = "single"
    roster = roster_names(options.get("roster_dir", DEFAULT_ROSTER_DIR))
    lead = None
    out_members = []
    for member in members:
        role = member["role"]
        if not role:
            errors.append("agent: list item missing a role")
            continue
        if role in ("auto", "off"):
            errors.append(f"'{role}' is a policy keyword; not valid as a team member")
        elif roster and role not in roster:
            errors.append(f"unknown role '{role}' (not in roster)")
        if member["lead"]:
            if lead:
                errors.append(f"multiple leads ({lead}, {role}); only one allowed")
            else:
                lead = role
        resolved = resolve_model_dict(
            model_override or member["model"] or _str(step.get("model")),
            effort_override or member["effort"] or _str(step.get("effort")),
            profile,
            options,
        )
        resolved.update(role=role, lead=member["lead"])
        if model_override or effort_override:
            resolved["reason"] = "run tuning override" + (
                "; " + resolved["reason"] if resolved.get("reason") else ""
            )
        out_members.append(resolved)
    result = {"mode": mode, "lead": lead, "members": out_members, "errors": errors}
    if team_mode not in ("full", "solo"):
        errors.append(f"--team-mode: unknown value '{team_mode}' (full|solo)")
    elif team_mode == "solo" and mode == "team" and out_members:
        keep = next((member for member in out_members if member["lead"]), out_members[0])
        dropped = [member["role"] for member in out_members if member is not keep]
        note = "team collapsed to lead (solo mode)"
        if not keep["lead"]:
            note += "; no declared lead - first member kept"
        keep["reason"] = (keep["reason"] + "; " if keep.get("reason") else "") + note
        result.update(
            mode="single",
            lead=keep["role"] if keep["lead"] else None,
            members=[keep],
            collapsed={"from": len(out_members), "dropped": dropped},
        )
    return result


def cmd_resolve_team(
    def_path: str | Path, step_id: str, opts: dict[str, Any] | None = None
) -> dict[str, Any]:
    definition = parse_yaml(Path(def_path).read_text())
    steps = definition.get("steps", []) if isinstance(definition, dict) else []
    if not isinstance(steps, list):
        steps = []
    step = next(
        (item for item in steps if isinstance(item, dict) and item.get("id") == step_id), {}
    )
    return resolve_team(step, opts)
