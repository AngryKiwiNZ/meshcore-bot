#!/usr/bin/env python3
"""
Check a remote MeshCore repeater clock against this machine's UTC clock.

This script connects to the local MeshCore companion node, resolves a remote
target repeater by name or public-key prefix, then retries the remote basic
clock request until it either succeeds or exhausts the configured attempts.

Notes:
- It needs exclusive access to the local MeshCore connection while it runs.
- The current Python MeshCore stack is much more reliable with `req_basic_sync`
  than with remote CLI `cmd <node> clock` replies, so this script uses the
  basic clock request path.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from meshcore import EventType, MeshCore


@dataclass
class ClockCheckResult:
    target_name: str
    target_public_key: str
    login_ok: bool
    login_permissions: Optional[int]
    login_is_admin: Optional[bool]
    login_server_epoch: Optional[int]
    login_attempts_used: int
    clock_ok: bool
    clock_attempts_used: int
    pi_time_epoch: float
    remote_time_epoch: Optional[int]
    drift_seconds: Optional[float]
    error: Optional[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check a remote MeshCore repeater clock against Pi UTC time."
    )
    parser.add_argument(
        "target",
        help="Remote repeater name, exact/substring match, or public-key prefix.",
    )
    parser.add_argument(
        "-s",
        "--serial",
        default="/dev/ttyACM0",
        help="Serial device for the local MeshCore companion node.",
    )
    parser.add_argument(
        "--password",
        default="",
        help="Remote repeater login password. Empty string is common for guest access.",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip the remote login step and try clock requests directly.",
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Use the server timestamp embedded in the login response and skip a separate clock request.",
    )
    parser.add_argument(
        "--login-attempts",
        type=int,
        default=4,
        help="How many login attempts to make before giving up.",
    )
    parser.add_argument(
        "--clock-attempts",
        type=int,
        default=6,
        help="How many remote basic clock attempts to make before giving up.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Delay in seconds between retries.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Timeout in seconds for remote requests.",
    )
    parser.add_argument(
        "--min-timeout",
        type=float,
        default=10.0,
        help="Minimum timeout in seconds for remote requests.",
    )
    parser.add_argument(
        "--warn-drift-seconds",
        type=float,
        default=120.0,
        help="Drift threshold above which the result is considered too high.",
    )
    parser.add_argument(
        "--fixed-path",
        default="",
        help="Optional fixed out_path to force on the contact before requests, e.g. '02' or '02,7d'.",
    )
    parser.add_argument(
        "--clock-method",
        choices=("basic", "cli"),
        default="basic",
        help="Clock fetch method: binary basic request or repeater CLI 'clock' command.",
    )
    return parser.parse_args()


def format_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def resolve_contact(contacts: Dict[str, Dict[str, Any]], target: str) -> Dict[str, Any]:
    target_lower = target.strip().lower()
    exact_name = []
    partial_name = []
    prefix_matches = []

    for contact in contacts.values():
        public_key = contact.get("public_key", "")
        name = contact.get("adv_name", "")
        if public_key.lower().startswith(target_lower):
            prefix_matches.append(contact)
        if name.lower() == target_lower:
            exact_name.append(contact)
        elif target_lower in name.lower():
            partial_name.append(contact)

    candidates = exact_name or prefix_matches or partial_name
    if not candidates:
        raise ValueError(f"No contact matched target {target!r}")
    if len(candidates) > 1:
        names = ", ".join(
            f"{c.get('adv_name', '<unknown>')} ({c.get('public_key', '')[:12]})"
            for c in candidates[:5]
        )
        raise ValueError(
            f"Target {target!r} is ambiguous; matching contacts: {names}"
        )
    return candidates[0]


def normalize_fixed_path(raw_value: str) -> Optional[Tuple[str, int]]:
    raw = (raw_value or "").strip().lower()
    if not raw:
        return None

    if ":" in raw:
        raw, _, mode_text = raw.rpartition(":")
        try:
            path_hash_mode = int(mode_text)
        except ValueError as exc:
            raise ValueError(f"Invalid fixed path {raw_value!r}: bad path hash mode") from exc
        bytes_per_hop = path_hash_mode + 1
    else:
        bytes_per_hop = 0

    chunks = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if not chunks:
        raise ValueError(f"Invalid fixed path {raw_value!r}: no path chunks")

    if bytes_per_hop == 0:
        if len(chunks) > 1:
            first_len = len(chunks[0])
            if any(len(chunk) != first_len for chunk in chunks):
                raise ValueError(
                    f"Invalid fixed path {raw_value!r}: comma-separated chunks must have equal widths"
                )
            if first_len % 2 != 0:
                raise ValueError(f"Invalid fixed path {raw_value!r}: chunk width must be even hex length")
            bytes_per_hop = first_len // 2
        else:
            if len(chunks[0]) % 2 != 0:
                raise ValueError(f"Invalid fixed path {raw_value!r}: must be even-length hex")
            bytes_per_hop = 1

    normalized = "".join(chunks)
    if any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError(f"Invalid fixed path {raw_value!r}: must contain only hex characters")
    if len(normalized) % (bytes_per_hop * 2) != 0:
        raise ValueError(
            f"Invalid fixed path {raw_value!r}: total path length is not a multiple of hop width"
        )
    return normalized, bytes_per_hop


def apply_fixed_path(contact: Dict[str, Any], raw_path: str) -> Optional[str]:
    normalized = normalize_fixed_path(raw_path)
    if not normalized:
        return None
    fixed_path, bytes_per_hop = normalized
    contact["out_path"] = fixed_path
    contact["out_path_len"] = max(1, len(fixed_path) // (bytes_per_hop * 2))
    contact["out_path_hash_mode"] = max(0, bytes_per_hop - 1)
    return fixed_path


async def attempt_login(
    mc: MeshCore,
    contact: Dict[str, Any],
    password: str,
    attempts: int,
    retry_delay: float,
    timeout: float,
    min_timeout: float,
) -> tuple[bool, int, Optional[int], Optional[bool], Optional[int]]:
    for attempt in range(1, attempts + 1):
        result = await mc.commands.send_login_sync(
            contact,
            password,
            timeout=timeout,
            min_timeout=min_timeout,
        )
        if result is not None:
            payload = getattr(result, "payload", {}) or {}
            return (
                True,
                attempt,
                payload.get("permissions"),
                payload.get("is_admin"),
                payload.get("server_timestamp"),
            )
        if attempt < attempts:
            await asyncio.sleep(retry_delay)
    return False, attempts, None, None, None


async def attempt_clock_request(
    mc: MeshCore,
    contact: Dict[str, Any],
    attempts: int,
    retry_delay: float,
    timeout: float,
    min_timeout: float,
) -> tuple[Optional[int], Optional[float], int, Optional[str]]:
    last_error = None

    for attempt in range(1, attempts + 1):
        started = time.time()
        payload = await mc.commands.req_basic_sync(
            contact,
            timeout=timeout,
            min_timeout=min_timeout,
        )
        finished = time.time()

        if payload and payload.get("data"):
            try:
                remote_epoch = int.from_bytes(
                    bytes.fromhex(payload["data"][0:8]),
                    byteorder="little",
                    signed=False,
                )
                midpoint = (started + finished) / 2.0
                return remote_epoch, float(remote_epoch - midpoint), attempt, None
            except (TypeError, ValueError) as exc:
                last_error = f"clock_parse_error:{exc}"
        else:
            last_error = "clock_response_missing"

        if attempt < attempts:
            await asyncio.sleep(retry_delay)

    return None, None, attempts, last_error


def _extract_epoch_from_cli_text(text: str) -> Optional[int]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("> "):
        text = text[2:].strip()

    iso_match = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", text)
    if iso_match:
        timestamp = f"{iso_match.group(1)} {iso_match.group(2)}"
        parsed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        return int(parsed.timestamp())

    cli_match = re.search(
        r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*UTC",
        text,
    )
    if not cli_match:
        return None
    hour, minute, day, month, year = map(int, cli_match.groups())
    try:
        parsed = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp())


async def _fetch_contact_cli_response(
    mc: MeshCore,
    target_pubkey_prefix: str,
    timeout: float,
) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = await mc.commands.get_msg(timeout=2.0)
        except TimeoutError:
            continue
        except Exception:
            await asyncio.sleep(0.5)
            continue

        if result.type.value == "no_more_messages":
            await asyncio.sleep(0.5)
            continue
        if result.type.value == "error":
            await asyncio.sleep(0.5)
            continue
        if result.type.value != "contact_message":
            continue

        payload = getattr(result, "payload", {}) or {}
        msg_prefix = str(payload.get("pubkey_prefix", "")).lower()
        txt_type = payload.get("txt_type", 0)
        if msg_prefix == target_pubkey_prefix.lower() and txt_type == 1:
            return payload
    return None


async def attempt_clock_cli_request(
    mc: MeshCore,
    contact: Dict[str, Any],
    attempts: int,
    retry_delay: float,
    timeout: float,
) -> tuple[Optional[int], Optional[float], int, Optional[str]]:
    last_error = None
    pubkey = contact.get("public_key", "")
    pubkey_prefix = pubkey[:12]

    for attempt in range(1, attempts + 1):
        started = time.time()
        send_result = await mc.commands.send_cmd(pubkey, "clock")
        if getattr(send_result, "type", None).value == "error":
            last_error = "clock_cli_send_failed"
        else:
            response_payload = await _fetch_contact_cli_response(mc, pubkey_prefix, timeout)
            finished = time.time()
            if response_payload is not None:
                remote_epoch = _extract_epoch_from_cli_text(str(response_payload.get("text", "")))
                if remote_epoch is not None:
                    midpoint = (started + finished) / 2.0
                    return remote_epoch, float(remote_epoch - midpoint), attempt, None
                last_error = "clock_cli_parse_failed"
            else:
                last_error = "clock_cli_response_missing"

        if attempt < attempts:
            await asyncio.sleep(retry_delay)

    return None, None, attempts, last_error


async def run_check(args: argparse.Namespace) -> ClockCheckResult:
    mc = await MeshCore.create_serial(args.serial, debug=False)
    await mc.commands.send_device_query()
    contacts_result = await mc.commands.get_contacts()
    if (
        contacts_result is None
        or getattr(contacts_result, "type", None) == EventType.ERROR
    ):
        raise RuntimeError("Unable to load contacts from the local MeshCore node")

    contact = resolve_contact(mc.contacts, args.target)
    apply_fixed_path(contact, args.fixed_path)

    login_ok = False
    login_attempts_used = 0
    login_permissions = None
    login_is_admin = None
    login_server_epoch = None
    error = None

    if not args.skip_login:
        (
            login_ok,
            login_attempts_used,
            login_permissions,
            login_is_admin,
            login_server_epoch,
        ) = await attempt_login(
            mc=mc,
            contact=contact,
            password=args.password,
            attempts=max(1, args.login_attempts),
            retry_delay=max(0.0, args.retry_delay),
            timeout=max(1.0, args.timeout),
            min_timeout=max(0.0, args.min_timeout),
        )
        if not login_ok:
            error = "login_failed"

    if args.login_only:
        remote_epoch = int(login_server_epoch) if login_server_epoch is not None else None
        drift_seconds = (
            float(remote_epoch - time.time()) if remote_epoch is not None else None
        )
        clock_attempts_used = 0
        clock_error = None if remote_epoch is not None else "login_clock_missing"
    elif args.clock_method == "cli":
        remote_epoch, drift_seconds, clock_attempts_used, clock_error = await attempt_clock_cli_request(
            mc=mc,
            contact=contact,
            attempts=max(1, args.clock_attempts),
            retry_delay=max(0.0, args.retry_delay),
            timeout=max(1.0, args.timeout),
        )
    else:
        remote_epoch, drift_seconds, clock_attempts_used, clock_error = await attempt_clock_request(
            mc=mc,
            contact=contact,
            attempts=max(1, args.clock_attempts),
            retry_delay=max(0.0, args.retry_delay),
            timeout=max(1.0, args.timeout),
            min_timeout=max(0.0, args.min_timeout),
        )

    if remote_epoch is None:
        # New-style repeater login responses carry the server's own epoch and
        # are independent of CLI replay state, making this a reliable fallback.
        if login_server_epoch is not None:
            remote_epoch = int(login_server_epoch)
            drift_seconds = float(remote_epoch - time.time())
            error = None
        else:
            error = clock_error or error or "clock_failed"

    try:
        await mc.commands.send_logout(contact)
    except Exception:
        pass

    return ClockCheckResult(
        target_name=contact.get("adv_name", args.target),
        target_public_key=contact.get("public_key", ""),
        login_ok=login_ok if not args.skip_login else False,
        login_permissions=login_permissions,
        login_is_admin=login_is_admin,
        login_server_epoch=login_server_epoch,
        login_attempts_used=login_attempts_used,
        clock_ok=remote_epoch is not None,
        clock_attempts_used=clock_attempts_used,
        pi_time_epoch=time.time(),
        remote_time_epoch=remote_epoch,
        drift_seconds=drift_seconds,
        error=error,
    )


def print_result(result: ClockCheckResult, warn_drift_seconds: float) -> int:
    print(f"Target: {result.target_name} ({result.target_public_key[:12]})")
    print(f"Raspberry Pi UTC time: {format_utc(result.pi_time_epoch)}")
    if result.login_ok:
        access = "admin" if result.login_is_admin else "guest"
        print(f"Remote login access: {access} (permissions={result.login_permissions})")

    if result.remote_time_epoch is not None:
        print(f"Remote MeshCore UTC time: {format_utc(result.remote_time_epoch)}")
        print(f"Clock drift seconds: {result.drift_seconds:.1f}")
        if abs(result.drift_seconds or 0.0) <= warn_drift_seconds:
            print(f"Status: OK (drift within {warn_drift_seconds:.0f}s threshold)")
            return 0
        print(f"Status: TOO HIGH (drift exceeds {warn_drift_seconds:.0f}s threshold)")
        return 2

    print("Remote MeshCore UTC time: unavailable")
    if result.error:
        print(f"Status: FAILED ({result.error})")
    else:
        print("Status: FAILED")
    return 1


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run_check(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return print_result(result, warn_drift_seconds=args.warn_drift_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
