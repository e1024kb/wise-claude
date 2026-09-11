from __future__ import annotations

import json
import math
import os
import re

import regex  # type: ignore[import-untyped]
from collections.abc import Callable, Mapping, Set
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import PLUGIN_ROOT, plugin_data_root
from .yaml_compat import MISSING, js_items, js_json, js_string, parse_yaml, json_value

from .constants import (
    HARNESSES,
    EFFORTS,
    AUTH_MODES,
    RUN_MODES,
    MCP_POLICIES,
    PROFILE_LEVELS,
    STEP_TYPES,
    TRIGGER_RULES,
    PHASES,
)

RESERVED_NAMES = frozenset(("list", "create", "run", "resume", "remove", "status"))
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
CAP_RE = re.compile(r"^[a-z][a-z0-9_]*$")
INPUT_NAME_RE = CAP_RE
FROM_CONTEXT_RE = re.compile(
    r"^(guidance|ticket\[\]\.(ref|title|body|url)|links\[\]|decisions\.[A-Za-z0-9_-]+)$"
)
V1_STEP_TYPES = frozenset(("prompt", "skill", "interactive", "supervised-prompt"))
V2_HINT = "set `version: 2`; v2 step types are agent | bash | approval | ask | units (see docs/wise/research-ts-engine.md P2)"
STEP_BASE_KEYS = (
    "id",
    "type",
    "group",
    "depends_on",
    "trigger-rule",
    "when",
    "description",
    "optional",
    "harness",
    "model",
    "effort",
    "auth",
    "fallback",
    "mode",
    "resume",
    "max_turns",
    "timeout",
    "stale_after",
    "allowed_tools",
    "allow-api",
    "mcp",
)
STEP_KEYS = {
    "agent": (*STEP_BASE_KEYS, "prompt", "skill", "schema", "outputs", "until"),
    "bash": (*STEP_BASE_KEYS, "run", "outputs"),
    "approval": (*STEP_BASE_KEYS, "message"),
    "ask": (*STEP_BASE_KEYS, "message", "options", "allow_text", "output"),
    "units": (*STEP_BASE_KEYS, "pipeline", "items", "groups", "caps", "parallel", "reviewers"),
}
V1_STEP_KEY_HINTS = {
    "max_iterations": "drop it; use `schema:` for structured output (E9) and `max_turns:` to cap turns (E11)",
    "agent": "drop the roster role binding; fold the role into `prompt:` (a Claude child loads the plugin's agents)",
    "payload": "fold the arguments into `prompt:` (`skill:` sugar emits `Run /<skill>`)",
    "command": "rename to `run:`",
    "success": "drop it; a bash step succeeds on exit code 0, assert on output through `outputs:`",
    "cwd": "drop it; steps run in the run's cwd (`cd` inside `run:` when needed)",
    "question": "rename to `message:`",
    "header": "drop it; the harness renders the gate",
    "skip_label": "drop it; put the skip wording in `options:`",
}
TOP_KEYS = (
    "version",
    "name",
    "description",
    "author",
    "project-selection",
    "preflight",
    "requires",
    "tuning",
    "profiles",
    "inputs",
    "step-select",
    "steps",
    "agents",
)


def _record(value: Any) -> bool:
    return isinstance(value, dict)


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive_integer(value: Any) -> bool:
    return _number(value) and math.isfinite(value) and value >= 1 and int(value) == value


def _one_of(values: Any, value: Any) -> bool:
    return isinstance(value, str) and value in values


def _nullish(value: Any, default: Any) -> Any:
    return default if value is MISSING or value is None else value


class _Issues:
    def __init__(self) -> None:
        self.list: list[dict[str, Any]] = []

    def error(self, path: str, message: str, hint: str | None = None) -> None:
        self._add("error", path, message, hint)

    def warn(self, path: str, message: str, hint: str | None = None) -> None:
        self._add("warning", path, message, hint)

    def _add(self, level: str, path: str, message: str, hint: str | None) -> None:
        issue = {"level": level, "path": path, "message": message}
        if hint is not None:
            issue["hint"] = hint
        self.list.append(issue)

    def unknown(self, raw: dict[str, Any], path: str, known: Any) -> None:
        for key, _ in js_items(raw):
            if key not in known:
                self.warn(f"{path}.{key}" if path else key, f"unknown key `{key}` is ignored")


def _field(
    iss: _Issues,
    raw: dict[str, Any],
    out: dict[str, Any],
    path: str,
    key: str,
    check: Callable[[Any], bool],
    message: str,
) -> None:
    if key in raw:
        if check(raw[key]):
            out[key] = raw[key]
        else:
            iss.error(f"{path}.{key}" if path else key, message)


def _enum_field(
    iss: _Issues,
    raw: dict[str, Any],
    out: dict[str, Any],
    path: str,
    key: str,
    values: Any,
    *,
    one_of: bool = True,
) -> None:
    message = f"{key} must be {'one of ' if one_of else ''}{' | '.join(values)}"
    _field(iss, raw, out, path, key, lambda v: _one_of(values, v), message)


def _tuning_default(iss: _Issues, raw: Any, path: str) -> dict[str, Any] | None:
    if isinstance(raw, str):
        parts = [x.strip() for x in raw.split("/")]
        obj = f"{{ harness: claude, model: {parts[0]}"
        if len(parts) > 1 and parts[1]:
            obj += f", effort: {parts[1]}"
        iss.error(
            path, f"v1 string tuning value {js_json(raw)}", f"write it as a mapping: {obj} }}"
        )
        return None
    if not _record(raw):
        iss.error(path, "expected a mapping with harness / model / effort")
        return None
    iss.unknown(raw, path, ("harness", "model", "effort"))
    before = len(iss.list)
    out: dict[str, Any] = {}
    _enum_field(iss, raw, out, path, "harness", HARNESSES)
    _field(
        iss,
        raw,
        out,
        path,
        "model",
        lambda x: isinstance(x, str) and bool(x),
        "model must be a non-empty string",
    )
    _enum_field(iss, raw, out, path, "effort", EFFORTS)
    return out if len(iss.list) == before else None


def _harness_list(iss: _Issues, raw: Any, path: str) -> list[str] | None:
    if not isinstance(raw, list):
        iss.error(path, "expected a list of harnesses")
        return None
    out = []
    for i, value in enumerate(raw):
        if _one_of(HARNESSES, value):
            out.append(value)
        else:
            iss.error(f"{path}[{i}]", f"harness must be one of {' | '.join(HARNESSES)}")
    return out


