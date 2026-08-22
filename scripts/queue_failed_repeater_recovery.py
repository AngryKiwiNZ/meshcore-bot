#!/usr/bin/env python3
"""Queue one bounded Poll Now recovery for each currently failed repeater.

The running bot remains the sole owner of the serial connection.  This helper
feeds its existing trigger file one target at a time and waits for the resulting
database sample before proceeding, so the monitor's configured quiet interval
and per-device request budget remain in force.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _latest_failed(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT node_key, MAX(id) AS id
                FROM repeater_monitor_samples
                GROUP BY node_key
            )
            SELECT sample.node_key, sample.display_name, sample.id
            FROM repeater_monitor_samples AS sample
            JOIN latest ON latest.id = sample.id
            WHERE COALESCE(sample.status_ok, 0) = 0
            ORDER BY lower(sample.display_name), sample.node_key
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _latest_sample(db_path: Path, node_key: str) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, collected_at, status_ok, login_ok, login_attempts,
                   poll_duration_seconds, error_text, temperature_c, battery_mv
            FROM repeater_monitor_samples
            WHERE node_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (node_key,),
        ).fetchone()
    return dict(row) if row else None


def _read_status(status_path: Path) -> dict[str, Any]:
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _wait_until_available(
    trigger_path: Path,
    status_path: Path,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = _read_status(status_path)
        if not trigger_path.exists() and status.get("state") not in {
            "polling",
            "refresh_queued",
            "clock_sync_queued",
            "clock_sync",
        }:
            return True
        time.sleep(2)
    return False


def _queue_poll(trigger_path: Path, node_key: str) -> float:
    requested_at = time.time()
    payload = {
        "node_key": node_key,
        "action": "poll",
        "requested_at": requested_at,
    }
    trigger_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = trigger_path.with_suffix(trigger_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload), encoding="utf-8")
    temporary_path.replace(trigger_path)
    return requested_at


def _wait_for_sample(
    db_path: Path,
    node_key: str,
    previous_id: int,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        sample = _latest_sample(db_path, node_key)
        if sample and int(sample["id"]) > previous_id:
            return sample
        time.sleep(2)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "meshcore_bot.db")
    parser.add_argument(
        "--trigger",
        type=Path,
        default=PROJECT_ROOT / "data/repeater_monitor_refresh.trigger",
    )
    parser.add_argument(
        "--status",
        type=Path,
        default=PROJECT_ROOT / "data/repeater_monitor_status.json",
    )
    parser.add_argument("--availability-timeout", type=float, default=300.0)
    parser.add_argument("--result-timeout", type=float, default=240.0)
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Node-key prefix to omit; may be supplied more than once",
    )
    args = parser.parse_args()

    failed = _latest_failed(args.db)
    excluded = [str(value).strip().lower() for value in args.exclude if str(value).strip()]
    if excluded:
        failed = [
            target
            for target in failed
            if not any(str(target["node_key"]).lower().startswith(prefix) for prefix in excluded)
        ]
    print(f"Recovery queue contains {len(failed)} failed repeaters", flush=True)
    recovered = 0
    completed = 0

    for index, target in enumerate(failed, start=1):
        node_key = str(target["node_key"])
        name = str(target.get("display_name") or node_key[:12])
        previous_id = int(target["id"])
        print(f"[{index}/{len(failed)}] waiting to queue {name}", flush=True)
        if not _wait_until_available(
            args.trigger,
            args.status,
            args.availability_timeout,
        ):
            print(f"[{index}/{len(failed)}] skipped {name}: monitor stayed busy", flush=True)
            continue

        _queue_poll(args.trigger, node_key)
        print(f"[{index}/{len(failed)}] queued {name}", flush=True)
        sample = _wait_for_sample(
            args.db,
            node_key,
            previous_id,
            args.result_timeout,
        )
        if sample is None:
            print(f"[{index}/{len(failed)}] no result received for {name}", flush=True)
            continue

        completed += 1
        status_ok = bool(sample.get("status_ok"))
        recovered += int(status_ok)
        print(
            f"[{index}/{len(failed)}] {name}: "
            f"status={'RECOVERED' if status_ok else 'failed'} "
            f"login={'ok' if sample.get('login_ok') else 'not-used/failed'} "
            f"duration={float(sample.get('poll_duration_seconds') or 0):.1f}s "
            f"error={sample.get('error_text') or 'none'}",
            flush=True,
        )

    print(
        f"Recovery complete: {recovered}/{completed} completed polls recovered "
        f"({len(failed) - completed} without a result)",
        flush=True,
    )
    return 0 if completed == len(failed) else 1


if __name__ == "__main__":
    sys.exit(main())
