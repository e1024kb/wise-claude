from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ledger import env_positive_int, session_is_fresh, utc_now

WORKER_STALE_SECS_DEFAULT = 180
WORKER_POLL_SECS_DEFAULT = 30
WORKER_MAX_NUDGES_DEFAULT = 2
WORKER_MAX_RESPAWNS_DEFAULT = 1


def supervise_config(env: Mapping[str, str] | None = None) -> dict[str, int]:
    return {
        key: env_positive_int(name, default, env)
        for key, name, default in (
            ("stale_secs", "WISE_WORKER_STALE_SECS", WORKER_STALE_SECS_DEFAULT),
            ("poll_secs", "WISE_WORKER_POLL_SECS", WORKER_POLL_SECS_DEFAULT),
            ("max_nudges", "WISE_WORKER_MAX_NUDGES", WORKER_MAX_NUDGES_DEFAULT),
            ("max_respawns", "WISE_WORKER_MAX_RESPAWNS", WORKER_MAX_RESPAWNS_DEFAULT),
        )
    }


def worker_heartbeat(
    run_dir: str | Path, name: str, phase: str = "", task: str = "", *, now: datetime | None = None
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"INVALID:worker-name:{name!r} (must be a bare alphanumeric/_/- token)")
    workers = Path(run_dir) / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    line = utc_now(now)
    if phase:
        line += f"\tphase={phase}"
    if task:
        line += f"\ttask={task}"
    fd, temporary = tempfile.mkstemp(prefix=f"{name}.hb.", suffix=".tmp", dir=workers)
    try:
        try:
            stream = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with stream:
            stream.write(line + "\n")
        os.replace(temporary, workers / f"{name}.hb")
    except BaseException:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_heartbeat(path: Path) -> str | None:
    try:
        tokens = path.read_text().split()
        return tokens[0] if tokens else None
    except (OSError, UnicodeError):
        return None


def stale_workers(
    run_dir: str | Path,
    expected: str = "",
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    stale_after = supervise_config(env)["stale_secs"]
    workers = Path(run_dir) / "workers"
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for heartbeat in sorted(workers.glob("*.hb")):
        name = heartbeat.stem
        seen.add(name)
        stamp = read_heartbeat(heartbeat)
        if session_is_fresh(stamp, stale_after, current.timestamp() * 1000):
            continue
        age = None
        if stamp:
            try:
                date = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                age = int((current - date).total_seconds())
            except ValueError:
                pass
        rows.append(dict(name=name, stamp=stamp, status="stale", age=age))
    for raw in expected.split(","):
        name = raw.strip()
        if name and name not in seen:
            rows.append(dict(name=name, stamp=None, status="missing", age=None))
    return rows


def format_stale_worker(row: dict[str, Any]) -> str:
    return f"{row['name']}\t{row['stamp'] or 'NONE'}\t{row['status']}\t{row['age'] if row['age'] is not None else '?'}"