def _tuning(iss: _Issues, raw: Any) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    if raw is MISSING or raw is None:
        return groups
    if not _record(raw):
        iss.error("tuning", "expected a mapping with `groups:`")
        return groups
    iss.unknown(raw, "tuning", ("groups",))
    raw_groups = _nullish(raw.get("groups"), [])
    if not isinstance(raw_groups, list):
        iss.error("tuning.groups", "expected a list of groups")
        return groups
    seen = set()
    for i, group in enumerate(raw_groups):
        p = f"tuning.groups[{i}]"
        if not _record(group):
            iss.error(p, "expected a mapping")
            continue
        iss.unknown(
            group,
            p,
            (
                "id",
                "label",
                "description",
                "default",
                "fallback",
                "locked",
                "allow-api",
                "options",
                "steps",
            ),
        )
        gid = group.get("id", MISSING)
        if not isinstance(gid, str) or not SLUG_RE.search(gid):
            iss.error(f"{p}.id", f"tuning group id {js_json(gid)} must match {SLUG_RE.pattern}")
            continue
        if gid in seen:
            iss.error(f"{p}.id", f"duplicate tuning group id {js_json(gid)}")
            continue
        seen.add(gid)
        if "steps" in group:
            iss.error(
                f"{p}.steps",
                "v1 `steps:` binding on a tuning group",
                f"remove `steps:`; set `group: {gid}` on each of those steps and give the group a `default: {{ harness, model, effort }}`",
            )
        default = None
        if "default" not in group:
            iss.error(f"{p}.default", "tuning group needs a `default: { harness, model, effort }`")
        else:
            default = _tuning_default(iss, group["default"], f"{p}.default")
        out: dict[str, Any] = {"id": gid, "default": default if default is not None else {}}
        for key in ("label", "description"):
            _field(iss, group, out, p, key, lambda x: isinstance(x, str), f"{key} must be a string")
        if "fallback" in group:
            fallback = _harness_list(iss, group["fallback"], f"{p}.fallback")
            if fallback is not None:
                out["fallback"] = fallback
        for key in ("locked", "allow-api"):
            _field(
                iss, group, out, p, key, lambda x: isinstance(x, bool), f"{key} must be a boolean"
            )
        if "options" in group:
            if not isinstance(group["options"], list):
                iss.error(f"{p}.options", "options must be a list of presets")
            else:
                options = []
                option_ids = set()
                for j, option in enumerate(group["options"]):
                    op = f"{p}.options[{j}]"
                    if not _record(option):
                        iss.error(op, "expected a mapping with id and value")
                        continue
                    iss.unknown(option, op, ("id", "label", "description", "value"))
                    oid = option.get("id", MISSING)
                    if not isinstance(oid, str) or not SLUG_RE.search(oid):
                        iss.error(
                            f"{op}.id", f"preset id {js_json(oid)} must match {SLUG_RE.pattern}"
                        )
                        continue
                    if oid == "default" or oid in option_ids:
                        iss.error(f"{op}.id", f"preset id {js_json(oid)} is reserved or duplicated")
                        continue
                    option_ids.add(oid)
                    value = _tuning_default(iss, option.get("value", MISSING), f"{op}.value")
                    if value is None:
                        continue
                    preset = {"id": oid, "value": value}
                    for key in ("label", "description"):
                        if isinstance(option.get(key), str):
                            preset[key] = option[key]
                    options.append(preset)
                out["options"] = options
        groups.append(out)
    return groups


