from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import shlex
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit

from .launcher import resolve_root, validate_root

HOSTS = ("claude", "codex", "cursor", "grok")
SERVER = "wise-engine"


class SetupError(RuntimeError):
    pass


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def location(home: Path) -> Path:
    return home / ".local/share/wise"


def config_path(host: str, home: Path, override: str | None = None) -> Path:
    if host not in HOSTS:
        raise SetupError(f"Unknown host: {host}")
    defaults = {
        "claude": ".claude.json",
        "codex": ".codex/config.toml",
        "cursor": ".cursor/mcp.json",
        "grok": ".grok/config.toml",
    }
    if override:
        return Path(override).expanduser().absolute()
    if home.absolute() == Path.home().absolute():
        if host == "codex" and os.environ.get("CODEX_HOME"):
            return Path(os.environ["CODEX_HOME"]).expanduser().absolute() / "config.toml"
        if host == "claude" and os.environ.get("CLAUDE_CONFIG_DIR"):
            return Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser().absolute() / ".claude.json"
    return home / defaults[host]


def merge_config(host: str, raw: bytes | None, entry: dict[str, Any]) -> bytes:
    text = raw.decode() if raw is not None else ""
    try:
        if host in ("codex", "grok"):
            doc = tomlkit.parse(text)
            servers = doc.setdefault("mcp_servers", tomlkit.table())
            previous = servers.get(SERVER, {})
            updated = dict(previous)
            for key in ("url", "headers", "http_headers", "bearer_token_env_var", "type"):
                updated.pop(key, None)
            updated.update(entry)
            if "env" in entry:
                updated["env"] = {
                    **{
                        key: value
                        for key, value in dict(previous.get("env", {})).items()
                        if key != "WISE_PLUGIN_ROOT"
                    },
                    **entry["env"],
                }
            servers[SERVER] = updated
            return tomlkit.dumps(doc).encode()
        json_doc = json.loads(_json_clean(text)) if text.strip() else {}
        servers = json_doc.setdefault("mcpServers", {})
        previous = servers.get(SERVER, {})
        updated = dict(previous)
        for key in ("url", "headers"):
            updated.pop(key, None)
        updated.update(entry)
        if "env" in entry:
            updated["env"] = {
                **{
                    key: value
                    for key, value in dict(previous.get("env", {})).items()
                    if key != "WISE_PLUGIN_ROOT"
                },
                **entry["env"],
            }
        updated["type"] = "stdio"
        servers[SERVER] = updated
        return _json_replace(
            text if text.strip() else "{}", ("mcpServers", SERVER), updated
        ).encode()
    except (ValueError, TypeError, AttributeError) as error:
        raise SetupError("Host config is invalid or unsupported; it was not modified") from error


def _json_clean(text: str) -> str:
    chars = list(text)
    index = 0
    quoted = False
    while index < len(chars):
        if quoted:
            if chars[index] == "\\":
                index += 2
                continue
            if chars[index] == '"':
                quoted = False
        elif chars[index] == '"':
            quoted = True
        elif text[index : index + 2] == "//":
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            chars[index:end] = " " * (end - index)
            index = end
            continue
        elif text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("Unterminated JSON comment")
            end += 2
            chars[index:end] = ["\n" if ch == "\n" else " " for ch in text[index:end]]
            index = end
            continue
        index += 1
    cleaned = "".join(chars)
    quoted = False
    index = 0
    while index < len(chars):
        if quoted:
            if chars[index] == "\\":
                index += 2
                continue
            if chars[index] == '"':
                quoted = False
        elif chars[index] == '"':
            quoted = True
        elif chars[index] == "," and cleaned[index + 1 :].lstrip()[:1] in ("}", "]"):
            chars[index] = " "
        index += 1
    return "".join(chars)


def _json_replace(text: str, keys: tuple[str, ...], value: Any) -> str:
    clean = _json_clean(text)
    decoder = json.JSONDecoder()
    start = len(clean) - len(clean.lstrip())
    if clean[start : start + 1] != "{":
        raise ValueError("JSON config must be an object")
    index = start + 1
    fields: dict[str, tuple[int, int]] = {}
    while True:
        while index < len(clean) and clean[index] in " \r\n\t,":
            index += 1
        if clean[index : index + 1] == "}":
            break
        key, index = decoder.raw_decode(clean, index)
        while clean[index].isspace():
            index += 1
        if clean[index] != ":" or key in fields:
            raise ValueError("Invalid or duplicate JSON member")
        index += 1
        while clean[index].isspace():
            index += 1
        begin = index
        _, index = decoder.raw_decode(clean, index)
        fields[key] = (begin, index)
    key = keys[0]
    if key in fields:
        begin, end = fields[key]
        replacement = (
            _json_replace(text[begin:end], keys[1:], value)
            if len(keys) > 1
            else json.dumps(value, ensure_ascii=False, indent=2)
        )
        return text[:begin] + replacement + text[end:]
    nested = value
    for child in reversed(keys[1:]):
        nested = {child: nested}
    addition = json.dumps(key) + ": " + json.dumps(nested, ensure_ascii=False, indent=2)
    return text[: start + 1] + "\n" + addition + ("," if fields else "") + text[start + 1 :]


