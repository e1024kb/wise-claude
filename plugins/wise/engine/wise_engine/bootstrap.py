from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

from .paths import ENGINE_ROOT, plugin_data_root


class BootstrapError(RuntimeError):
    pass


def environment_key(requirements: Path) -> str:
    digest = hashlib.sha256(requirements.read_bytes())
    digest.update(sys.version.encode())
    digest.update(sysconfig.get_platform().encode())
    digest.update(str(Path(sys.executable).resolve()).encode())
    return digest.hexdigest()[:24]


def environment_path(requirements: Path, data_root: Path) -> Path:
    return data_root / "python" / environment_key(requirements)


def is_ready(target: Path, requirements: Path) -> bool:
    try:
        if target.is_symlink():
            return False
        marker = json.loads((target / ".ready.json").read_text())
        return marker == {"key": environment_key(requirements)} and os.access(
            target / "bin/python", os.X_OK
        )
    except (OSError, ValueError):
        return False


def ensure_environment(
    requirements: Path,
    data_root: Path,
    *,
    probe: bool = False,
) -> Path:
    if sys.version_info < (3, 11):
        raise BootstrapError("Python 3.11 or newer is required")
    target = environment_path(requirements, data_root)
    if is_ready(target, requirements):
        return target / "bin/python"
    if probe:
        raise BootstrapError("Python engine dependencies are not installed")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / f"{target.name}.lock"
    with lock.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if is_ready(target, requirements):
            return target / "bin/python"
        if target.is_symlink():
            raise BootstrapError("Managed environment path must not be a symlink")
        if target.exists():
            shutil.rmtree(target)
        try:
            print("wise-engine: installing Python dependencies", file=sys.stderr)
            venv.EnvBuilder(with_pip=True).create(target)
            interpreter = target / "bin/python"
            result = subprocess.run(
                [
                    str(interpreter),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "-r",
                    str(requirements),
                ],
                stdout=sys.stderr,
                stderr=sys.stderr,
                check=False,
            )
            if result.returncode:
                raise BootstrapError(f"Dependency installation failed (exit {result.returncode})")
            marker = target / ".ready.json.tmp"
            marker.write_text(json.dumps({"key": environment_key(requirements)}))
            marker.replace(target / ".ready.json")
            return interpreter
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        interpreter = ensure_environment(
            ENGINE_ROOT / "requirements.txt", plugin_data_root(), probe=args.probe
        )
    except (BootstrapError, OSError) as exc:
        print(f"wise-engine: {exc}", file=sys.stderr)
        return 69
    if args.probe:
        print(str(interpreter))
        return 0
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ENGINE_ROOT)
    os.execve(str(interpreter), [str(interpreter), "-m", "wise_engine", *arguments], environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