def _profiles(iss: _Issues, raw: Any, group_ids: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if raw is MISSING or raw is None:
        return out
    if not _record(raw):
        iss.error("profiles", "expected a mapping keyed by low | medium | max")
        return out
    for level, raw_entry in js_items(raw):
        p = f"profiles.{level}"
        if level not in PROFILE_LEVELS:
            iss.error(
                p, f"profile level {js_json(level)} must be one of {' | '.join(PROFILE_LEVELS)}"
            )
            continue
        entry = _nullish(raw_entry, {})
        if not _record(entry):
            iss.error(p, "expected a mapping (an empty one means the workflow defaults)")
            continue
        iss.unknown(entry, p, ("tuning", "caps", "description", "step-preset", "skip", "team-mode"))
        for key in ("step-preset", "skip"):
            if key in entry:
                iss.error(
                    f"{p}.{key}",
                    f"v1 `{key}:` on a profile",
                    "drop it; v2 has no presets, the harness asks `step-select` as one multi-select over the optional steps",
                )
        if "team-mode" in entry:
            iss.error(
                f"{p}.team-mode",
                "v1 `team-mode:` on a profile",
                "drop it; v2 has no agent teams, one agent per step",
            )
        profile: dict[str, Any] = {}
        _field(
            iss,
            entry,
            profile,
            p,
            "description",
            lambda x: isinstance(x, str),
            "description must be a string",
        )
        tuning = _nullish(entry.get("tuning"), {})
        if not _record(tuning):
            iss.error(
                f"{p}.tuning", "expected a mapping of tuning group id to { harness, model, effort }"
            )
        else:
            tuning_out = {}
            for gid, value in js_items(tuning):
                tp = f"{p}.tuning.{gid}"
                if gid not in group_ids:
                    iss.error(tp, f"unknown tuning group {js_json(gid)}")
                elif value == "default":
                    iss.error(
                        tp,
                        'v1 `"default"` tuning value',
                        "omit the group from the profile to keep its default",
                    )
                else:
                    td = _tuning_default(iss, value, tp)
                    if td is not None:
                        tuning_out[gid] = td
            profile["tuning"] = tuning_out
        caps = _nullish(entry.get("caps"), {})
        if not _record(caps):
            iss.error(f"{p}.caps", "expected a mapping of cap name to positive integer")
        else:
            caps_out = {}
            for name, value in js_items(caps):
                if not CAP_RE.search(name):
                    iss.error(
                        f"{p}.caps.{name}", f"cap name {js_json(name)} must match {CAP_RE.pattern}"
                    )
                elif not _positive_integer(value):
                    iss.error(
                        f"{p}.caps.{name}",
                        f"cap {js_json(name)} must be a positive integer, got {js_json(value)}",
                    )
                else:
                    caps_out[name] = value
            profile["caps"] = caps_out
        out[level] = profile
    return out


def _utf16_units(text: str) -> str:
    encoded = text.encode("utf-16-le", "surrogatepass")
    return "".join(
        chr(int.from_bytes(encoded[i : i + 2], "little")) for i in range(0, len(encoded), 2)
    )


def _from_utf16(text: str) -> str:
    return b"".join(ord(char).to_bytes(2, "little") for char in text).decode(
        "utf-16-le", "surrogatepass"
    )


class _RegexMatch:
    def __init__(self, match: Any) -> None:
        self._match = match

    def group(self, number: int = 0) -> str | None:
        value = self._match.group(f"wise_group_{number}" if number else 0)
        return _from_utf16(value) if value is not None else None


class _Regex:
    def __init__(self, compiled: Any, groups: int) -> None:
        self._compiled = compiled
        self.groups = groups

    def search(self, value: str) -> _RegexMatch | None:
        match = self._compiled.search(_utf16_units(value))
        return _RegexMatch(match) if match is not None else None


@lru_cache(maxsize=128)
def _ascii_capture_only(pattern: str, wanted: int) -> bool:
    stack: list[tuple[int | None, int]] = []
    count = index = 0
    in_class = False
    body = None
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif char == "(" and not in_class:
            capture = not pattern.startswith("(?", index) or (
                pattern.startswith("(?<", index)
                and pattern[index + 3 : index + 4] not in ("=", "!")
            )
            start = index + 1
            if capture:
                count += 1
                if pattern.startswith("(?<", index):
                    start = pattern.index(">", index) + 1
            stack.append((count if capture else None, start))
        elif char == ")" and not in_class and stack:
            number, start = stack.pop()
            if number == wanted:
                body = pattern[start:index]
                break
        index += 1
    if body is None or not body.isascii():
        return False
    index = 0
    while index < len(body):
        char = body[index]
        if ord(char) > 127 or char == ".":
            return False
        if char == "[":
            end = index + 1
            while end < len(body):
                if body[end] == "\\":
                    end += 2
                elif body[end] == "]":
                    break
                else:
                    end += 1
            token = body[index : end + 1]
            compiled = regex.compile(_translate_regex(token), regex.VERSION0 | regex.ASCII)
            if compiled.search("".join(map(chr, range(128, 65536)))) is not None:
                return False
            index = end + 1
            continue
        if char == "\\" and index + 1 < len(body):
            escaped = body[index + 1]
            if escaped in "sSDWk123456789":
                return False
            if escaped in "xu":
                length = 2 if escaped == "x" else 4
                digits = body[index + 2 : index + 2 + length]
                if len(digits) == length and re.fullmatch(r"[0-9a-fA-F]+", digits):
                    if int(digits, 16) > 127:
                        return False
                    index += length
            index += 2
            continue
        index += 1
    return True


def _space_class(complement: bool = False) -> str:
    spaces = (
        set(range(9, 14))
        | {32, 160, 0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF}
        | set(range(0x2000, 0x200B))
    )
    points = sorted(set(range(65536)) - spaces if complement else spaces)
    ranges = []
    start = previous = points[0]
    for point in points[1:]:
        if point != previous + 1:
            ranges.append((start, previous))
            start = point
        previous = point
    ranges.append((start, previous))
    return "".join(
        f"\\u{start:04x}" + (f"-\\u{end:04x}" if end != start else "") for start, end in ranges
    )


_JS_SPACE = _space_class()
_JS_NONSPACE = _space_class(True)


@lru_cache(maxsize=1)
def _case_equivalents() -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for point in range(65536):
        char = chr(point)
        upper = char.upper()
        # Expansions and non-ASCII-to-ASCII uppercase folds leave the character unchanged.
        canonical = char if len(upper) != 1 or (point >= 128 and ord(upper) < 128) else upper
        groups.setdefault(canonical, []).append(char)
    return {char: tuple(group) for group in groups.values() if len(group) > 1 for char in group}


def _case_literal(text: str) -> str:
    out = []
    for char in _utf16_units(text):
        group = _case_equivalents().get(char, (char,))
        encoded = "".join(f"\\u{ord(value):04x}" for value in group)
        out.append(f"[{encoded}]" if len(group) > 1 else encoded)
    return "".join(out)


@lru_cache(maxsize=128)
def _case_class(source: str) -> str:
    negative = source.startswith("[^")
    positive = "[" + source[2:] if negative else source
    compiled = regex.compile(_translate_regex(positive), regex.VERSION0 | regex.ASCII)
    points: set[int] = set()
    equivalents = _case_equivalents()
    for point in range(65536):
        char = chr(point)
        if compiled.fullmatch(char) is not None:
            points.update(ord(value) for value in equivalents.get(char, (char,)))
    if not points:
        return r"[\s\S]" if negative else "(?!)"
    ordered = sorted(points)
    ranges = []
    start = previous = ordered[0]
    for point in ordered[1:]:
        if point != previous + 1:
            ranges.append((start, previous))
            start = point
        previous = point
    ranges.append((start, previous))
    body = "".join(
        f"\\u{start:04x}" + (f"-\\u{end:04x}" if end != start else "") for start, end in ranges
    )
    return f"[{'^' if negative else ''}{body}]"


def _regex_groups(pattern: str) -> tuple[int, dict[str, int]]:
    count = 0
    names = {}
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        elif char == "(" and not in_class:
            if not pattern.startswith("(?", index):
                count += 1
            elif pattern.startswith("(?<", index) and pattern[index + 3 : index + 4] not in (
                "=",
                "!",
            ):
                end = pattern.find(">", index + 3)
                name = pattern[index + 3 : end] if end >= 0 else ""
                if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name):
                    raise ValueError("Invalid capture group name")
                if name in names:
                    raise ValueError("Duplicate capture group name")
                count += 1
                names[name] = count
            elif pattern[index + 2 : index + 3] not in (":", "=", "!") and not pattern.startswith(
                ("(?<=", "(?<!"), index
            ):
                modifier = re.match(r"\(\?([ims]*)(?:-([ims]*))?:", pattern[index:])
                if modifier is None or not (modifier[1] or modifier[2]):
                    raise ValueError("Invalid group")
                flags = modifier[1] + (modifier[2] or "")
                if len(flags) != len(set(flags)):
                    raise ValueError("Repeated flag in flag group")
        index += 1
    return count, names


