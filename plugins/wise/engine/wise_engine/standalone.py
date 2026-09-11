from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ledger, profile, supervision
from .paths import runs_root

LEGACY_RUN_MESSAGE = (
    "unsupported legacy run; migrate its workflow definition and start a new run "
    "after reviewing already-completed side effects"
)


def _legacy_notices(root: Path) -> None:
    if root.is_dir():
        for state in sorted(root.glob("*/state.yaml")):
            print(f"LEGACY-RUN:{state.parent.name}: {LEGACY_RUN_MESSAGE}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wise session, profile, history and supervision helpers"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "runs-root",
        "current-session-id",
        "profile-get",
        "supervise-config",
        "list-resumable-runs",
        "prune-runs",
    ):
        commands.add_parser(name)
    for name, arguments in (
        ("profile-set", ("level",)),
        ("session-path", ("session_id",)),
        ("session-label", ("run_id", "workflow_name")),
        ("find-runs-by-session", ("session_id",)),
        ("list-runs", ("runs_root",)),
        ("dump-state", ("state_path",)),
    ):
        command = commands.add_parser(name)
        for argument in arguments:
            command.add_argument(argument)
    command = commands.add_parser("worker-heartbeat")
    command.add_argument("run_dir")
    command.add_argument("name")
    command.add_argument("phase", nargs="?", default="")
    command.add_argument("task", nargs="?", default="")
    command = commands.add_parser("stale-workers")
    command.add_argument("run_dir")
    command.add_argument("expected", nargs="?", default="")
    args = parser.parse_args(argv)
    if args.command == "runs-root":
        print(runs_root())
    elif args.command == "current-session-id":
        print(profile.current_session_id())
    elif args.command == "profile-get":
        print(profile.profile_get())
    elif args.command == "profile-set":
        result = profile.profile_set(args.level)
        if not result["ok"]:
            print(result["message"], file=sys.stderr)
            return 2
        print(f"PROFILE: level={result['level']} scope=session session={result['session']}")
    elif args.command == "session-path":
        transcript = profile.session_path(args.session_id)
        if transcript is None:
            return 2
        print(transcript)
    elif args.command == "session-label":
        print(profile.session_label(args.run_id, args.workflow_name))
    elif args.command == "supervise-config":
        print(json.dumps(supervision.supervise_config()))
    elif args.command == "worker-heartbeat":
        try:
            supervision.worker_heartbeat(args.run_dir, args.name, args.phase, args.task)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            return 2
    elif args.command == "stale-workers":
        for row in supervision.stale_workers(args.run_dir, args.expected):
            print(supervision.format_stale_worker(row))
    elif args.command == "dump-state":
        path = Path(args.state_path)
        directory = path if path.is_dir() else path.parent
        if path.name == "state.yaml" or (
            not (directory / "state.json").is_file() and (directory / "state.yaml").is_file()
        ):
            print(f"LEGACY-RUN:{directory.name}: {LEGACY_RUN_MESSAGE}", file=sys.stderr)
            return 2
        print(ledger.dump_state(directory))
    else:
        root = Path(args.runs_root) if args.command == "list-runs" else runs_root()
        _legacy_notices(root)
        if args.command == "list-runs":
            print(ledger.format_runs_table(ledger.list_runs(root)))
        elif args.command == "list-resumable-runs":
            print(json.dumps(ledger.list_resumable_runs(root), indent=2))
        elif args.command == "find-runs-by-session":
            for row in ledger.find_runs_by_session(root, args.session_id):
                print(ledger.format_session_run_row(row))
        elif args.command == "prune-runs":
            result = ledger.prune_runs(root)
            for run_id in result["pruned"]:
                print(f"PRUNED:{run_id}")
            for failure in result["failed"]:
                print(f"PRUNE-FAILED:{failure['run_id']}:{failure['reason']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
