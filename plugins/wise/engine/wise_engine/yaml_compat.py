from __future__ import annotations

import json
import math
import re
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
from ruamel.yaml.resolver import VersionedResolver

MISSING = object()


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
        rules: dict[Any, Any] = {"": [("tag:yaml.org,2002:null", _CORE_TAGS[0][1])]}
        for tag, pattern, starts in _CORE_TAGS:
            for first in starts:
                rules.setdefault(first, []).append((f"tag:yaml.org,2002:{tag}", pattern))
        return rules


def parse_yaml(source: str) -> Any:
    """Load YAML core scalars without timestamp or merge-key coercion."""
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
            if tag == "null":
                return None
            if tag == "bool":
                return value.lower() == "true"
            if tag == "int":
                radix = 8 if value.startswith("0o") else 16 if value.startswith("0x") else 10
                raw = int(value[2:] if radix != 10 else value, radix)
                try:
                    number = float(raw)
                except OverflowError:
                    number = math.inf if raw > 0 else -math.inf
                return int(number) if math.isfinite(number) else number
            if tag == "float":
                if value.lower() == ".nan":
                    return math.nan
                if value.lower().endswith(".inf"):
                    return -math.inf if value.startswith("-") else math.inf
                return float(value)
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
            for key_node, value_node in current.value:
                key_value = construct(key_node)
                key = js_string(key_value)
                signature = (type(key_value), key)
                if signature in seen:
                    raise ValueError(
                        f"Map keys must be unique at line {key_node.start_mark.line + 1}"
                    )
                seen.add(signature)
                mapping[key] = construct(value_node)
            return dict(js_items(mapping))
        return None

    return {} if node is None else construct(node)