def _translate_regex(pattern: str) -> str:
    count, names = _regex_groups(pattern)
    capture_index = 0
    output = []
    in_class = False
    dot_all = multiline = ignore_case = False
    scopes: list[tuple[bool, bool, bool]] = []

    def backreference(number: int) -> str:
        target = f"wise_group_{number}"
        if ignore_case:
            if not _ascii_capture_only(pattern, number):
                raise ValueError(
                    "case-insensitive backreferences require an ASCII-only capture; Unicode backreference matching is unsupported"
                )
            return f"(?({target})(?ai:\\g<{target}>)|)"
        return f"(?({target})\\g<{target}>|)"

    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "[" and not in_class:
            if pattern.startswith("[^]", index):
                output.append(r"[\s\S]")
                index += 3
                continue
            if pattern.startswith("[]", index):
                output.append("(?!)")
                index += 2
                continue
            if ignore_case:
                end = index + 1
                while end < len(pattern):
                    if pattern[end] == "\\":
                        end += 2
                    elif pattern[end] == "]":
                        break
                    else:
                        end += 1
                if end >= len(pattern):
                    raise ValueError("Unterminated character class")
                output.append(_case_class(pattern[index : end + 1]))
                index = end + 1
                continue
            in_class = True
        elif char == "]" and in_class:
            in_class = False
        if not in_class and char == "(":
            scopes.append((dot_all, multiline, ignore_case))
            if not pattern.startswith("(?", index):
                capture_index += 1
                output.append(f"(?P<wise_group_{capture_index}>")
                index += 1
                continue
            modifier = re.match(r"\(\?([ims]*)(?:-([ims]*))?:", pattern[index:])
            if modifier is not None and (modifier[1] or modifier[2]):
                enabled, disabled = modifier[1], modifier[2] or ""
                dot_all = (dot_all or "s" in enabled) and "s" not in disabled
                multiline = (multiline or "m" in enabled) and "m" not in disabled
                ignore_case = (ignore_case or "i" in enabled) and "i" not in disabled
                output.append("(?:")
                index += len(modifier[0])
                continue
        elif not in_class and char == ")" and scopes:
            dot_all, multiline, ignore_case = scopes.pop()
        if (
            not in_class
            and pattern.startswith("(?<", index)
            and pattern[index + 3 : index + 4] not in ("=", "!")
        ):
            end = pattern.index(">", index + 3)
            name = pattern[index + 3 : end]
            capture_index += 1
            output.append(f"(?P<wise_group_{names[name]}>")
            index = end + 1
            continue
        if char == "\\":
            if index + 1 == len(pattern):
                raise ValueError("\\ at end of pattern")
            escaped = pattern[index + 1]
            index += 2
            if escaped in "sS":
                body = _JS_SPACE if escaped == "s" else _JS_NONSPACE
                output.append(body if in_class else f"[{body}]")
            elif escaped == "B" and in_class:
                output.append("B")
            elif escaped in "dDwWbBfnrtv" or escaped in r"^$\.*+?()[]{}|/-":
                output.append("\\" + escaped)
            elif (
                escaped == "c"
                and index < len(pattern)
                and pattern[index].isascii()
                and (
                    pattern[index].isalpha()
                    or (in_class and (pattern[index].isdigit() or pattern[index] == "_"))
                )
            ):
                output.append(f"\\x{ord(pattern[index].upper()) % 32:02x}")
                index += 1
            elif escaped in "xu" and re.match(
                r"[0-9a-fA-F]{" + ("2" if escaped == "x" else "4") + "}", pattern[index:]
            ):
                length = 2 if escaped == "x" else 4
                literal = chr(int(pattern[index : index + length], 16))
                output.append(
                    _case_literal(literal)
                    if ignore_case
                    else "\\" + escaped + pattern[index : index + length]
                )
                index += length
            elif escaped == "k" and names and not in_class:
                match = re.match(r"<([^>]+)>", pattern[index:])
                if match is None or match[1] not in names:
                    raise ValueError("Invalid named capture referenced")
                number = names[match[1]]
                output.append(backreference(number))
                index += len(match[0])
            elif escaped.isascii() and escaped.isdigit():
                match = re.match(r"[0-9]+", pattern[index - 1 :])
                assert match is not None
                digits = match[0]
                number = int(digits)
                if not in_class and escaped != "0" and number <= count:
                    output.append(backreference(number))
                    index += len(digits) - 1
                elif escaped in "01234567":
                    length = 3 if escaped in "0123" else 2
                    octal = re.match(r"[0-7]{1," + str(length) + "}", digits)
                    assert octal is not None
                    output.append(f"\\x{int(octal[0], 8):02x}")
                    index += len(octal[0]) - 1
                else:
                    output.append(escaped)
            elif escaped == "c":
                output.append(r"\\c")
            else:
                output.append(_case_literal(escaped) if ignore_case else escaped)
            continue
        if not in_class:
            if char == ".":
                output.append(r"[\s\S]" if dot_all else r"[^\n\r\u2028\u2029]")
            elif char == "^":
                output.append(r"(?:\A|(?<=[\n\r\u2028\u2029]))" if multiline else r"\A")
            elif char == "$":
                output.append(r"(?=[\n\r\u2028\u2029]|\Z)" if multiline else r"\Z")
            elif (
                char == "+"
                and output
                and (
                    output[-1] in ("*", "+", "?")
                    or re.search(r"(?<!\\)\{[0-9]+(?:,[0-9]*)?\}$", "".join(output))
                )
            ):
                raise ValueError("Nothing to repeat")
            else:
                output.append(_case_literal(char) if ignore_case and char.isalpha() else char)
        else:
            output.append(char)
        index += 1
    return _utf16_units("".join(output))


def _compile_regex(pattern: str) -> _Regex:
    try:
        converted = _translate_regex(pattern)
        return _Regex(
            regex.compile(converted, regex.VERSION0 | regex.ASCII), _regex_groups(pattern)[0]
        )
    except (ValueError, regex.error) as exc:
        reason = str(exc)
        if "missing )" in reason:
            reason = "Unterminated group"
        elif "unterminated character set" in reason:
            reason = "Unterminated character class"
        elif "nothing to repeat" in reason:
            reason = "Nothing to repeat"
        elif "unbalanced parenthesis" in reason:
            reason = "Unmatched ')'"
        elif "bad character range" in reason:
            reason = "Range out of order in character class"
        raise ValueError(f"Invalid regular expression: /{pattern}/: {reason}") from exc


def _regex_field(iss: _Issues, raw: Any, path: str) -> str | None:
    if not isinstance(raw, str) or not raw:
        iss.error(path, "expected a non-empty regex string")
        return None
    try:
        _compile_regex(raw)
    except ValueError as exc:
        iss.error(path, f"invalid regex: {exc}")
        return None
    return raw


def _escape_regex(value: str) -> str:
    return re.sub(r"([.*+?^${}()|\[\]\\])", r"\\\1", value)


