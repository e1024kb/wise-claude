from __future__ import annotations

import json
import math
import re
import warnings
from datetime import datetime, time, timezone
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
from ruamel.yaml.resolver import VersionedResolver

MISSING = object()


class YamlTimestamp(dict[str, Any]):
    def __init__(self, value: str) -> None:
        super().__init__()
        parsed = SafeConstructor().construct_yaml_timestamp(
            ScalarNode("tag:yaml.org,2002:timestamp", value)
        )
        if not isinstance(parsed, datetime):
            parsed = datetime.combine(parsed, time.min)
        self.iso = (
            parsed.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds")
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        )
        self.iso = self.iso.replace("+00:00", "Z")


def json_value(value: Any) -> Any:
    if isinstance(value, YamlTimestamp):
        return value.iso
    if isinstance(value, dict):
        return {key: json_value(entry) for key, entry in value.items()}
    if isinstance(value, list):
        return [json_value(entry) for entry in value]
    return value


def js_string(value: Any) -> str:
    if value is MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (float, int)):
        if not math.isfinite(value):
            return "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"
        if value == 0:
            return "0"
        if float(value).is_integer() and abs(value) < 1e21:
            return str(int(value))
        text = str(value).lower()
        if "e" in text:
            mantissa, exponent = text.split("e")
            exp = int(exponent)
            if -6 <= exp < 21:
                return format(value, ".15f").rstrip("0").rstrip(".")
            return f"{mantissa.removesuffix('.0')}e{'+' if exp >= 0 else ''}{exp}"
        return text
    if isinstance(value, list):
        return ",".join("" if item is None else js_string(item) for item in value)
    return "[object Object]"


def js_json(value: Any) -> str:
    if isinstance(value, YamlTimestamp):
        return json.dumps(value.iso)
    if value is MISSING:
        return "undefined"
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return js_string(value) if math.isfinite(value) else "null"
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join("null" if x is MISSING else js_json(x) for x in value) + "]"
    return (
        "{"
        + ",".join(
            json.dumps(str(k), ensure_ascii=False) + ":" + js_json(v)
            for k, v in js_items(value)
            if v is not MISSING
        )
        + "}"
    )


def js_items(value: dict[str, Any]) -> list[tuple[str, Any]]:
    indexed = []
    ordinary = []
    for key, entry in value.items():
        if re.fullmatch(r"0|[1-9][0-9]*", key) and int(key) < 2**32 - 1:
            indexed.append((key, entry))
        else:
            ordinary.append((key, entry))
    return sorted(indexed, key=lambda pair: int(pair[0])) + ordinary


