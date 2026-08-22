#!/usr/bin/env python3
"""
Focused telemetry probe for a single MeshCore repeater.

This script connects directly to the local companion, loads contacts, and then
tries several request/path combinations against a target repeater so we can see
which flow actually returns data.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from meshcore import MeshCore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "meshcore_bot.db"


def _read_tracked_path(db_path: str, public_key: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT out_path, out_path_len, out_bytes_per_hop
            FROM complete_contact_tracking
            WHERE lower(public_key) = lower(?)
               OR lower(public_key) LIKE lower(?) || '%'
            ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
            LIMIT 1
            """,
            (public_key, public_key),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _apply_manual_path(contact: Dict[str, Any], path_hex: str) -> Dict[str, Any]:
    clone = copy.deepcopy(contact)
    normalized = path_hex.replace(",", "").strip().lower()
    clone["out_path"] = normalized
    clone["out_path_len"] = max(1, len(normalized) // 2)
    clone["out_path_hash_mode"] = 0
    return clone


def _apply_tracked_path(contact: Dict[str, Any], tracked: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    clone = copy.deepcopy(contact)
    if not tracked or not tracked.get("out_path"):
        clone["out_path"] = ""
        clone["out_path_len"] = -1
        clone["out_path_hash_mode"] = 0
        return clone
    clone["out_path"] = tracked["out_path"]
    clone["out_path_len"] = tracked["out_path_len"]
    bytes_per_hop = tracked.get("out_bytes_per_hop") or 1
    clone["out_path_hash_mode"] = max(0, int(bytes_per_hop) - 1)
    return clone


def _apply_flood(contact: Dict[str, Any]) -> Dict[str, Any]:
    clone = copy.deepcopy(contact)
    clone["out_path"] = ""
    clone["out_path_len"] = -1
    clone["out_path_hash_mode"] = 0
    return clone


async def _try_status(mc: MeshCore, contact: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    return await mc.commands.req_status_sync(contact, timeout=timeout)


async def _try_telemetry(mc: MeshCore, contact: Dict[str, Any], timeout: float) -> Optional[Any]:
    return await mc.commands.req_telemetry_sync(contact, timeout=timeout)


async def _try_login(mc: MeshCore, contact: Dict[str, Any], timeout: float) -> bool:
    res = await mc.commands.send_login_sync(contact, "", timeout=timeout)
    return res is not None


def _contact_summary(contact: Dict[str, Any]) -> str:
    return (
        f"out_path={contact.get('out_path')!r} "
        f"out_path_len={contact.get('out_path_len')!r} "
        f"out_path_hash_mode={contact.get('out_path_hash_mode')!r}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="Repeater name or key prefix")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--manual-path", default="", help="Optional manual path hex, e.g. 7d or 02")
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--post-login-retries", type=int, default=3)
    parser.add_argument("--post-login-delay", type=float, default=3.0)
    parser.add_argument(
        "--variant",
        choices=("live", "flood", "tracked", "manual", "all"),
        default="all",
    )
    parser.add_argument("--discover-first", action="store_true")
    parser.add_argument("--reset-first", action="store_true")
    args = parser.parse_args()

    print(f"connecting to {args.port} at {args.baud} baud", flush=True)
    mc = await MeshCore.create_serial(args.port, baudrate=args.baud, debug=False, only_error=False)
    if mc is None:
        print("failed: unable to connect to local companion", file=sys.stderr)
        return 2

    try:
        print("connected; loading contacts", flush=True)
        await mc.ensure_contacts()
        print("contacts loaded", flush=True)
        contact = mc.get_contact_by_name(args.target) or mc.get_contact_by_key_prefix(args.target)
        if not contact:
            print(f"failed: contact not found for {args.target}", file=sys.stderr)
            return 3

        print("base_contact", json.dumps({
            "name": contact.get("adv_name"),
            "public_key": contact.get("public_key"),
            "out_path": contact.get("out_path"),
            "out_path_len": contact.get("out_path_len"),
            "out_path_hash_mode": contact.get("out_path_hash_mode"),
        }, indent=2), flush=True)

        tracked = _read_tracked_path(args.db_path, contact["public_key"])
        print("tracked_path", json.dumps(tracked, indent=2), flush=True)

        variants = [
            ("live", copy.deepcopy(contact)),
            ("flood", _apply_flood(contact)),
            ("tracked", _apply_tracked_path(contact, tracked)),
        ]
        if args.manual_path.strip():
            variants.append(("manual", _apply_manual_path(contact, args.manual_path)))
        if args.variant != "all":
            variants = [item for item in variants if item[0] == args.variant]

        for label, variant in variants:
            print(f"\n=== variant: {label} ===", flush=True)
            print(_contact_summary(variant), flush=True)
            if args.reset_first:
                try:
                    reset_res = await mc.commands.reset_path(variant)
                    print("reset_path", getattr(reset_res, "payload", None), flush=True)
                    variant["out_path"] = ""
                    variant["out_path_len"] = -1
                    variant["out_path_hash_mode"] = 0
                except Exception as exc:
                    print(f"reset_path exception: {exc}", flush=True)
            if args.discover_first:
                try:
                    disc_res = await mc.commands.send_path_discovery_sync(variant, timeout=args.timeout)
                    disc_payload = disc_res.payload if disc_res is not None else None
                    print("discover_path", json.dumps(disc_payload, indent=2) if disc_payload else "None", flush=True)
                    if disc_payload and disc_payload.get("out_path"):
                        variant["out_path"] = disc_payload["out_path"]
                        variant["out_path_len"] = max(1, len(disc_payload["out_path"]) // 2)
                        variant["out_path_hash_mode"] = 0
                        print("post_discovery_contact", _contact_summary(variant), flush=True)
                except Exception as exc:
                    print(f"discover_path exception: {exc}", flush=True)
            for attempt in range(1, args.retries + 1):
                print(f"-- attempt {attempt}/{args.retries}", flush=True)
                status = await _try_status(mc, variant, args.timeout)
                print("status", json.dumps(status, indent=2) if status else "None", flush=True)
                telemetry = await _try_telemetry(mc, variant, args.timeout)
                print("telemetry", json.dumps(telemetry, indent=2) if telemetry else "None", flush=True)
                login_ok = await _try_login(mc, variant, args.timeout)
                print("login", login_ok, flush=True)
                if login_ok:
                    for post_attempt in range(1, args.post_login_retries + 1):
                        status_after_login = await _try_status(mc, variant, args.timeout)
                        print(
                            f"status_after_login[{post_attempt}]",
                            json.dumps(status_after_login, indent=2) if status_after_login else "None",
                            flush=True,
                        )
                        telemetry_after_login = await _try_telemetry(mc, variant, args.timeout)
                        print(
                            f"telemetry_after_login[{post_attempt}]",
                            json.dumps(telemetry_after_login, indent=2) if telemetry_after_login else "None",
                            flush=True,
                        )
                        if status_after_login is not None or telemetry_after_login is not None:
                            break
                        if post_attempt < args.post_login_retries:
                            await asyncio.sleep(args.post_login_delay)
                    try:
                        await mc.commands.send_logout(variant)
                    except Exception:
                        pass
                await asyncio.sleep(2)
    finally:
        await mc.disconnect()

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
