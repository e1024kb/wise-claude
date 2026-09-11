from __future__ import annotations

import copy
import io
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import (
    DoubleQuotedScalarString,
    FoldedScalarString,
    LiteralScalarString,
)

from .yaml_compat import MISSING, js_items, js_json, js_string, parse_yaml

Rec = dict[str, Any]
V1_STEP_TYPES = dict.fromkeys(("prompt", "interactive", "supervised-prompt", "skill"), "agent")
V2_STEP_KEYS = set(
    "id type group depends_on trigger-rule when description optional harness model effort auth fallback mode resume max_turns timeout stale_after allowed_tools prompt skill schema outputs until run message options allow_text output pipeline items groups caps parallel reviewers".split()
)
GROUP_KEYS = set("id label description default fallback locked options".split())
INPUT_KEYS = set("name prompt description optional default from-context validate extract".split())
FLOW_SEQ_KEYS = set(
    "outputs required enum allowed_tools options fallback plugins tools optional caps reviewers".split()
)
_ENUM_GROUP = re.compile(
    r"\^?([A-Za-z0-9_ :=-]*?)\((?:\?:)?([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)*)\)([A-Za-z0-9_ :=-]*?)\$?\Z"
)
_ENUM_BARE = re.compile(r"\^?([A-Za-z0-9_-]+(?:\|[A-Za-z0-9_-]+)+)\$?\Z")
_YAML11_WORDS = re.compile(r"(?:y|n|yes|no|on|off|true|false|null|~)\Z", re.I)