_CORE_TAGS = (
    ("null", re.compile(r"^(?:~|null|Null|NULL|)$"), "~nN"),
    ("bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), "tTfF"),
    ("int", re.compile(r"^(?:[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+)$"), "-+0123456789"),
    (
        "float",
        re.compile(
            r"^(?:[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)[eE][-+]?[0-9]+|"
            r"[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*)|[-+]?\.(?:inf|Inf|INF)|\.nan|\.NaN|\.NAN)$"
        ),
        "-+.0123456789",
    ),
)


class _CoreResolver(VersionedResolver):
    @property
    def versioned_resolver(self) -> dict[Any, Any]:
        if self.processing_version == (1, 1):
            return super().versioned_resolver
        rules: dict[Any, Any] = {"": [("tag:yaml.org,2002:null", _CORE_TAGS[0][1])]}
        for tag, pattern, starts in _CORE_TAGS:
            for first in starts:
                rules.setdefault(first, []).append((f"tag:yaml.org,2002:{tag}", pattern))
        return rules


def parse_yaml(source: str) -> Any:
    """Use core scalars by default and honor explicit YAML 1.1 documents."""
    directive = re.search(r"(?m)^%YAML ([0-9]+)\.([0-9]+)(?:[ \t]|$)", source)
    legacy = directive is not None and directive.group(1, 2) == ("1", "1")
    if directive is not None and directive.group(1, 2) not in (("1", "1"), ("1", "2")):
        warnings.warn(
            f"Unsupported YAML version {directive[1]}.{directive[2]}; using YAML 1.2", stacklevel=2
        )
        source = source[: directive.start(1)] + "1.2" + source[directive.end(2) :]
    yaml = YAML(typ="base")
    yaml.Resolver = _CoreResolver
    node = yaml.compose(source)
    cache: dict[int, Any] = {}

    def construct(current: Any) -> Any:
        identity = id(current)
        if identity in cache:
            return cache[identity]
        if isinstance(current, ScalarNode):
            value = current.value
            tag = (current.tag or "").rsplit(":", 1)[-1]
            if tag == "timestamp":
                return YamlTimestamp(value)
            if tag == "null":
                return None if value in ("", "~", "null", "Null", "NULL") else value
            if tag == "bool":
                if value.lower() in (
                    ("y", "yes", "true", "on", "n", "no", "false", "off")
                    if legacy
                    else ("true", "false")
                ):
                    return value.lower() in ("y", "yes", "true", "on")
                warnings.warn(f"Unresolved YAML tag {current.tag}: {value}", stacklevel=2)
                return value
            if tag in ("int", "float"):
                text = value.replace("_", "") if legacy else value
                if not legacy:
                    rule = _CORE_TAGS[2 if tag == "int" else 3][1]
                    if not rule.fullmatch(text):
                        warnings.warn(f"Unresolved YAML tag {current.tag}: {value}", stacklevel=2)
                        return value
                if text.lower() == ".nan":
                    return math.nan
                if text.lower().lstrip("-+") == ".inf":
                    return -math.inf if text.startswith("-") else math.inf
                if ":" in text and legacy:
                    negative = text.startswith("-")
                    number = 0.0
                    for part in text.lstrip("-+").split(":"):
                        number = number * 60 + float(part)
                    number = -number if negative else number
                    return int(number) if tag == "int" else number
                if tag == "float":
                    return float(text)
                unsigned = text.lstrip("-+")
                radix = (
                    16
                    if unsigned.startswith("0x")
                    else 8
                    if unsigned.startswith("0o")
                    else 2
                    if legacy and unsigned.startswith("0b")
                    else 8
                    if legacy and len(unsigned) > 1 and unsigned.startswith("0")
                    else 10
                )
                digits = unsigned[2:] if unsigned.startswith(("0x", "0o", "0b")) else unsigned
                raw = int(digits, radix) * (-1 if text.startswith("-") else 1)
                try:
                    number = float(raw)
                except OverflowError:
                    number = math.inf if raw > 0 else -math.inf
                return int(number) if math.isfinite(number) else number
            return value

        if isinstance(current, SequenceNode):
            sequence: list[Any] = []
            cache[identity] = sequence
            sequence.extend(construct(child) for child in current.value)
            return sequence
        if isinstance(current, MappingNode):
            mapping: dict[str, Any] = {}
            cache[identity] = mapping
            seen = set()
            explicit = {
                js_string(construct(k))
                for k, _ in current.value
                if k.tag != "tag:yaml.org,2002:merge"
            }
            for key_node, value_node in current.value:
                if legacy and key_node.tag == "tag:yaml.org,2002:merge":
                    merged = construct(value_node)
                    sources = merged if isinstance(merged, list) else [merged]
                    for inherited in sources:
                        if not isinstance(inherited, dict):
                            raise ValueError("Merge sources must be mappings")
                        for key, value in inherited.items():
                            if key not in explicit:
                                mapping.setdefault(key, value)
                    continue
                key_value = construct(key_node)
                key = js_string(key_value)
                signature = (
                    float
                    if isinstance(key_value, (int, float)) and not isinstance(key_value, bool)
                    else type(key_value),
                    key,
                )
                if signature in seen:
                    raise ValueError(
                        f"Map keys must be unique at line {key_node.start_mark.line + 1}"
                    )
                seen.add(signature)
                mapping[key] = construct(value_node)
            ordered = dict(js_items(mapping))
            mapping.clear()
            mapping.update(ordered)
            return mapping
        return None

    return {} if node is None else construct(node)