@dataclass
class Change:
    path: Path
    before: bytes | None
    after: bytes
    mode: int = 0o600
    before_mode: int | None = None

    def preview(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "before_sha256": digest(self.before),
            "after_sha256": digest(self.after),
            "changed": self.before != self.after or self.before_mode != self.mode,
        }


@dataclass
class Plan:
    host: str
    root: Path
    state: Path
    entry: dict[str, Any]
    changes: list[Change]

    def preview(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "plugin_root": str(self.root),
            "server": SERVER,
            "entry": self.entry,
            "files": [change.preview() for change in self.changes],
            "host_verified": False,
        }


def plan_setup(
    *,
    plugin_root: str | Path,
    host: str,
    home: str | Path,
    config: str | None = None,
    source: dict[str, Any] | None = None,
    python: str | None = None,
) -> Plan:
    root = validate_root(plugin_root)
    home = Path(home).expanduser().absolute()
    target = config_path(host, home, config)
    state = location(home)
    registry_path = state / "installations.json"
    original = read(registry_path)
    registry = json.loads(original) if original else {"version": 1, "hosts": {}}
    binding: dict[str, Any] = {"plugin_root": str(root)}
    if source is not None:
        binding["source"] = source
    registry["hosts"][host] = binding
    registry["default_host"] = host
    if resolve_root(registry, host).resolve() != root.resolve():
        raise SetupError("Active metadata does not select the installation being registered")
    launcher = state / "bin/wise-engine"
    interpreter = python or str(getattr(sys, "_base_executable", None) or sys.executable)
    if not os.path.isabs(interpreter) or not os.access(interpreter, os.X_OK):
        raise SetupError("Python must be an absolute executable path")
    entry = {
        "command": str(launcher),
        "args": ["--wise-host", host, "mcp"],
        "env": {"WISE_PYTHON": interpreter, "WISE_HOST": host},
    }
    registry["hosts"][host]["python"] = interpreter
    config_bytes = merge_config(host, read(target), entry)
    binding.update(
        config=str(target),
        entry_sha256=digest(json_bytes(_server_entry(host, config_bytes))),
        install_version=json.loads((root / ".claude-plugin/plugin.json").read_text()).get(
            "version"
        ),
    )
    proposed = [
        (state / "launcher.py", Path(__file__).with_name("launcher.py").read_bytes(), 0o600),
        (
            launcher,
            _launcher_script(registry),
            0o700,
        ),
        (registry_path, json_bytes(registry), 0o600),
        (target, config_bytes, 0o600),
    ]
    changes = []
    for path, content, mode in proposed:
        if path.is_symlink():
            raise SetupError(f"Refusing to replace a symlink: {path}; use its explicit target")
        existing = read(path)
        before_mode = stat.S_IMODE(path.stat().st_mode) if existing is not None else None
        if before_mode is not None and path != launcher:
            mode = before_mode
        changes.append(Change(path, existing, content, mode, before_mode))
    return Plan(host, root, state, entry, changes)


def _launcher_script(registry: dict[str, Any]) -> bytes:
    lines = [
        "#!/bin/sh",
        'python_bin="${WISE_PYTHON:-}"',
        'if [ -z "$python_bin" ]; then',
        '  host="${WISE_HOST:-' + registry["default_host"] + '}"',
        '  if [ "${1:-}" = --wise-host ]; then host="${2:-}"; fi',
        '  case "$host" in',
    ]
    for host, binding in registry["hosts"].items():
        lines.append(f"    {shlex.quote(host)}) python_bin={shlex.quote(binding['python'])} ;;")
    lines.extend(
        [
            "    *) python_bin=python3 ;;",
            "  esac",
            "fi",
            'exec "$python_bin" "${0%/*}/../launcher.py" "$@"',
            "",
        ]
    )
    return "\n".join(lines).encode()


