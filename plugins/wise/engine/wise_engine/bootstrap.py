from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import threading
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


def _run_installer(command: list[str]) -> None:
    if threading.current_thread() is not threading.main_thread():
        raise BootstrapError("Dependency installation must run on the main thread")
    previous = signal.getsignal(signal.SIGTERM)
    process = None

    def terminate(signum: int, frame: object) -> None:
        raise BootstrapError("Dependency installation interrupted")

    signal.signal(signal.SIGTERM, terminate)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            start_new_session=True,
        )
        code = process.wait()
        if code:
            raise BootstrapError(f"Dependency installation failed (exit {code})")
    except BaseException:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


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
            venv.EnvBuilder(with_pip=False).create(target)
            interpreter = target / "bin/python"
            _run_installer([str(interpreter), "-m", "ensurepip", "--upgrade"])
            _run_installer(
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
                ]
            )
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
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--script", type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        interpreter = ensure_environment(
            ENGINE_ROOT / "requirements.txt", plugin_data_root(), probe=args.probe
        )
    except (BootstrapError, OSError) as exc:
        print(f"wise-engine: {exc}", file=sys.stderr)
        return 69
    if args.probe or args.prepare:
        print(str(interpreter))
        return 0
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ENGINE_ROOT)
    environment["WISE_ENGINE_BASE_PYTHON"] = sys.executable
    if args.script is not None:
        command = [str(interpreter), str(args.script.resolve()), *arguments]
    else:
        command = [str(interpreter), "-m", "wise_engine", *arguments]
    os.execve(str(interpreter), command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