def _inputs(iss: _Issues, raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if raw is MISSING or raw is None:
        return out
    if not isinstance(raw, list):
        iss.error("inputs", "expected a list of inputs")
        return out
    seen = set()
    for i, entry in enumerate(raw):
        p = f"inputs[{i}]"
        if not _record(entry):
            iss.error(p, "expected a mapping")
            continue
        iss.unknown(
            entry,
            p,
            (
                "name",
                "prompt",
                "description",
                "optional",
                "default",
                "from-context",
                "validate",
                "extract",
                "options",
            ),
        )
        name = entry.get("name", MISSING)
        if not isinstance(name, str) or not INPUT_NAME_RE.search(name):
            iss.error(f"{p}.name", f"input name {js_json(name)} must match {INPUT_NAME_RE.pattern}")
            continue
        if name in seen:
            iss.error(f"{p}.name", f"duplicate input {js_json(name)}")
            continue
        seen.add(name)
        item: dict[str, Any] = {"name": name}
        if "options" in entry:
            values = []
            if isinstance(entry["options"], list):
                values = [
                    js_string(_nullish(o.get("value"), "")) if _record(o) else js_string(o)
                    for o in entry["options"]
                ]
                values = [value for value in values if value]
            alt = (
                f'validate: "^({"|".join(_escape_regex(v) for v in values)})$"'
                if values
                else "a `validate:` regex"
            )
            iss.error(
                f"{p}.options",
                f"v1 choice input {js_json(name)}",
                f"v2 inputs use `validate:`: keep `default:` and add {alt}; without `extract:`, strict literal alternations containing only ASCII letters, digits, underscores, or hyphens render as choices. Other values and patterns render as text; use an `ask` step with `options:` when those values need a picker",
            )
        for key in ("prompt", "description"):
            _field(
                iss, entry, item, p, key, lambda x: isinstance(x, str), f"{key} must be a string"
            )
        _field(
            iss,
            entry,
            item,
            p,
            "optional",
            lambda x: isinstance(x, bool),
            "optional must be a boolean",
        )
        if "default" in entry and entry["default"] is not None:
            value = entry["default"]
            if isinstance(value, (str, bool, int, float)):
                item["default"] = js_string(value)
            else:
                iss.error(f"{p}.default", "default must be a scalar")
        if "from-context" in entry:
            value = entry["from-context"]
            if isinstance(value, str) and FROM_CONTEXT_RE.search(value):
                item["from-context"] = value
            else:
                iss.error(
                    f"{p}.from-context",
                    f"from-context {js_json(value)} must be one of guidance | ticket[].ref | ticket[].title | ticket[].body | ticket[].url | links[] | decisions.<key>",
                )
        for key in ("validate", "extract"):
            if key in entry:
                pattern = _regex_field(iss, entry[key], f"{p}.{key}")
                if pattern is not None:
                    item[key] = pattern
        out.append(item)
    return out


def _step_select(iss: _Issues, raw: Any, step_ids: set[str]) -> dict[str, Any] | None:
    if raw is MISSING or raw is None:
        return None
    if not _record(raw):
        iss.error("step-select", "expected a mapping with `prompt:` and `optional:`")
        return None
    iss.unknown(raw, "step-select", ("prompt", "optional", "presets"))
    out: dict[str, Any] = {}
    _field(
        iss,
        raw,
        out,
        "step-select",
        "prompt",
        lambda x: isinstance(x, str),
        "prompt must be a string",
    )
    if "presets" in raw:
        iss.error(
            "step-select.presets",
            "v1 `presets:`",
            "drop it; v2 has no presets, the harness asks `step-select` as one multi-select over `optional:`",
        )
    if "optional" in raw:
        if not isinstance(raw["optional"], list):
            iss.error("step-select.optional", "expected a list of step ids")
        else:
            ids = []
            seen = set()
            for i, entry in enumerate(raw["optional"]):
                p = f"step-select.optional[{i}]"
                if _record(entry):
                    steps = (
                        [js_string(x) for x in entry["steps"]]
                        if isinstance(entry.get("steps"), list)
                        else [js_string(_nullish(entry.get("id"), ""))]
                    )
                    iss.error(
                        p,
                        f"v1 step-select entry {js_json(entry.get('id', MISSING))} (label / steps / ask-group)",
                        f"list step ids directly: `optional: [{', '.join(steps)}]`; put the wording in each step's `description:`",
                    )
                elif not isinstance(entry, str):
                    iss.error(p, "expected a step id")
                elif entry not in step_ids:
                    iss.error(p, f"unknown step {js_json(entry)}")
                elif entry in seen:
                    iss.error(p, f"duplicate optional step {js_json(entry)}")
                else:
                    seen.add(entry)
                    ids.append(entry)
            out["optional"] = ids
    return out


def _schema_hint(outputs: list[str]) -> str:
    props = (
        ", ".join(f"{o}: {{ type: string }}" for o in outputs)
        if outputs
        else "<name>: { type: string }"
    )
    return f"replace with `schema: {{ type: object, properties: {{ {props} }}, required: [{', '.join(outputs)}] }}` and keep `outputs:` as the names to copy"


def _v1_hints(iss: _Issues, step: dict[str, Any], p: str) -> None:
    for key, hint in V1_STEP_KEY_HINTS.items():
        if key in step:
            iss.error(f"{p}.{key}", f"v1 step key `{key}`", hint)
    if "until" in step and step.get("type") != "agent":
        outputs: Any = step.get("outputs")
        iss.error(
            f"{p}.until",
            "v1 `until:` regex capture",
            _schema_hint(outputs if _strings(outputs) else []),
        )


def _step_base(
    iss: _Issues, step: dict[str, Any], p: str, ids: set[str], group_ids: set[str]
) -> dict[str, Any]:
    sid, kind = step["id"], step["type"]
    out = {"id": sid, "type": kind}
    if "group" in step:
        gid = step["group"]
        if not isinstance(gid, str) or gid not in group_ids:
            iss.error(f"{p}.group", f"group {js_json(gid)} does not name a tuning group")
        else:
            out["group"] = gid
            if kind not in ("agent", "units"):
                iss.warn(f"{p}.group", f"group has no effect on a {kind} step")
    if "depends_on" in step:
        deps = step["depends_on"]
        if not _strings(deps):
            iss.error(f"{p}.depends_on", "expected a list of step ids")
        else:
            for dep in deps:
                if dep == sid:
                    iss.error(f"{p}.depends_on", f"step {js_json(sid)} depends on itself")
                elif dep not in ids:
                    iss.error(f"{p}.depends_on", f"unknown step {js_json(dep)}")
            out["depends_on"] = deps
    _enum_field(iss, step, out, p, "trigger-rule", TRIGGER_RULES)
    if "when" in step:
        value = step["when"]
        if isinstance(value, list):
            expression = " && ".join(js_string(x) for x in value)
            iss.error(
                f"{p}.when",
                "v1 list of `when:` conditions",
                f'join them into one expression: `when: "{expression}"`',
            )
        elif isinstance(value, str):
            out["when"] = value
        else:
            iss.error(f"{p}.when", "when must be an expression string")
    _field(
        iss,
        step,
        out,
        p,
        "description",
        lambda x: isinstance(x, str),
        "description must be a string",
    )
    _field(
        iss, step, out, p, "optional", lambda x: isinstance(x, bool), "optional must be a boolean"
    )
    _enum_field(iss, step, out, p, "harness", HARNESSES)
    _field(
        iss,
        step,
        out,
        p,
        "model",
        lambda x: isinstance(x, str) and bool(x),
        "model must be a non-empty string",
    )
    _enum_field(iss, step, out, p, "effort", EFFORTS)
    _enum_field(iss, step, out, p, "auth", AUTH_MODES)
    if "fallback" in step:
        fallback = _harness_list(iss, step["fallback"], f"{p}.fallback")
        if fallback is not None:
            out["fallback"] = fallback
    _enum_field(iss, step, out, p, "mode", RUN_MODES)
    _enum_field(iss, step, out, p, "resume", ("unit", "fresh"), one_of=False)
    _field(
        iss, step, out, p, "max_turns", _positive_integer, "max_turns must be a positive integer"
    )
    for key in ("timeout", "stale_after"):
        _field(
            iss,
            step,
            out,
            p,
            key,
            lambda x: _number(x) and x > 0,
            f"{key} must be a positive number of seconds",
        )
    _field(
        iss,
        step,
        out,
        p,
        "allowed_tools",
        lambda x: _strings(x) and all(v.strip() for v in x),
        "allowed_tools must be a list of non-empty strings",
    )
    _field(
        iss, step, out, p, "allow-api", lambda x: isinstance(x, bool), "allow-api must be a boolean"
    )
    _enum_field(iss, step, out, p, "mcp", MCP_POLICIES)
    return out


def _required_string(iss: _Issues, raw: dict[str, Any], p: str, key: str) -> str | None:
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value
    iss.error(f"{p}.{key}", f"{key} must be a non-empty string")
    return None


def _agent(
    iss: _Issues, step: dict[str, Any], p: str, base: dict[str, Any]
) -> dict[str, Any] | None:
    prompt = None
    skill = None
    if "skill" in step:
        if not isinstance(step["skill"], str) or not step["skill"].strip():
            iss.error(f"{p}.skill", "skill must be a non-empty skill name")
        else:
            skill = step["skill"].strip().removeprefix("/")
            if "prompt" in step:
                iss.error(
                    f"{p}.prompt", "`skill:` and `prompt:` are exclusive; `skill:` emits the prompt"
                )
            if "harness" in base and base["harness"] != "claude":
                iss.error(
                    f"{p}.harness",
                    "`skill:` forces `harness: claude` (only a Claude child loads plugin skills)",
                )
            prompt = f"Run /{skill}"
            base["harness"] = "claude"
    else:
        prompt = _required_string(iss, step, p, "prompt")
    out = {**base, "type": "agent", "prompt": prompt or ""}
    if skill is not None:
        out["skill"] = skill
    schema = None
    if "schema" in step:
        if _record(step["schema"]):
            schema = step["schema"]
            out["schema"] = schema
        else:
            iss.error(f"{p}.schema", "schema must be a JSON-schema mapping")
    if "until" in step:
        if isinstance(step["until"], str):
            out["until"] = step["until"]
            iss.warn(
                f"{p}.until",
                "`until:` is deprecated and accepted for one release",
                _schema_hint(step["outputs"] if _strings(step.get("outputs")) else []),
            )
        else:
            iss.error(f"{p}.until", "until must be a regex string")
    if "outputs" in step:
        outputs = step["outputs"]
        if not _strings(outputs):
            iss.error(f"{p}.outputs", "outputs must be a list of names")
        else:
            out["outputs"] = outputs
            if schema is None and "until" not in out:
                iss.error(
                    f"{p}.outputs",
                    "outputs need a `schema:` to be copied from",
                    _schema_hint(outputs),
                )
            elif schema is not None and _record(schema.get("properties")):
                for name in outputs:
                    if name not in schema["properties"]:
                        iss.error(
                            f"{p}.outputs", f"output {js_json(name)} is not a schema property"
                        )
    return out if prompt is not None else None


def _typed_step(
    iss: _Issues,
    step: dict[str, Any],
    p: str,
    base: dict[str, Any],
    group_ids: set[str],
    cap_names: set[str],
) -> dict[str, Any] | None:
    kind = step["type"]
    if kind == "agent":
        return _agent(iss, step, p, base)
    key = "run" if kind == "bash" else "items" if kind == "units" else "message"
    ok = True
    if kind == "units" and not _one_of(("ticket", "plan"), step.get("pipeline")):
        iss.error(f"{p}.pipeline", "pipeline must be ticket | plan")
        ok = False
    value = _required_string(iss, step, p, key)
    out = {**base, key: value or ""}
    if kind == "bash":
        _field(iss, step, out, p, "outputs", _strings, "outputs must be a list of names")
    elif kind == "ask":
        _field(
            iss,
            step,
            out,
            p,
            "options",
            lambda x: _strings(x) and len(x) > 0,
            "options must be a non-empty list of strings",
        )
        _field(
            iss,
            step,
            out,
            p,
            "allow_text",
            lambda x: isinstance(x, bool),
            "allow_text must be a boolean",
        )
        _field(
            iss,
            step,
            out,
            p,
            "output",
            lambda x: isinstance(x, str) and CAP_RE.search(x) is not None,
            f"output must match {CAP_RE.pattern}",
        )
    elif kind == "units":
        groups = {}
        if not _record(step.get("groups")) or len(step["groups"]) == 0:
            iss.error(f"{p}.groups", "groups must map each phase to a tuning group id")
            ok = False
        else:
            for phase, gid in js_items(step["groups"]):
                if phase not in PHASES:
                    iss.warn(f"{p}.groups.{phase}", f"unknown phase {js_json(phase)}")
                if not isinstance(gid, str) or gid not in group_ids:
                    iss.error(
                        f"{p}.groups.{phase}", f"group {js_json(gid)} does not name a tuning group"
                    )
                    ok = False
                else:
                    groups[phase] = gid
        out = {
            **base,
            "pipeline": "plan" if step.get("pipeline") == "plan" else "ticket",
            "items": value or "",
            "groups": groups,
        }
        if "caps" in step:
            if not _strings(step["caps"]):
                iss.error(f"{p}.caps", "caps must be a list of cap names")
            else:
                for cap in step["caps"]:
                    if not CAP_RE.search(cap):
                        iss.error(
                            f"{p}.caps", f"cap name {js_json(cap)} must match {CAP_RE.pattern}"
                        )
                    elif cap not in cap_names:
                        iss.warn(f"{p}.caps", f"cap {js_json(cap)} is not set by any profile")
                out["caps"] = step["caps"]
        _field(
            iss, step, out, p, "parallel", _positive_integer, "parallel must be a positive integer"
        )
        _field(
            iss, step, out, p, "reviewers", _strings, "reviewers must be a list of GitHub logins"
        )
    return out if value is not None and ok else None


def _steps(
    iss: _Issues, raw: Any, group_ids: set[str], cap_names: set[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list) or not raw:
        iss.error("steps", "expected a non-empty list of steps")
        return out
    ids = set()
    for i, step in enumerate(raw):
        p = f"steps[{i}]"
        if (
            not _record(step)
            or "id" not in step
            or not isinstance(step.get("type"), str)
            or not step["type"]
        ):
            iss.error(p, f"step is missing id or type: {js_json(step)}")
            continue
        sid = step["id"]
        if not isinstance(sid, str) or not STEP_ID_RE.search(sid):
            iss.error(f"{p}.id", f"step id {js_json(sid)} must match {STEP_ID_RE.pattern}")
            continue
        if sid in ids:
            iss.error(f"{p}.id", f"duplicate step id {js_json(sid)}")
        ids.add(sid)
    for i, step in enumerate(raw):
        p = f"steps[{i}]"
        if (
            not _record(step)
            or not isinstance(step.get("id"), str)
            or not STEP_ID_RE.search(step["id"])
            or not isinstance(step.get("type"), str)
        ):
            continue
        sid, kind = step["id"], step["type"]
        if kind in V1_STEP_TYPES:
            hint = "use `type: units` for the ticket / plan loop (D14, engine-side) or `type: agent` for a single child; loops live in the engine"
            if kind == "prompt":
                hint = "rename to `type: agent`"
            elif kind == "skill":
                skill = step["skill"] if isinstance(step.get("skill"), str) else "<skill>"
                hint = f'use `type: agent` with `skill: {skill}` (sugar for `prompt: "Run /<skill>"`, harness claude)'
            iss.error(f"{p}.type", f"v1 step type {js_json(kind)} on step {js_json(sid)}", hint)
            _v1_hints(iss, step, p)
            continue
        if kind not in STEP_TYPES:
            iss.error(
                f"{p}.type",
                f"unknown step type {js_json(kind)}; expected {' | '.join(STEP_TYPES)}",
                V2_HINT,
            )
            continue
        _v1_hints(iss, step, p)
        iss.unknown(step, p, (*STEP_KEYS[kind], *V1_STEP_KEY_HINTS))
        base = _step_base(iss, step, p, ids, group_ids)
        typed = _typed_step(iss, step, p, base, group_ids, cap_names)
        if typed is not None:
            out.append(typed)
    return out


def _preflight(iss: _Issues, raw: Any) -> dict[str, Any] | None:
    if raw is MISSING or raw is None:
        return None
    if not _record(raw):
        iss.error("preflight", "expected a mapping")
        return None
    iss.unknown(
        raw,
        "preflight",
        ("control-mode", "worktree", "permissions", "rename_session", "tuning", "step-select"),
    )
    for key in ("rename_session", "tuning", "step-select"):
        if key in raw:
            iss.error(
                f"preflight.{key}",
                f"v1 pre-flight pin `{key}`",
                "drop it; v2 always builds the questionary from `tuning:` / `step-select:` and the harness renames sessions (D11)",
            )
    out: dict[str, Any] = {}
    cm = raw.get("control-mode", MISSING)
    if cm is not MISSING:
        if _one_of(("synchronous", "interactive"), cm):
            out["control-mode"] = cm
        elif _one_of(("wave-sync", "auto-advance"), cm):
            iss.error(
                "preflight.control-mode",
                f"v1 control mode {js_json(cm)}",
                "use `control-mode: interactive` (gates pause the run)",
            )
        elif cm == "prompt":
            iss.error(
                "preflight.control-mode",
                'v1 control mode "prompt"',
                "pick `synchronous` or `interactive`; v2 does not ask the control mode",
            )
        else:
            iss.error("preflight.control-mode", "control-mode must be synchronous | interactive")
    _enum_field(iss, raw, out, "preflight", "permissions", ("allowlist", "full"), one_of=False)
    wt = raw.get("worktree", MISSING)
    if wt is not MISSING:
        if _one_of(("current", "new"), wt):
            out["worktree"] = wt
        elif wt == "prompt":
            iss.error(
                "preflight.worktree",
                'v1 worktree "prompt"',
                "pick `current` or `new`; v2 does not ask the worktree",
            )
        else:
            iss.error("preflight.worktree", "worktree must be current | new")
    return out


def _requires(iss: _Issues, raw: Any) -> dict[str, Any] | None:
    if raw is MISSING or raw is None:
        return None
    if isinstance(raw, list):
        plugins = []
        for entry in raw:
            if not _record(entry):
                continue
            if isinstance(entry.get("plugin"), str):
                plugins.append(entry["plugin"])
            elif isinstance(entry.get("skill"), str):
                plugins.append(entry["skill"].split(":", 1)[0])
        iss.error(
            "requires",
            "v1 `requires:` list of { plugin } / { skill } entries",
            f"write `requires: {{ plugins: [{', '.join(plugins)}] }}` (a skill requirement names its owning plugin)",
        )
        return None
    if not _record(raw):
        iss.error("requires", "expected a mapping with `plugins:` and/or `tools:`")
        return None
    iss.unknown(raw, "requires", ("plugins", "tools"))
    out: dict[str, Any] = {}
    _field(iss, raw, out, "requires", "plugins", _strings, "expected a list of plugin names")
    _field(iss, raw, out, "requires", "tools", _strings, "expected a list of executable names")
    return out


def validate_def(raw: Any, path: str) -> dict[str, Any]:
    iss = _Issues()
    if not _record(raw):
        iss.error("", f"{path}: expected a YAML mapping at the top level")
        return {"issues": iss.list}
    iss.unknown(raw, "", TOP_KEYS)
    version = raw.get("version", MISSING)
    if version is MISSING:
        iss.error("version", "missing `version`", V2_HINT)
    elif _number(version) and version == 1:
        iss.error("version", "v1 workflow", V2_HINT)
    elif not _number(version) or version != 2:
        iss.error("version", f"unsupported version {js_json(version)}", V2_HINT)
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        iss.error("name", "name must be a non-empty string")
    if "description" in raw and not isinstance(raw["description"], str):
        iss.error("description", "description must be a string")
    if "agents" in raw:
        iss.error(
            "agents",
            "v1 workflow-level `agents:` policy",
            "drop it; v2 has no roster routing, each `agent` step carries its own prompt",
        )
    project_selection = raw.get("project-selection", MISSING)
    if project_selection is not MISSING and not _one_of(
        ("current", "ask", "none"), project_selection
    ):
        if project_selection == "prompt":
            iss.error("project-selection", 'v1 value "prompt"', "use `project-selection: ask`")
        elif project_selection == "any":
            iss.error("project-selection", 'v1 value "any"', "use `project-selection: none`")
        else:
            iss.error("project-selection", "project-selection must be current | ask | none")
    preflight = _preflight(iss, raw.get("preflight", MISSING))
    requires = _requires(iss, raw.get("requires", MISSING))
    groups = _tuning(iss, raw.get("tuning", MISSING))
    group_ids = {g["id"] for g in groups}
    profiles = _profiles(iss, raw.get("profiles", MISSING), group_ids)
    cap_names = {cap for p in profiles.values() for cap in p.get("caps", {})}
    inputs = _inputs(iss, raw.get("inputs", MISSING))
    steps = _steps(iss, raw.get("steps", MISSING), group_ids, cap_names)
    step_select = _step_select(iss, raw.get("step-select", MISSING), {s["id"] for s in steps})
    if any(issue["level"] == "error" for issue in iss.list):
        return {"issues": iss.list}
    definition = {"version": 2, "name": name, "steps": steps}
    if "description" in raw:
        definition["description"] = raw["description"]
    if project_selection is not MISSING:
        definition["project-selection"] = project_selection
    for key, value in (("preflight", preflight), ("requires", requires)):
        if value is not None:
            definition[key] = value
    for key, normalized in (
        ("tuning", {"groups": groups}),
        ("profiles", profiles),
        ("inputs", inputs),
    ):
        if key in raw and raw[key] is not None:
            definition[key] = normalized
    if step_select is not None:
        definition["step-select"] = step_select
    return {"def": json_value(definition), "issues": iss.list}


def default_roots(
    opts: Mapping[str, Any] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: str | None = None,
) -> dict[str, str]:
    options = dict(opts or {})
    values = dict(env if env is not None else options.get("env", os.environ))
    selected_home = home if home is not None else options.get("home")
    if selected_home is not None:
        values["HOME"] = selected_home
    return {
        "user_root": str(plugin_data_root(values) / "workflows/definitions"),
        "bundled_root": str(PLUGIN_ROOT / "workflows"),
    }


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def locate_def(name: str, roots: Mapping[str, Any]) -> dict[str, str] | None:
    if name in RESERVED_NAMES:
        return None
    for source, root_key in (("user", "user_root"), ("bundled", "bundled_root")):
        root = Path(roots[root_key])
        for path in (root / name / "workflow.yaml", root / f"{name}.yaml"):
            if _is_file(path):
                return {
                    "name": name,
                    "path": str(path),
                    "dir": str(path.parent) if path.name == "workflow.yaml" else "",
                    "source": source,
                }
    return None


def load_def(path: str | Path) -> Any:
    return _nullish(parse_yaml(Path(path).read_text(encoding="utf-8")), {})


def load_and_validate(located: Mapping[str, Any]) -> dict[str, Any]:
    return validate_def(load_def(located["path"]), located["path"])


def list_defs(roots: Mapping[str, Any]) -> list[dict[str, Any]]:
    seen = set()
    items = []
    for source, root_key in (("user", "user_root"), ("bundled", "bundled_root")):
        root = Path(roots[root_key])
        if not _is_dir(root):
            continue
        entries = []
        seen_in_root = set()
        children = sorted(root.iterdir(), key=lambda p: p.name.encode("utf-16-be", "surrogatepass"))
        for child in children:
            path = child / "workflow.yaml"
            if _is_dir(child) and _is_file(path):
                entries.append((child.name, path))
                seen_in_root.add(child.name)
        for child in children:
            if child.name.endswith(".yaml") and child.stem not in seen_in_root and _is_file(child):
                entries.append((child.stem, child))
        for name, path in entries:
            try:
                data = load_def(path)
            except Exception as exc:
                items.append(
                    {
                        "name": name,
                        "description": f"<unreadable: {exc}>",
                        "source": source,
                        "shadowed": name in seen,
                        "path": str(path),
                    }
                )
                continue
            raw = data if _record(data) else {}
            description = (
                raw["description"].strip() if isinstance(raw.get("description"), str) else ""
            )
            items.append(
                {
                    "name": raw["name"]
                    if isinstance(raw.get("name"), str) and raw["name"]
                    else name,
                    "description": description or None,
                    "source": source,
                    "shadowed": name in seen,
                    "path": str(path),
                }
            )
            seen.add(name)
    return items


def installed_plugins(
    opts: Mapping[str, Any] | None = None,
    *,
    plugins_root: str | Path | None = None,
    home: str | Path | None = None,
) -> set[str]:
    options = opts or {}
    root = Path(
        plugins_root
        if plugins_root is not None
        else options.get(
            "plugins_root", Path(home or options.get("home", Path.home())) / ".claude/plugins"
        )
    )
    names: set[str] = set()
    if not _is_dir(root):
        return names
    try:
        raw = json.loads((root / "installed_plugins.json").read_text())
        plugins: Any = raw.get("plugins") if _record(raw) else None
        if _record(plugins):
            names.update(key.split("@", 1)[0] for key in plugins if key.split("@", 1)[0])
    except (OSError, ValueError):
        pass
    if names:
        return names

    def walk(directory: Path, depth: int) -> None:
        if depth > 4 or not _is_dir(directory):
            return
        manifest = directory / ".claude-plugin/plugin.json"
        if _is_file(manifest):
            name = None
            try:
                data = json.loads(manifest.read_text())
                if _record(data) and isinstance(data.get("name"), str) and data["name"]:
                    name = data["name"]
            except (OSError, ValueError):
                pass
            parts = directory.relative_to(root).parts
            names.add(
                name
                if name is not None
                else parts[2]
                if len(parts) >= 3 and parts[0] == "cache"
                else directory.name
            )
            return
        try:
            children = list(directory.iterdir())
        except OSError:
            return
        for child in children:
            if _is_dir(child):
                walk(child, depth + 1)

    try:
        children = list(root.iterdir())
    except OSError:
        return names
    for child in children:
        if _is_dir(child):
            walk(child, 1)
    return names


def on_path(name: str, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return any(
        directory and os.access(os.path.join(directory, name), os.X_OK)
        for directory in values.get("PATH", "").split(":")
    )


def probe_requires(
    definition: Mapping[str, Any],
    opts: Mapping[str, Any] | None = None,
    *,
    installed: Set[str] | None = None,
    has_tool: Callable[[str], bool] | None = None,
    plugins_root: str | Path | None = None,
) -> dict[str, Any]:
    options = opts or {}
    req = definition.get("requires") or {}
    missing: list[str] = []
    if req.get("plugins"):
        available = installed if installed is not None else options.get("installed")
        if available is None:
            available = installed_plugins(plugins_root=plugins_root or options.get("plugins_root"))
        missing.extend(f"plugin:{plugin}" for plugin in req["plugins"] if plugin not in available)
    checker = has_tool if has_tool is not None else options.get("has_tool", on_path)
    missing.extend(f"tool:{tool}" for tool in req.get("tools", []) if not checker(tool))
    return {"ok": len(missing) == 0, "missing": missing}


def list_inputs(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {**item, "prompt": _nullish(item.get("prompt"), f"Value for {item['name']}?")}
        for item in definition.get("inputs", [])
    ]


def validate_input(
    raw: str, extract: str | None = None, validate: str | None = None
) -> dict[str, Any]:
    value = raw
    if extract:
        try:
            pattern = _compile_regex(extract)
        except ValueError as exc:
            return {
                "ok": False,
                "reason": "bad-extract-regex",
                "message": f"INVALID:bad-extract-regex:{exc}",
            }
        match = pattern.search(raw)
        if match is None:
            return {"ok": False, "reason": "no-match", "message": "INVALID:no-match"}
        value = (match.group(1) or "") if pattern.groups else (match.group(0) or "")
    if validate:
        try:
            _compile_regex(validate)
            pattern = _compile_regex(f"^(?:{validate})$")
        except ValueError as exc:
            return {
                "ok": False,
                "reason": "bad-validate-regex",
                "message": f"INVALID:bad-validate-regex:{exc}",
            }
        if not pattern.search(value):
            return {"ok": False, "reason": "validate", "message": "INVALID:validate"}
    return {"ok": True, "value": value}
