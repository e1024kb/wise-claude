from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import re
from pathlib import Path


ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text())
    server = config["server"]
    child_env = dict(os.environ)
    resolved_env = {}
    for key, value in server.get("env", {}).items():
        match = ENV_REF.fullmatch(value) if isinstance(value, str) else None
        resolved_env[key] = child_env[match[1]] if match else value
    child = subprocess.Popen(
        [server["command"], *server.get("args", [])],
        cwd=server.get("cwd") or config["cwd"],
        env={**child_env, **resolved_env},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert child.stdin and child.stdout
    initialize: list[object] = []

    def forward() -> None:
        try:
            for line in sys.stdin.buffer:
                message = json.loads(line)
                if message.get("method") == "initialize":
                    initialize.append(message.get("id"))
                assert child.stdin
                child.stdin.write(line)
                child.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        finally:
            if child.poll() is None:
                child.terminate()

    threading.Thread(target=forward, daemon=True).start()
    try:
        for line in child.stdout:
            message = json.loads(line)
            if message.get("id") in initialize and "result" in message:
                fd = os.open(config["ready"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                initialize.clear()
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


if __name__ == "__main__":
    main()