class MigrationError(ValueError):
    def __init__(self, message: str, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


class _Notes:
    def __init__(self) -> None:
        self.list: list[Rec] = []

    def add(self, path: str, kind: str, message: str) -> None:
        self.list.append(dict(path=path, kind=kind, message=message))

    def rewritten(self, path: str, message: str) -> None:
        self.add(path, "rewritten", message)

    def warning(self, path: str, message: str) -> None:
        self.add(path, "warning", message)

    def manual(self, path: str, message: str) -> None:
        self.add(path, "manual", message)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def enum_from_until(until: str) -> list[str] | None:
    group = _ENUM_GROUP.fullmatch(until)
    bare = _ENUM_BARE.fullmatch(until)
    text = group[2] if group else bare[1] if bare else None
    return list(dict.fromkeys(text.split("|"))) if text is not None else None


def _parse_tuning(raw: str) -> Rec | None:
    parts = [part.strip() for part in raw.split("/")]
    if not parts[0]:
        return None
    out = {"model": parts[0]}
    if len(parts) > 1 and parts[1]:
        out["effort"] = parts[1]
    return out


def _tuning_mapping(parsed: Rec, with_harness: bool) -> Rec:
    return ({"harness": "claude"} if with_harness else {}) | parsed


def _requires(value: Any, notes: _Notes) -> Any:
    if not isinstance(value, list):
        return value
    plugins = []
    for i, entry in enumerate(value):
        if isinstance(entry, dict) and isinstance(entry.get("plugin"), str):
            plugin = entry["plugin"]
        elif isinstance(entry, dict) and isinstance(entry.get("skill"), str):
            plugin = entry["skill"].split(":", 1)[0]
        else:
            notes.warning(f"requires[{i}]", f"unrecognised entry {js_json(entry)} dropped")
            continue
        if plugin not in plugins:
            plugins.append(plugin)
    notes.rewritten(
        "requires",
        f"list of {{ plugin }} / {{ skill }} entries -> {{ plugins: [{', '.join(plugins)}] }}",
    )
    return {"plugins": plugins}


def _preflight(value: Any, notes: _Notes) -> Any:
    if not isinstance(value, dict):
        notes.warning("preflight", "not a mapping, kept as is")
        return value
    out: Rec = {}
    for key, item in js_items(value):
        p = f"preflight.{key}"
        if key in ("rename_session", "tuning", "step-select"):
            notes.rewritten(
                p,
                f"dropped `{key}: {js_json(item)}`; v2 always builds the questionary from tuning / step-select and the harness names sessions",
            )
        elif key == "control-mode":
            if item in ("wave-sync", "auto-advance", "prompt"):
                out[key] = "interactive"
                notes.rewritten(p, f'{js_json(item)} -> "interactive" (gates pause the run)')
            else:
                out[key] = item
        elif key == "worktree":
            if item == "prompt":
                out[key] = "current"
                notes.warning(
                    p,
                    '"prompt" -> "current"; v2 does not ask, change to "new" if the workflow should edit a throwaway tree',
                )
            else:
                out[key] = item
        else:
            out[key] = item
            notes.warning(p, f"unknown pre-flight key `{key}` kept as is")
    return out if out else MISSING


def _group_default(value: Any, p: str, notes: _Notes) -> Rec | None:
    if isinstance(value, str):
        parsed = _parse_tuning(value)
        if parsed is None:
            notes.warning(p, f"unparseable tuning string {js_json(value)} dropped")
            return None
        out = _tuning_mapping(parsed, True)
        notes.rewritten(p, f"{js_json(value)} -> {js_json(out)}")
        return out
    if isinstance(value, dict):
        if "harness" in value:
            return value
        notes.rewritten(p, "added `harness: claude`")
        return {"harness": "claude", **value}
    notes.warning(p, f"unrecognised default {js_json(value)} dropped")
    return None


def _tuning(raw: Any, steps: list[Any], notes: _Notes) -> tuple[Any, Rec, Rec]:
    binding: Rec = {}
    defaults: Rec = {}
    if not isinstance(raw, dict) or not isinstance(raw.get("groups"), list):
        notes.warning("tuning", "expected a mapping with `groups:`; kept as is")
        return raw, binding, defaults
    groups = []
    for i, group in enumerate(raw["groups"]):
        p = f"tuning.groups[{i}]"
        if not isinstance(group, dict):
            notes.warning(p, "not a mapping, kept as is")
            groups.append(group)
            continue
        ident = group["id"] if isinstance(group.get("id"), str) else ""
        rest = {}
        default = None
        bound = []
        for key, value in js_items(group):
            if key == "default":
                default = _group_default(value, p + ".default", notes)
            elif key == "steps":
                if _strings(value):
                    bound = value
                    binding.update(dict.fromkeys(value, ident))
                    notes.rewritten(
                        p + ".steps",
                        f"dropped steps: [{', '.join(value)}]; set `group: {ident}` on those steps",
                    )
                else:
                    notes.warning(
                        p + ".steps", f"unrecognised steps binding {js_json(value)} dropped"
                    )
            else:
                rest[key] = value
                if key not in GROUP_KEYS:
                    notes.warning(p + "." + key, f"unknown group key `{key}` kept as is")
        if default is None:
            source = None
            for sid in bound:
                step = next((s for s in steps if isinstance(s, dict) and s.get("id") == sid), None)
                if (
                    step is not None
                    and isinstance(step.get("model"), str)
                    and step["model"] != "inherit"
                ):
                    source = step
                    break
            if source is not None:
                parsed = {"model": source["model"]}
                if isinstance(source.get("effort"), str):
                    parsed["effort"] = source["effort"]
                default = _tuning_mapping(parsed, True)
                notes.rewritten(
                    p + ".default",
                    f"derived {js_json(default)} from step {js_json(source.get('id', MISSING))}'s pins",
                )
            else:
                default = {"harness": "claude"}
                notes.manual(
                    p + ".default", "no default and no bound step pins a model; set model / effort"
                )
        defaults[ident] = default
        ordered = {key: rest[key] for key in ("id", "label", "description") if key in rest}
        ordered["default"] = default
        ordered.update({key: value for key, value in rest.items() if key not in ordered})
        groups.append(ordered)
    value = {}
    for key, item in js_items(raw):
        value[key] = groups if key == "groups" else item
        if key != "groups":
            notes.warning("tuning." + key, f"unknown tuning key `{key}` kept as is")
    return value, binding, defaults


def _profiles(raw: Any, notes: _Notes) -> Any:
    if not isinstance(raw, dict):
        notes.warning("profiles", "not a mapping, kept as is")
        return raw
    out = {}
    for level, entry_raw in js_items(raw):
        p = f"profiles.{level}"
        entry = {} if entry_raw is None else entry_raw
        if not isinstance(entry, dict):
            out[level] = entry_raw
            notes.warning(p, "not a mapping, kept as is")
            continue
        profile: Rec = {}
        for key, value in js_items(entry):
            if key == "tuning":
                if not isinstance(value, dict):
                    profile[key] = value
                    notes.warning(p + ".tuning", "not a mapping, kept as is")
                    continue
                tuning = {}
                for gid, pin in js_items(value):
                    tp = f"{p}.tuning.{gid}"
                    if pin == "default":
                        notes.rewritten(
                            tp, 'dropped "default" (omitting the group keeps its default)'
                        )
                    elif isinstance(pin, str):
                        parsed = _parse_tuning(pin)
                        if parsed:
                            tuning[gid] = _tuning_mapping(parsed, False)
                            notes.rewritten(tp, f"{js_json(pin)} -> {js_json(tuning[gid])}")
                        else:
                            notes.warning(tp, f"unparseable tuning string {js_json(pin)} dropped")
                    else:
                        tuning[gid] = pin
                if tuning:
                    profile[key] = tuning
            elif key in ("step-preset", "skip"):
                notes.warning(
                    p + "." + key,
                    f"dropped {key}: {js_json(value)}; v2 has no presets, optional steps are chosen in the step-select question",
                )
            elif key == "team-mode":
                notes.rewritten(p + ".team-mode", "dropped team-mode; v2 has no agent teams")
            else:
                profile[key] = value
                if key not in ("caps", "description"):
                    notes.warning(p + "." + key, f"unknown profile key `{key}` kept as is")
        out[level] = profile
    return out


def _inputs(raw: Any, notes: _Notes) -> Any:
    if not isinstance(raw, list):
        notes.warning("inputs", "not a list, kept as is")
        return raw
    entries = []
    for i, entry in enumerate(raw):
        p = f"inputs[{i}]"
        if not isinstance(entry, dict):
            notes.warning(p, "not a mapping, kept as is")
            entries.append(entry)
            continue
        name = entry["name"] if isinstance(entry.get("name"), str) else ""
        options = None
        if isinstance(entry.get("options"), list):
            options = []
            for item in entry["options"]:
                value = js_string(
                    (item.get("value") if item.get("value") is not None else "")
                    if isinstance(item, dict)
                    else item
                )
                label = item.get("label", "") if isinstance(item, dict) else ""
                if value:
                    options.append((value, label if isinstance(label, str) else ""))
        out = {}
        for key, value in js_items(entry):
            if key == "options":
                continue
            if key == "prompt" and options is not None and isinstance(value, str):
                menu = " | ".join(f"{v}: {label}" if label else v for v, label in options)
                out[key] = f"{value} ({menu})"
                continue
            out[key] = value
            if key not in INPUT_KEYS:
                notes.warning(p + "." + key, f"unknown input key `{key}` kept as is")
        if options is not None:
            if "validate" not in out:
                escaped = [re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", v) for v, _ in options]
                out["validate"] = "^(" + "|".join(escaped) + ")$"
            notes.rewritten(
                p + ".options",
                f"choice input {js_json(name)} -> validate {js_json(out['validate'])}; option labels folded into the prompt",
            )
        if "from-context" not in out:
            context = (
                "ticket[].ref"
                if re.fullmatch(r"ticket(_ids?|s)?", name)
                else "guidance"
                if name in ("config_prompt", "guidance")
                else None
            )
            if context:
                out["from-context"] = context
                notes.rewritten(
                    p + ".from-context",
                    f"added from-context: {context} (pre-filled from the run context)",
                )
        entries.append(out)
    return entries


def _step_select(raw: Any, notes: _Notes) -> tuple[Any, Rec]:
    labels: Rec = {}
    if not isinstance(raw, dict):
        notes.warning("step-select", "not a mapping, kept as is")
        return raw, labels
    out = {}
    for key, value in js_items(raw):
        if key == "prompt":
            out[key] = value
        elif key == "optional":
            if not isinstance(value, list):
                out[key] = value
                notes.warning("step-select.optional", "not a list, kept as is")
                continue
            ids = []
            for i, entry in enumerate(value):
                p = f"step-select.optional[{i}]"
                if isinstance(entry, str):
                    if entry not in ids:
                        ids.append(entry)
                    continue
                if not isinstance(entry, dict):
                    notes.warning(p, f"unrecognised entry {js_json(entry)} dropped")
                    continue
                ident = entry.get("id") if isinstance(entry.get("id"), str) else ""
                steps = (
                    entry["steps"] if _strings(entry.get("steps")) and entry["steps"] else [ident]
                )
                for sid in steps:
                    if sid not in ids:
                        ids.append(sid)
                    if isinstance(entry.get("label"), str):
                        labels.setdefault(sid, entry["label"])
                notes.rewritten(
                    p,
                    f"entry {js_json(ident)} -> step ids [{', '.join(steps)}]; its label becomes those steps' description",
                )
                if len(steps) > 1:
                    notes.manual(
                        p,
                        f"entry {js_json(ident)} covered {len(steps)} steps; v2 offers each step separately, review whether all of them should be selectable",
                    )
            out[key] = ids
        elif key == "presets":
            notes.warning(
                "step-select.presets", "dropped presets; v2 asks one multi-select over `optional:`"
            )
        else:
            out[key] = value
            notes.warning("step-select." + key, f"unknown step-select key `{key}` kept as is")
    return out, labels


def _role_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    role = value.strip().removeprefix("wise:")
    return role if role and role not in ("auto", "off") else None


def _role_prefix(lead: str, others: list[str]) -> str:
    files = [f"${{CLAUDE_PLUGIN_ROOT}}/agents/{lead}.md", *[f"{role}.md" for role in others]]
    if not others:
        return f"Act as the wise `{lead}` agent (see {files[0]})."
    lenses = ", ".join(f"`{role}`" for role in others)
    return f"Act as the wise `{lead}` agent leading this step and cover the {lenses} {'lenses' if len(others) > 1 else 'lens'} too (see {', '.join(files)})."


def _agent_prefix(agent: Any, p: str, notes: _Notes) -> str | None:
    if agent is MISSING:
        return None
    if agent is False or agent in ("off", "auto"):
        notes.rewritten(
            p + ".agent", f"dropped `agent: {js_string(agent)}`; v2 has no roster routing"
        )
        return None
    if isinstance(agent, str):
        role = _role_name(agent)
        if not role:
            notes.warning(p + ".agent", f"unrecognised agent {js_json(agent)} dropped")
            return None
        notes.rewritten(
            p + ".agent",
            f'role {js_json(role)} folded into the prompt ("Act as the wise {role} agent")',
        )
        return _role_prefix(role, [])
    if isinstance(agent, list):
        members: list[Rec] = []
        for item in agent:
            role = _role_name(item.get("role") if isinstance(item, dict) else item)
            if role:
                members.append(
                    {"role": role, "lead": isinstance(item, dict) and item.get("lead") is True}
                )
        if not members:
            notes.warning(p + ".agent", "team with no recognisable roles dropped")
            return None
        lead = next((member for member in members if member["lead"]), members[0])
        others = [m["role"] for m in members if m is not lead]
        notes.manual(
            p + ".agent",
            f"team [{', '.join(m['role'] for m in members)}] folded into one agent led by {lead['role']}; v2 has no agent teams, review the prompt (member model / effort overrides dropped)",
        )
        return _role_prefix(lead["role"], others)
    notes.warning(p + ".agent", f"unrecognised agent binding {js_json(agent)} dropped")
    return None


def _enum_info(step: Rec, p: str, ident: str, notes: _Notes) -> Rec | None:
    if not isinstance(step.get("until"), str):
        return None
    values = enum_from_until(step["until"])
    outputs = step["outputs"] if _strings(step.get("outputs")) else []
    if values is None or len(outputs) > 1:
        props = (
            ", ".join(f"{o}: {{ type: string }}" for o in outputs)
            if outputs
            else "<name>: { type: string }"
        )
        hint = f"schema: {{ type: object, properties: {{ {props} }}, required: [{', '.join(outputs)}] }} (keep outputs: as the names to copy)"
        notes.warning(
            p + ".until",
            f"kept `until:` {js_json(step['until'])} (deprecated regex capture, not a plain enum); replace it with {hint} and rewrite the prompt to return the structured result",
        )
        return None
    name = outputs[0] if outputs else ident.replace("-", "_")
    notes.rewritten(
        p + ".until",
        f"enum regex {js_json(step['until'])} -> schema {{ {name}: enum [{', '.join(values)}] }} + outputs [{name}]; the prompt's verdict-line wording is now stale",
    )
    return {"name": name, "values": values}


def _compose_prompt(prompt: str, prefix: str | None, info: Rec | None) -> str:
    out = f"{prefix}\n\n{prompt}" if prefix is not None else prompt
    if info:
        newline = "\n" if out.endswith("\n") else ""
        out = f"{out.rstrip()}\n\nReturn the field directly as the structured result (no wrapping, no JSON-in-a-string): `{info['name']}` = one of {' | '.join(info['values'])}.{newline}"
    return out


def _empty(value: Any) -> bool:
    return (
        value is MISSING
        or value is None
        or value == ""
        or isinstance(value, (list, dict))
        and not value
    )


def _step(step: Rec, i: int, binding: Rec, defaults: Rec, labels: Rec, notes: _Notes) -> Rec:
    p = f"steps[{i}]"
    ident = step["id"] if isinstance(step.get("id"), str) else ""
    old_type = step["type"] if isinstance(step.get("type"), str) else ""
    kind = V1_STEP_TYPES.get(old_type, old_type)
    agent = kind == "agent"
    group = binding.get(ident, MISSING)
    default = defaults.get(group) if group is not MISSING else None
    prefix = _agent_prefix(step.get("agent", MISSING), p, notes) if agent else None
    info = _enum_info(step, p, ident, notes) if agent else None
    skill = (
        step["skill"].strip().removeprefix("/") if isinstance(step.get("skill"), str) else MISSING
    )
    payload = step.get("payload", MISSING)
    payload_prompt = (
        f"Run /{skill} with: {payload if isinstance(payload, str) else js_json(payload)}"
        if skill is not MISSING and not _empty(payload)
        else MISSING
    )
    run = (
        step["command"]
        if isinstance(step.get("command"), str)
        else step["run"]
        if isinstance(step.get("run"), str)
        else MISSING
    )
    if (
        run is not MISSING
        and isinstance(step.get("cwd"), str)
        and step["cwd"].strip() != "{{project.path}}"
    ):
        run = f'cd "{step["cwd"]}" || exit 1\n{run}'
    out: Rec = {}
    for key, value in js_items(step):
        kp = p + "." + key
        if key == "type":
            out[key] = kind
            if kind != old_type:
                notes.rewritten(kp, f'type {js_json(old_type)} -> "agent"')
            if group is not MISSING and "group" not in step:
                out["group"] = group
                notes.rewritten(
                    p + ".group",
                    f"bound to tuning group {js_json(group)} (was the group's steps: list)",
                )
        elif key == "prompt":
            out[key] = (
                _compose_prompt(value, prefix, info) if agent and isinstance(value, str) else value
            )
        elif key == "skill":
            if payload_prompt is not MISSING:
                out.update(prompt=payload_prompt, harness="claude")
                notes.rewritten(
                    kp,
                    f"skill {js_json(skill)} + payload -> prompt {js_json(payload_prompt)} (harness claude)",
                )
            else:
                if skill is not MISSING:
                    out[key] = skill
                notes.rewritten(
                    kp,
                    f'skill {js_json(skill)} kept as `skill:` sugar (emits "Run /{js_string(skill)}", harness claude)',
                )
        elif key == "payload":
            if _empty(value):
                notes.rewritten(kp, "dropped empty payload")
        elif key == "until":
            if info:
                name = info["name"]
                out["schema"] = {
                    "type": "object",
                    "properties": {name: {"type": "string", "enum": info["values"]}},
                    "required": [name],
                    "additionalProperties": False,
                }
                if not _strings(step.get("outputs")):
                    out["outputs"] = [name]
            else:
                out[key] = value
        elif key == "max_iterations":
            if (
                type(value) in (int, float)
                and float(value).is_integer()
                and 1 <= value <= 10
                and "max_turns" not in step
            ):
                out["max_turns"] = value
                notes.rewritten(
                    kp,
                    f"max_iterations {js_string(value)} -> max_turns {js_string(value)} (turns of one child, not whole-step retries; raise it when the step uses tools)",
                )
            else:
                notes.warning(
                    kp,
                    f"dropped max_iterations {js_json(value)}; set `max_turns:` if the step needs a turn cap",
                )
        elif key == "agent":
            pass
        elif key in ("command", "run"):
            if run is not MISSING:
                out["run"] = run
            if key == "command":
                notes.rewritten(kp, "command -> run")
        elif key == "success":
            success = value if isinstance(value, dict) else {}
            lost = [
                k
                for k, v in success.items()
                if not (k == "exit_code" and type(v) in (int, float) and v == 0)
            ]
            if lost:
                notes.warning(
                    kp,
                    f"dropped success: {{ {', '.join(lost)} }}; v2 succeeds on exit code 0, assert the output inside run: or through outputs:",
                )
            else:
                notes.rewritten(kp, "dropped success (exit code 0 is the v2 success)")
        elif key == "cwd":
            notes.rewritten(
                kp,
                f"cwd {js_json(value)} -> `cd` at the top of run:"
                if isinstance(value, str) and value.strip() != "{{project.path}}"
                else "dropped cwd (steps run in the project path)",
            )
        elif key == "question":
            out["message"] = value
            notes.rewritten(kp, "question -> message")
        elif key == "header":
            notes.rewritten(kp, "dropped header; the harness renders the gate")
        elif key in ("skip_label", "confirm_label", "confirm_value"):
            if "options" in out:
                continue
            options = [
                step[k] for k in ("skip_label", "confirm_label") if isinstance(step.get(k), str)
            ]
            if options:
                out["options"] = options
            if "confirm_label" not in step:
                out["allow_text"] = True
            notes.rewritten(
                kp,
                f"skip_label / confirm_label -> options: {js_json(options)}"
                + (" + allow_text: true" if "confirm_label" not in step else ""),
            )
            if isinstance(step.get("skip_label"), str):
                output = step.get("output") if isinstance(step.get("output"), str) else "<output>"
                notes.manual(
                    kp,
                    f"skip now records the option text, not ''; update `when:` guards comparing {output} to '' (and any confirm_value match)",
                )
        elif key == "when":
            out[key] = (
                " && ".join(js_string(item) for item in value) if isinstance(value, list) else value
            )
            if isinstance(value, list):
                notes.rewritten(kp, f"list of conditions -> {js_json(out[key])}")
        elif key in ("model", "effort", "harness"):
            if default is not None:
                if default.get(key, MISSING) != value:
                    notes.warning(
                        kp,
                        f"dropped {key}: {js_json(value)}; the tuning group {js_json(group)} carries {js_json(default.get(key, MISSING))}",
                    )
                else:
                    notes.rewritten(
                        kp,
                        f"dropped {key}: {js_json(value)}; carried by tuning group {js_json(group)}",
                    )
            elif key == "model" and value == "inherit":
                notes.rewritten(kp, 'dropped model: "inherit" (the default)')
            else:
                out[key] = value
        elif key == "surface":
            notes.warning(
                kp,
                "dropped surface; v2 has no chat surfacing, the step's output lands in the run log",
            )
        else:
            out[key] = value
            if key not in V2_STEP_KEYS:
                notes.warning(kp, f"unknown step key `{key}` kept as is")
    if ident in labels and "description" not in out:
        out["description"] = labels[ident]
        notes.rewritten(
            p + ".description", f"step-select label {js_json(labels[ident])} -> description"
        )
    if old_type == "interactive":
        notes.manual(
            p + ".type",
            "`interactive` ran inline in the conductor; it is now an isolated agent child: ask the user through the `wise_ask` tool, read context with `wise_context`, add `mode: full-access` for unattended git",
        )
    elif old_type == "supervised-prompt":
        notes.manual(
            p + ".type",
            "`supervised-prompt` hang protection is now the adapter's `timeout:` / `stale_after:`; set them on the step",
        )
    return out


def migrate_def(raw: Any, path: str) -> Rec:
    notes = _Notes()
    if not isinstance(raw, dict):
        notes.warning("", f"{path}: expected a YAML mapping at the top level; nothing migrated")
        return {"def": raw, "notes": notes.list}
    if type(raw.get("version")) in (int, float) and raw["version"] == 2:
        notes.warning("version", "already version 2; nothing to migrate")
        return {"def": raw, "notes": notes.list}
    out: Rec = {"version": 2}
    if "version" not in raw:
        notes.rewritten("version", "added `version: 2`")
    elif type(raw["version"]) in (int, float) and raw["version"] == 1:
        notes.rewritten("version", "version 1 -> 2")
    else:
        notes.warning("version", f"unsupported version {js_json(raw['version'])} treated as v1")
    steps = raw["steps"] if isinstance(raw.get("steps"), list) else []
    tuning, binding, defaults = (
        _tuning(raw["tuning"], steps, notes) if "tuning" in raw else (MISSING, {}, {})
    )
    selection, labels = (
        _step_select(raw["step-select"], notes) if "step-select" in raw else (MISSING, {})
    )
    for key, value in js_items(raw):
        if key == "version":
            continue
        if key in ("name", "description", "author"):
            out[key] = value
        elif key == "project-selection":
            out[key] = "ask" if value == "prompt" else "none" if value == "any" else value
            if value in ("prompt", "any"):
                notes.rewritten(key, f"{js_json(value)} -> {js_json(out[key])}")
        elif key == "agents":
            notes.rewritten(
                key,
                f"dropped workflow-level `agents: {js_json(value)}`; v2 has no roster routing, each agent step carries its own prompt",
            )
        elif key == "requires":
            out[key] = _requires(value, notes)
        elif key == "preflight":
            pf = _preflight(value, notes)
            if pf is not MISSING:
                out[key] = pf
        elif key == "tuning":
            out[key] = tuning
        elif key == "step-select":
            out[key] = selection
        elif key == "profiles":
            out[key] = _profiles(value, notes)
        elif key == "inputs":
            out[key] = _inputs(value, notes)
        elif key == "steps":
            if isinstance(value, list):
                out[key] = []
                for i, item in enumerate(value):
                    if isinstance(item, dict):
                        out[key].append(_step(item, i, binding, defaults, labels, notes))
                    else:
                        notes.warning(f"steps[{i}]", "not a mapping, kept as is")
                        out[key].append(item)
            else:
                out[key] = value
                notes.warning(key, "not a list, kept as is")
        else:
            out[key] = value
            notes.warning(key, f"unknown top-level key `{key}` kept as is")
    return {"def": out, "notes": notes.list}


def _flow_text(value: Any) -> str | None:
    if isinstance(value, dict):
        entries = [(key, _flow_text(item)) for key, item in value.items()]
        if any(text is None for _, text in entries):
            return None
        return "{" + ", ".join(f"{key}: {text}" for key, text in entries) + "}"
    if isinstance(value, list):
        texts = [_flow_text(item) for item in value]
        if any(text is None for text in texts):
            return None
        return "[" + ", ".join(str(text) for text in texts) + "]"
    return js_string(value)


def _styled(value: Any, key: str = "", indent: int = 0, source: Any = None) -> Any:
    if isinstance(value, dict):
        out: Any = CommentedMap()
        if isinstance(source, CommentedMap):
            out.ca.comment = copy.deepcopy(source.ca.comment)
            out.ca.end = copy.deepcopy(source.ca.end)
        renames = {
            "run": "command",
            "message": "question",
            "schema": "until",
            "max_turns": "max_iterations",
        }
        for name, item in value.items():
            old_name = (
                name if isinstance(source, dict) and name in source else renames.get(name, name)
            )
            old = source.get(old_name) if isinstance(source, dict) else None
            out[name] = _styled(item, name, indent + 2, old)
            if isinstance(source, CommentedMap) and old_name in source.ca.items:
                out.ca.items[name] = copy.deepcopy(source.ca.items[old_name])
        scalar_map = all(not isinstance(item, (list, dict)) for item in value.values())
        if key in ("default", "value") and scalar_map:
            text = _flow_text(value)
            if text is not None and max(0, indent - 2) + len(key) + 2 + len(text) <= 100:
                out.fa.set_flow_style()
        if (
            key == "properties"
            or key == "tuning"
            and value
            and all(
                isinstance(v, dict) and all(not isinstance(i, (list, dict)) for i in v.values())
                for v in value.values()
            )
        ):
            for name, item in out.items():
                if isinstance(item, CommentedMap):
                    text = _flow_text(item)
                    if text is not None and indent + len(name) + 2 + len(text) <= 100:
                        item.fa.set_flow_style()
        return out
    if isinstance(value, list):
        out = CommentedSeq(
            [
                _styled(
                    item,
                    "",
                    indent + 2,
                    source[i] if isinstance(source, list) and i < len(source) else None,
                )
                for i, item in enumerate(value)
            ]
        )
        if isinstance(source, CommentedSeq):
            out.ca.comment = copy.deepcopy(source.ca.comment)
            out.ca.end = copy.deepcopy(source.ca.end)
            for index, comment in source.ca.items.items():
                if index < len(out):
                    out.ca.items[index] = copy.deepcopy(comment)
        if all(not isinstance(item, (list, dict)) for item in value) and (
            key in FLOW_SEQ_KEYS or key == "depends_on" and len(value) <= 3
        ):
            text = _flow_text(value)
            if text is not None and max(0, indent - 2) + len(key) + 2 + len(text) <= 100:
                out.fa.set_flow_style()
        return out
    if isinstance(value, str):
        if _YAML11_WORDS.fullmatch(value):
            return DoubleQuotedScalarString(value)
        if "\n" in value:
            return LiteralScalarString(value)
        if len(value) > 100 and " " in value and not re.search(r"^\s|\s$|\s\s", value):
            return FoldedScalarString(value)
    return value


def render_def(definition: Any, source: Any = None) -> str:
    yaml = YAML(typ="rt")
    yaml.width = 100
    yaml.indent(mapping=2, sequence=4, offset=2)
    stream = io.StringIO()
    yaml.dump(_styled(definition, source=source), stream)
    return stream.getvalue()


def _comments(tree: Any) -> list[str]:
    result: list[str] = []
    visited: set[int] = set()
    comment_tokens: set[int] = set()

    def collect(value: Any) -> None:
        if isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
        elif hasattr(value, "value") and isinstance(value.value, str):
            if id(value) in comment_tokens:
                return
            comment_tokens.add(id(value))
            for line in value.value.split("\n"):
                text = line.strip()
                if text.startswith("#"):
                    result.append(text)

    def walk(value: Any) -> None:
        if id(value) in visited:
            return
        visited.add(id(value))
        if isinstance(value, (CommentedMap, CommentedSeq)):
            collect(value.ca.comment)
            collect(value.ca.end)
            for comments in value.ca.items.values():
                collect(comments)
            for item in value.values() if isinstance(value, dict) else value:
                walk(item)

    walk(tree)
    return result


def migrate_file(
    path: str | Path,
    *,
    write: bool = False,
    out: str | Path | None = None,
    workflow: str | None = None,
) -> Rec:
    from .defs import validate_def

    file = Path(path).absolute()
    if file.is_dir():
        if (file / "state.yaml").is_file():
            raise MigrationError(
                "UNSUPPORTED_V1_RUN: legacy state.yaml runs cannot be migrated or resumed; migrate the workflow definition and start a new run after reviewing completed side effects"
            )
        raise MigrationError(f"migrate: workflow not found: {file}")
    try:
        source = file.read_text(encoding="utf-8")
    except OSError as error:
        raise MigrationError(f"migrate: workflow not found: {file}") from error
    raw = parse_yaml(source)
    raw = {} if raw is None else raw
    if (
        file.name == "state.yaml"
        or isinstance(raw, dict)
        and "run_id" in raw
        and isinstance(raw.get("steps"), dict)
    ):
        raise MigrationError(
            "UNSUPPORTED_V1_RUN: legacy state.yaml runs cannot be migrated or resumed; migrate the workflow definition and start a new run after reviewing completed side effects"
        )
    already = (
        isinstance(raw, dict) and type(raw.get("version")) in (int, float) and raw["version"] == 2
    )
    result = migrate_def(raw, str(file))
    definition, notes = result["def"], result["notes"]
    tree = YAML(typ="rt").load(source)
    rendered = render_def(definition, tree)
    if not already:
        remaining = Counter(_comments(YAML(typ="rt").load(rendered)))
        retained = []
        for comment in _comments(tree):
            if remaining[comment]:
                remaining[comment] -= 1
            else:
                retained.append(comment)
        if retained:
            rendered = rendered.rstrip("\n") + "\n\n" + "\n".join(retained) + "\n"
            notes.append(
                {
                    "path": "",
                    "kind": "manual",
                    "message": "comments on removed constructs are retained at the end of the document; review whether they still apply",
                }
            )
    validated = validate_def(definition, str(file))
    written: list[str] = []
    backup = None
    if not already:
        if write:
            backup = str(file) + ".v1.bak"
            if Path(backup).exists():
                notes.append(
                    {"path": "", "kind": "warning", "message": f"kept the existing backup {backup}"}
                )
            else:
                shutil.copyfile(file, backup)
            file.write_text(rendered, encoding="utf-8")
            written.append(str(file))
        if out is not None:
            destination = Path(out).absolute()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered, encoding="utf-8")
            written.append(str(destination))
    return {
        "workflow": workflow or file.stem,
        "path": str(file),
        "already_v2": already,
        "dry_run": not written,
        "ok": validated.get("def") is not None,
        "written": written,
        "backup": backup,
        "notes": notes,
        "issues": validated["issues"],
        "yaml": rendered,
    }


def format_migration(result: Rec) -> str:
    if result["already_v2"]:
        return f"{result['path']}: already v2, nothing to migrate"
    notes = result["notes"]
    count = {
        kind: sum(note["kind"] == kind for note in notes)
        for kind in ("rewritten", "warning", "manual")
    }
    where = (
        "written to "
        + ", ".join(result["written"])
        + (f" (backup {result['backup']})" if result["backup"] else "")
        if result["written"]
        else "dry run, nothing written (use --write or --out)"
    )
    errors = [issue for issue in result["issues"] if issue["level"] == "error"]
    lines = [
        f"{result['path']}: migrated to v2, {count['rewritten']} rewritten, {count['warning']} warning(s), {count['manual']} manual; {where}"
    ]
    lines.extend(f"  {note['kind'].upper()} {note['path']}: {note['message']}" for note in notes)
    lines.append(
        f"  result still has {len(errors)} validation error(s):"
        if errors
        else "  result validates with no errors"
    )
    lines.extend(
        f"  {issue['level'].upper()} {issue['path']}: {issue['message']}"
        + (f"\n    -> {issue['hint']}" if issue.get("hint") else "")
        for issue in errors
    )
    return "\n".join(lines)
