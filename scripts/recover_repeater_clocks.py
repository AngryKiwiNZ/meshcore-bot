#!/usr/bin/env python3
"""Recover repeaters whose CLI replay timestamp was poisoned by a future clock.

This is an operator-only maintenance tool. It temporarily advances the local
companion clock so a remote ``clkreboot`` is accepted, reboots the companion to
clear its monotonic CLI timestamp, restores the companion to host UTC, and then
syncs and verifies every requested repeater.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from meshcore import EventType, MeshCore

from check_remote_repeater_clock import resolve_contact


SERIAL_DEFAULT = "/dev/ttyACM0"
ROLLOVER_EPOCH = 0xFFFFFFFF
COMMAND_TIMESTAMP_GUARD_SECONDS = 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+", help="Repeater names or public-key prefixes")
    parser.add_argument("--serial", default=SERIAL_DEFAULT)
    parser.add_argument(
        "--confirm-companion-clock-reset",
        action="store_true",
        help="Required acknowledgement that the companion will be rebooted and its RTC reset",
    )
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="Skip clkreboot recovery and sync a repeater whose replay state was reset externally",
    )
    parser.add_argument(
        "--replay-reset",
        action="store_true",
        help="Reset a poisoned remote replay counter using an explicit UINT32_MAX admin message",
    )
    return parser.parse_args()


async def connect(serial: str, attempts: int = 8) -> MeshCore:
    for attempt in range(1, attempts + 1):
        mc = await MeshCore.create_serial(serial, debug=False)
        if mc is not None:
            await mc.commands.get_contacts()
            return mc
        if attempt < attempts:
            await asyncio.sleep(3)
    raise RuntimeError("companion_connection_failed")


async def disconnect(mc: Optional[MeshCore]) -> None:
    if mc is not None:
        try:
            await mc.disconnect()
        except Exception:
            pass


async def login_admin(mc: MeshCore, contact: Dict[str, Any], label: str) -> int:
    for attempt in range(1, 4):
        event = await mc.commands.send_login_sync(contact, "", timeout=35, min_timeout=10)
        payload = getattr(event, "payload", {}) or {}
        if getattr(event, "type", None) == EventType.LOGIN_SUCCESS:
            if not payload.get("is_admin", False):
                raise RuntimeError(f"{label}: admin_access_required")
            server_timestamp = payload.get("server_timestamp")
            if server_timestamp is None:
                raise RuntimeError(f"{label}: login_clock_missing; meshcore>=2.3.8 required")
            print(
                f"{label}: admin login confirmed (attempt {attempt}, "
                f"server_epoch={server_timestamp})",
                flush=True,
            )
            return int(server_timestamp)
        if attempt == 1:
            print(f"{label}: direct login timed out; resetting contact to flood", flush=True)
            await mc.commands.reset_path(contact)
            contact["out_path"] = ""
            contact["out_path_len"] = -1
            contact["out_path_hash_mode"] = 0
        if attempt < 3:
            await asyncio.sleep(5)
    raise RuntimeError(f"{label}: login_failed")


async def fetch_cli_reply(
    mc: MeshCore,
    pubkey_prefix: str,
    matcher: Callable[[str], bool],
    timeout: float = 45,
) -> Optional[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = await mc.commands.get_msg(timeout=2)
        except TimeoutError:
            continue
        if getattr(event, "type", None) != EventType.CONTACT_MSG_RECV:
            await asyncio.sleep(0.25)
            continue
        payload = getattr(event, "payload", {}) or {}
        text = str(payload.get("text") or "").strip()
        if (
            str(payload.get("pubkey_prefix") or "").lower() == pubkey_prefix.lower()
            and payload.get("txt_type") == 1
            and matcher(text)
        ):
            return text
    return None


async def command_with_reply(
    mc: MeshCore,
    contact: Dict[str, Any],
    command: str,
    matcher: Callable[[str], bool],
) -> str:
    label = contact.get("adv_name", "repeater")
    for attempt in range(1, 4):
        result = await mc.commands.send_cmd(contact, command)
        if result is None or getattr(result, "type", None) == EventType.ERROR:
            if attempt == 3:
                raise RuntimeError(f"{label}: {command}_send_failed")
        else:
            reply = await fetch_cli_reply(
                mc, contact["public_key"][:12], matcher, timeout=20
            )
            if reply is not None:
                return reply
        if attempt < 3:
            print(f"{label}: retrying {command} (attempt {attempt + 1}/3)", flush=True)
            await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
    raise RuntimeError(f"{label}: {command}_reply_missing")


def parse_clock_reply(text: str) -> int:
    import re

    match = re.search(
        r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*UTC",
        text,
    )
    if not match:
        raise RuntimeError(f"unrecognised_clock_reply:{text}")
    hour, minute, day, month, year = map(int, match.groups())
    return int(datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp())


async def restore_companion_clock(
    serial: str,
    mc: Optional[MeshCore],
    probe_public_key: Optional[str],
) -> MeshCore:
    if mc is not None:
        try:
            await mc.commands.reboot()
        finally:
            await disconnect(mc)
    print("Companion reboot requested; waiting for serial reconnect", flush=True)
    await asyncio.sleep(8)
    restored = await connect(serial)
    await restored.commands.set_time(ROLLOVER_EPOCH)
    # RTCClock::last_unique is independent of the displayed RTC. Force it to
    # UINT32_MAX while the probe is unauthenticated, then consume its wrap to
    # zero. Merely waiting for the RTC to wrap leaves last_unique poisoned.
    probe_contact = None
    if probe_public_key:
        probe_contact = resolve_contact(restored.contacts, probe_public_key)
    elif restored.contacts:
        probe_contact = next(iter(restored.contacts.values()))
    if probe_contact is None:
        raise RuntimeError("companion_unique_clock_probe_contact_missing")
    await restored.commands.send_cmd(probe_contact, "clock")
    await asyncio.sleep(2)
    await restored.commands.send_cmd(probe_contact, "clock")
    host_epoch = int(time.time())
    set_result = await restored.commands.set_time(host_epoch)
    if set_result is None or getattr(set_result, "type", None) != EventType.OK:
        raise RuntimeError("companion_clock_restore_failed")
    clock_result = await restored.commands.get_time()
    companion_epoch = int((getattr(clock_result, "payload", {}) or {}).get("time", 0))
    drift = companion_epoch - int(time.time())
    if abs(drift) > 5:
        raise RuntimeError(f"companion_clock_verification_failed:drift={drift}")
    print(f"Companion UTC restored and verified (drift={drift}s)", flush=True)
    return restored


async def run(args: argparse.Namespace) -> None:
    if args.sync_only and args.replay_reset:
        raise RuntimeError("choose only one of --sync-only or --replay-reset")
    if not args.sync_only and not args.replay_reset and not args.confirm_companion_clock_reset:
        raise RuntimeError("pass --confirm-companion-clock-reset to run recovery")

    mc: Optional[MeshCore] = await connect(args.serial)
    recovery_started = False
    targets: list[tuple[str, str]] = []
    try:
        for requested in args.targets:
            contact = resolve_contact(mc.contacts, requested)
            targets.append((contact["adv_name"], contact["public_key"]))
        if len(targets) != 1:
            raise RuntimeError("recovery_requires_exactly_one_target")

        if args.replay_reset:
            label, public_key = targets[0]
            contact = resolve_contact(mc.contacts, public_key)
            companion_result = await mc.commands.get_time()
            companion_epoch = int(
                (getattr(companion_result, "payload", {}) or {}).get("time", 0)
            )
            companion_drift = companion_epoch - int(time.time())
            if abs(companion_drift) > 5:
                raise RuntimeError(
                    f"companion_clock_verification_failed:drift={companion_drift}"
                )
            await login_admin(mc, contact, label)
            await mc.commands.reset_path(contact)
            contact["out_path"] = ""
            contact["out_path_len"] = -1
            contact["out_path_hash_mode"] = 0
            result = await mc.commands.send_msg(
                contact,
                "clkreboot",
                timestamp=ROLLOVER_EPOCH,
            )
            if result is None or getattr(result, "type", None) == EventType.ERROR:
                raise RuntimeError(f"{label}: replay_reset_send_failed")
            print(f"{label}: maximum-timestamp clkreboot transmitted", flush=True)
            await asyncio.sleep(12)
            await mc.commands.get_contacts()
            contact = resolve_contact(mc.contacts, public_key)
            await mc.commands.reset_path(contact)
            contact["out_path"] = ""
            contact["out_path_len"] = -1
            contact["out_path_hash_mode"] = 0
            reset_epoch = await login_admin(mc, contact, label)
            if reset_epoch > 1715770351 + 300:
                raise RuntimeError(f"{label}: replay_reset_not_confirmed:{reset_epoch}")
            await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
            sync_result = await mc.commands.send_msg(
                contact,
                "clock sync",
                timestamp=int(time.time()),
            )
            if sync_result is None or getattr(sync_result, "type", None) == EventType.ERROR:
                raise RuntimeError(f"{label}: clock_sync_send_failed")
            await asyncio.sleep(4)
            remote_epoch = await login_admin(mc, contact, label)
            drift = remote_epoch - int(time.time())
            verified = datetime.fromtimestamp(remote_epoch, tz=timezone.utc).isoformat()
            print(f"{label}: verified {verified} (drift={drift}s)", flush=True)
            if abs(drift) > 90:
                raise RuntimeError(f"{label}: verified_clock_drift_too_high:{drift}")
            await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
            advert_result = await mc.commands.send_msg(
                contact,
                "advert",
                timestamp=int(time.time()),
            )
            if advert_result is None or getattr(advert_result, "type", None) == EventType.ERROR:
                raise RuntimeError(f"{label}: advert_send_failed")
            print(f"{label}: immediate advert transmitted", flush=True)
            await mc.commands.send_logout(contact)
            return

        if args.sync_only:
            label, public_key = targets[0]
            contact = resolve_contact(mc.contacts, public_key)
            companion_result = await mc.commands.get_time()
            companion_epoch = int(
                (getattr(companion_result, "payload", {}) or {}).get("time", 0)
            )
            companion_drift = companion_epoch - int(time.time())
            if abs(companion_drift) > 5:
                raise RuntimeError(
                    f"companion_clock_verification_failed:drift={companion_drift}"
                )
            print(
                f"Companion UTC verified (drift={companion_drift}s)", flush=True
            )
            await login_admin(mc, contact, label)
            # Login requests and CLI messages share the repeater's per-client
            # replay timestamp. Keep them in different RTC ticks so firmware
            # does not mistake the command for a retry and skip execution.
            await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
            sync_result = await mc.commands.send_cmd(contact, "clock sync")
            if sync_result is None or getattr(sync_result, "type", None) == EventType.ERROR:
                raise RuntimeError(f"{label}: clock_sync_send_failed")
            sync_reply = await fetch_cli_reply(
                mc,
                contact["public_key"][:12],
                lambda text: text.startswith("OK - clock set:")
                or "clock cannot go backwards" in text.lower(),
            )
            if sync_reply:
                print(f"{label}: {sync_reply}", flush=True)
            else:
                print(
                    f"{label}: clock sync acknowledgement missing; performing direct read-back",
                    flush=True,
                )
            remote_epoch = await login_admin(mc, contact, label)
            drift = remote_epoch - int(time.time())
            verified = datetime.fromtimestamp(remote_epoch, tz=timezone.utc).isoformat()
            print(f"{label}: verified {verified} (drift={drift}s)", flush=True)
            if abs(drift) > 90:
                raise RuntimeError(f"{label}: verified_clock_drift_too_high:{drift}")
            await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
            advert_reply = await command_with_reply(
                mc,
                contact,
                "advert",
                lambda text: text.startswith("OK - Advert sent"),
            )
            print(f"{label}: {advert_reply}", flush=True)
            await mc.commands.send_logout(contact)
            return

        for label, public_key in targets:
            contact = resolve_contact(mc.contacts, public_key)
            await login_admin(mc, contact, label)
            # Login before advancing the companion: UINT32_MAX lasts for only
            # one second. Sending immediately is the final timestamp capable
            # of exceeding a poisoned remote replay counter.
            recovery_started = True
            result = await mc.commands.set_time(ROLLOVER_EPOCH)
            if result is None or getattr(result, "type", None) != EventType.OK:
                raise RuntimeError("companion_max_clock_set_failed")
            sent = await mc.commands.send_cmd(contact, "clkreboot")
            if sent is None or getattr(sent, "type", None) == EventType.ERROR:
                raise RuntimeError(f"{label}: clkreboot_send_failed")
            print(f"{label}: clkreboot transmitted at UINT32_MAX", flush=True)
            await asyncio.sleep(3)
            # Ensure the rollover probes below cannot be accepted remotely if
            # clkreboot was lost before reaching the repeater.
            try:
                await mc.commands.send_logout(contact)
            except Exception:
                pass

        mc = await restore_companion_clock(args.serial, mc, targets[0][1])
        recovery_started = False
        await asyncio.sleep(8)

        failures = []
        for label, public_key in targets:
            try:
                await mc.commands.get_contacts()
                contact = resolve_contact(mc.contacts, public_key)
                await login_admin(mc, contact, label)
                await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
                sync_result = await mc.commands.send_cmd(contact, "clock sync")
                if sync_result is None or getattr(sync_result, "type", None) == EventType.ERROR:
                    raise RuntimeError("clock_sync_send_failed")
                sync_reply = await fetch_cli_reply(
                    mc,
                    contact["public_key"][:12],
                    lambda text: text.startswith("OK - clock set:")
                    or "clock cannot go backwards" in text.lower(),
                )
                if sync_reply:
                    print(f"{label}: {sync_reply}", flush=True)
                else:
                    print(
                        f"{label}: clock sync acknowledgement missing; performing direct read-back",
                        flush=True,
                    )
                # A fresh login response contains the repeater's own epoch and
                # does not depend on CLI message delivery or replay filtering.
                remote_epoch = await login_admin(mc, contact, label)
                drift = remote_epoch - int(time.time())
                verified = datetime.fromtimestamp(remote_epoch, tz=timezone.utc).isoformat()
                print(f"{label}: verified {verified} (drift={drift}s)", flush=True)
                if abs(drift) > 90:
                    raise RuntimeError(f"verified_clock_drift_too_high:{drift}")
                await asyncio.sleep(COMMAND_TIMESTAMP_GUARD_SECONDS)
                advert_reply = await command_with_reply(
                    mc,
                    contact,
                    "advert",
                    lambda text: text.startswith("OK - Advert sent"),
                )
                print(f"{label}: {advert_reply}", flush=True)
                await mc.commands.send_logout(contact)
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                print(f"{label}: FAILED: {exc}", flush=True)
        if failures:
            raise RuntimeError("; ".join(failures))
    finally:
        if recovery_started:
            try:
                probe_public_key = targets[0][1] if targets else None
                mc = await restore_companion_clock(args.serial, mc, probe_public_key)
            except Exception as exc:
                print(f"CRITICAL: companion clock restoration failed: {exc}", flush=True)
        await disconnect(mc)


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Recovery failed: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