@contextmanager
def _setup_lock(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "setup.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_plan(plan: Plan) -> Path | None:
    with _setup_lock(plan.state):
        return _apply_plan(plan)


def _apply_plan(plan: Plan) -> Path | None:
    changed = [
        change
        for change in plan.changes
        if change.before != change.after or change.before_mode != change.mode
    ]
    for change in plan.changes:
        if (
            change.path.is_symlink()
            or read(change.path) != change.before
            or (
                change.path.exists()
                and stat.S_IMODE(change.path.stat().st_mode) != change.before_mode
            )
        ):
            raise SetupError("Setup files changed after preview; create a fresh plan")
    if not changed:
        return None
    transaction = plan.state / "setup-history" / f"{uuid.uuid4().hex}.json"
    records = [
        {
            "path": str(c.path),
            "before": base64.b64encode(c.before).decode() if c.before is not None else None,
            "after_sha256": digest(c.after),
            "mode": c.mode,
            "before_mode": c.before_mode,
        }
        for c in changed
    ]
    atomic_write(transaction, json_bytes({"version": 1, "files": records}), 0o600)
    applied = []
    try:
        for change in changed:
            atomic_write(change.path, change.after, change.mode)
            applied.append(change)
    except BaseException:
        for change in reversed(applied):
            if change.before is None:
                change.path.unlink(missing_ok=True)
            else:
                atomic_write(
                    change.path,
                    change.before,
                    change.before_mode if change.before_mode is not None else change.mode,
                )
        raise
    return transaction


def rollback(transaction: str | Path) -> None:
    with _setup_lock(Path(transaction).parent.parent):
        _rollback(transaction)


def _rollback(transaction: str | Path) -> None:
    records = json.loads(Path(transaction).read_text())["files"]
    for record in records:
        path = Path(record["path"])
        if (
            path.is_symlink()
            or digest(read(path)) != record["after_sha256"]
            or (path.exists() and stat.S_IMODE(path.stat().st_mode) != record["mode"])
        ):
            raise SetupError("Files changed after setup; rollback would overwrite later changes")
    for record in reversed(records):
        path = Path(record["path"])
        if record["before"] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(
                path,
                base64.b64decode(record["before"]),
                record.get("before_mode") or record["mode"],
            )


def doctor(*, home: str | Path, host: str, config: str | None = None) -> dict[str, Any]:
    home = Path(home).expanduser().absolute()
    state = location(home)
    registry = json.loads((state / "installations.json").read_text())
    root = resolve_root(registry, host)
    path = config_path(host, home, config)
    raw = path.read_text()
    doc = tomlkit.parse(raw) if host in ("codex", "grok") else json.loads(_json_clean(raw))
    entry = doc.get("mcp_servers" if host in ("codex", "grok") else "mcpServers", {}).get(SERVER)
    expected = {"command": str(state / "bin/wise-engine"), "args": ["--wise-host", host, "mcp"]}
    return {
        "host": host,
        "plugin_root": str(root),
        "config": str(path),
        "registration_ok": bool(
            entry
            and all(entry.get(k) == v for k, v in expected.items())
            and entry.get("env", {}).get("WISE_PYTHON") == registry["hosts"][host]["python"]
            and entry.get("env", {}).get("WISE_HOST") == host
            and not entry.get("env", {}).get("WISE_PLUGIN_ROOT")
        ),
        "launcher_ok": bool(
            os.access(state / "bin/wise-engine", os.X_OK)
            and (state / "launcher.py").is_file()
            and os.access(state / "launcher.py", os.R_OK)
            and os.access(registry["hosts"][host]["python"], os.X_OK)
        ),
        "config_sha256": digest(path.read_bytes()),
        "install_version": json.loads((root / ".claude-plugin/plugin.json").read_text()).get(
            "version"
        ),
        "host_verified": False,
    }


def _server_entry(host: str, content: bytes) -> Any:
    text = content.decode()
    doc = tomlkit.parse(text) if host in ("codex", "grok") else json.loads(_json_clean(text))
    return doc.get("mcp_servers" if host in ("codex", "grok") else "mcpServers", {}).get(SERVER)


def refresh_existing(
    *, plugin_root: str | Path, host: str, home: str | Path, config: str | None = None
) -> dict[str, Any]:
    home = Path(home).expanduser().absolute()
    root = validate_root(plugin_root)
    registry_path = location(home) / "installations.json"
    try:
        registry_before = registry_path.read_bytes()
        registry = json.loads(registry_before)
        binding = registry["hosts"][host]
        target = Path(binding["config"])
        if config is not None and config_path(host, home, config) != target:
            raise ValueError("config target differs from the registered target")
        config_before = target.read_bytes()
        current_entry = _server_entry(host, config_before)
        if digest(json_bytes(current_entry)) != binding["entry_sha256"]:
            raise ValueError("Wise server entry changed after setup")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise SetupError(
            "Automatic refresh requires an unchanged Wise registration; run explicit setup"
        ) from error
    version = json.loads((root / ".claude-plugin/plugin.json").read_text()).get("version")
    if binding["plugin_root"] == str(root) and binding.get("install_version") == version:
        return {"refreshed": False, "transaction": None, "plugin_root": str(root)}
    plan = plan_setup(
        plugin_root=root,
        host=host,
        home=home,
        config=str(target),
        source=binding.get("source"),
        python=binding["python"],
    )
    for change in plan.changes:
        expected = (
            registry_before
            if change.path == registry_path
            else config_before
            if change.path == target
            else None
        )
        if expected is not None and change.before != expected:
            raise SetupError("Setup files changed during automatic refresh; retry")
        if change.path == registry_path:
            updated = json.loads(change.after)
            updated["default_host"] = registry.get("default_host", host)
            change.after = json_bytes(updated)
        elif change.path == location(home) / "bin/wise-engine":
            updated_registry = json.loads(
                next(c.after for c in plan.changes if c.path == registry_path)
            )
            updated_registry["default_host"] = registry.get("default_host", host)
            change.after = _launcher_script(updated_registry)
    transaction = apply_plan(plan)
    return {
        "refreshed": transaction is not None,
        "transaction": str(transaction) if transaction else None,
        "plugin_root": str(root),
    }
