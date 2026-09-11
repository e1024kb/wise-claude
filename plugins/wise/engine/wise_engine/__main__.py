from __future__ import annotations

import sys

from .version import runtime_version, source_build_id


def main() -> int:
    if sys.argv[1:] == ["version"]:
        print(f"wise-engine {source_build_id()} (python {runtime_version()})")
        return 0
    print("wise-engine: Python migration in progress; only version is available", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
