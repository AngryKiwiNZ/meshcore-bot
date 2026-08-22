#!/usr/bin/env python3
"""
Repeater monitor service.

Polls a configured list of repeater nodes for status/battery telemetry and
derives clock drift from the latest stored repeater advertisement seen by the
local companion.
"""

import asyncio
import csv
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from meshcore import EventType

from .base_service import BaseServicePlugin
from ..utils import resolve_path, calculate_distance

MAX_REPLAY_TIMESTAMP = 0xFFFFFFFF


@dataclass
class RepeaterTarget:
    node_key: str
    display_name: str
    fixed_out_path: Optional[str] = None


class RepeaterMonitorService(BaseServicePlugin):
    config_section = "Repeater_Monitor"
    description = "Poll configured and nearby discovered repeater nodes for telemetry"

    def __init__(self, bot: Any):
        super().__init__(bot)
        self.db_path = bot.db_manager.db_path
        self.nodes_file = resolve_path(
            self.bot.config.get(
                self.config_section,
                "nodes_file",
                fallback="data/repeater_monitor_nodes.txt",
            ),
            self.bot.bot_root,
        )
        self.poll_interval_seconds = self.bot.config.getint(
            self.config_section,
            "poll_interval_seconds",
            fallback=7200,
        )
        self.login_retry_attempts = self.bot.config.getint(
            self.config_section,
            "login_retry_attempts",
            fallback=4,
        )
        # Telemetry polling is intentionally much more conservative than an
        # explicit clock-sync operation.  A failed poll must never inherit the
        # clock-sync retry budget and monopolise the shared radio.
        self.poll_login_attempts = max(
            0,
            min(
                self.bot.config.getint(
                    self.config_section,
                    "poll_login_attempts",
                    fallback=4,
                ),
                self.login_retry_attempts,
            ),
        )
        self.retry_delay_seconds = self.bot.config.getint(
            self.config_section,
            "retry_delay_seconds",
            fallback=15,
        )
        self.data_request_attempts = self.bot.config.getint(
            self.config_section,
            "data_request_attempts",
            fallback=2,
        )
        self.data_request_retry_delay_seconds = self.bot.config.getfloat(
            self.config_section,
            "data_request_retry_delay_seconds",
            fallback=3.0,
        )
        self.manual_data_request_attempts = self.bot.config.getint(
            self.config_section,
            "manual_data_request_attempts",
            fallback=max(self.data_request_attempts * 2, 6),
        )
        self.manual_data_request_retry_delay_seconds = self.bot.config.getfloat(
            self.config_section,
            "manual_data_request_retry_delay_seconds",
            fallback=max(self.data_request_retry_delay_seconds, 10.0),
        )
        self.collect_temperature = self.bot.config.getboolean(
            self.config_section,
            "collect_temperature",
            fallback=True,
        )
        self.inter_node_delay_seconds = self.bot.config.getfloat(
            self.config_section,
            "inter_node_delay_seconds",
            fallback=3.0,
        )
        self.refresh_check_interval_seconds = self.bot.config.getfloat(
            self.config_section,
            "refresh_check_interval_seconds",
            fallback=5.0,
        )
        self.min_cycle_gap_seconds = self.bot.config.getfloat(
            self.config_section,
            "min_cycle_gap_seconds",
            fallback=60.0,
        )
        self.retention_days = self.bot.config.getint(
            self.config_section,
            "retention_days",
            fallback=31,
        )
        self.clock_drift_threshold_seconds = self.bot.config.getint(
            self.config_section,
            "clock_drift_threshold_seconds",
            fallback=120,
        )
        self.command_timeout_seconds = self.bot.config.getfloat(
            self.config_section,
            "command_timeout_seconds",
            fallback=0.0,
        )
        self.manual_command_timeout_seconds = self.bot.config.getfloat(
            self.config_section,
            "manual_command_timeout_seconds",
            fallback=max(self.command_timeout_seconds, 15.0),
        )
        self.clock_sync_command_delay_seconds = self.bot.config.getfloat(
            self.config_section,
            "clock_sync_command_delay_seconds",
            fallback=10.0,
        )
        self.clock_sync_advert_delay_seconds = self.bot.config.getfloat(
            self.config_section,
            "clock_sync_advert_delay_seconds",
            fallback=2.0,
        )
        self.login_after_failures = self.bot.config.getint(
            self.config_section,
            "login_after_failures",
            fallback=5,
        )
        self.path_reset_after_failures = self.bot.config.getint(
            self.config_section,
            "path_reset_after_failures",
            fallback=3,
        )
        self.login_cooldown_seconds = self.bot.config.getfloat(
            self.config_section,
            "login_cooldown_seconds",
            fallback=3600.0,
        )
        self.backoff_base = self.bot.config.getfloat(
            self.config_section,
            "backoff_base",
            fallback=2.0,
        )
        self.max_failure_backoff_seconds = self.bot.config.getfloat(
            self.config_section,
            "max_failure_backoff_seconds",
            fallback=86400.0,
        )
        self.path_refresh_on_retry = self.bot.config.getboolean(
            self.config_section,
            "path_refresh_on_retry",
            fallback=True,
        )
        self.auto_discover_contacts = self.bot.config.getboolean(
            self.config_section,
            "auto_discover_contacts",
            fallback=True,
        )
        self.auto_discovery_radius_km = self.bot.config.getfloat(
            self.config_section,
            "auto_discovery_radius_km",
            fallback=50.0,
        )
        self.auto_discovery_latitude, self.auto_discovery_longitude = (
            self._load_auto_discovery_origin()
        )
        self.refresh_trigger_path = resolve_path(
            self.bot.config.get(
                self.config_section,
                "refresh_trigger_file",
                fallback="data/repeater_monitor_refresh.trigger",
            ),
            self.bot.bot_root,
        )
        self.status_path = resolve_path(
            self.bot.config.get(
                self.config_section,
                "status_file",
                fallback="data/repeater_monitor_status.json",
            ),
            self.bot.bot_root,
        )
        self.polling_control_path = resolve_path(
            self.bot.config.get(
                self.config_section,
                "polling_control_file",
                fallback="data/repeater_monitor_control.json",
            ),
            self.bot.bot_root,
        )
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        # A manual request may have been queued immediately before a restart
        # used to abort a slow scheduled poll.  Leave the marker unset so the
        # first loop consumes that saved request instead of starting another
        # scheduled target first.
        self._last_refresh_trigger_marker = None
        self._pending_refresh_node_key: Optional[str] = None
        self._pending_refresh_action = "poll"
        self._target_schedule_signature: Tuple[str, ...] = tuple()
        self._last_cycle_duration_seconds = 0.0
        self._last_cycle_completed_monotonic: Optional[float] = None
        self._consecutive_failures: Dict[str, int] = {}
        self._last_login_times: Dict[str, float] = {}
        self._last_login_server_timestamps: Dict[str, int] = {}
        self._next_target_update_times: Dict[str, float] = {}
        self._init_tables()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._write_status(state="starting")
        self._poll_task = asyncio.create_task(self._poll_loop(), name="repeater-monitor")
        self.logger.info("Repeater monitor service started")

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        self._write_status(state="stopped")
        self.logger.info("Repeater monitor service stopped")

    def is_healthy(self) -> bool:
        return self._running and self._poll_task is not None and not self._poll_task.done()

    def _init_tables(self) -> None:
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS repeater_monitor_nodes (
                    node_key TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    resolved_public_key TEXT,
                    last_contact_name TEXT,
                    last_attempt_at REAL,
                    last_success_at REAL,
                    last_login_ok INTEGER DEFAULT 0,
                    last_status_ok INTEGER DEFAULT 0,
                    last_clock_ok INTEGER DEFAULT 0,
                    last_battery_mv INTEGER,
                    last_battery_percent INTEGER,
                    last_temperature_c REAL,
                    last_uptime_seconds INTEGER,
                    last_airtime_seconds INTEGER,
                    last_rx_airtime_seconds INTEGER,
                    last_rssi INTEGER,
                    last_snr REAL,
                    last_noise_floor INTEGER,
                    last_tx_queue_len INTEGER,
                    last_nb_recv INTEGER,
                    last_nb_sent INTEGER,
                    last_recv_errors INTEGER,
                    last_sent_flood INTEGER,
                    last_sent_direct INTEGER,
                    last_recv_flood INTEGER,
                    last_recv_direct INTEGER,
                    last_full_events INTEGER,
                    last_direct_dups INTEGER,
                    last_flood_dups INTEGER,
                    last_clock_epoch INTEGER,
                    last_clock_drift_seconds REAL,
                    last_poll_duration_seconds REAL,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS repeater_monitor_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_key TEXT NOT NULL,
                    resolved_public_key TEXT,
                    display_name TEXT NOT NULL,
                    collected_at REAL NOT NULL,
                    login_ok INTEGER DEFAULT 0,
                    status_ok INTEGER DEFAULT 0,
                    clock_ok INTEGER DEFAULT 0,
                    battery_mv INTEGER,
                    battery_percent INTEGER,
                    temperature_c REAL,
                    uptime_seconds INTEGER,
                    airtime_seconds INTEGER,
                    rx_airtime_seconds INTEGER,
                    last_rssi INTEGER,
                    last_snr REAL,
                    noise_floor INTEGER,
                    tx_queue_len INTEGER,
                    nb_recv INTEGER,
                    nb_sent INTEGER,
                    recv_errors INTEGER,
                    sent_flood INTEGER,
                    sent_direct INTEGER,
                    recv_flood INTEGER,
                    recv_direct INTEGER,
                    full_events INTEGER,
                    direct_dups INTEGER,
                    flood_dups INTEGER,
                    clock_epoch INTEGER,
                    clock_drift_seconds REAL,
                    poll_duration_seconds REAL,
                    login_attempts INTEGER DEFAULT 0,
                    error_text TEXT
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_repeater_monitor_samples_node_time "
                "ON repeater_monitor_samples(node_key, collected_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_repeater_monitor_samples_time "
                "ON repeater_monitor_samples(collected_at)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS repeater_monitor_command_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_key TEXT NOT NULL,
                    logged_at REAL NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    message TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_repeater_monitor_command_log_node_time "
                "ON repeater_monitor_command_log(node_key, logged_at DESC, id DESC)"
            )
            try:
                cursor.execute(
                    "ALTER TABLE repeater_monitor_nodes ADD COLUMN last_battery_percent INTEGER"
                )
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE repeater_monitor_samples ADD COLUMN battery_percent INTEGER"
                )
            except sqlite3.OperationalError:
                pass
            for table_name, column_name, column_type in (
                ("repeater_monitor_nodes", "last_poll_duration_seconds", "REAL"),
                ("repeater_monitor_samples", "poll_duration_seconds", "REAL"),
                ("repeater_monitor_nodes", "last_temperature_c", "REAL"),
                ("repeater_monitor_samples", "temperature_c", "REAL"),
            ):
                try:
                    cursor.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    )
                except sqlite3.OperationalError:
                    pass
            for table_name, column_name, column_type in (
                ("repeater_monitor_nodes", "last_airtime_seconds", "INTEGER"),
                ("repeater_monitor_nodes", "last_rx_airtime_seconds", "INTEGER"),
                ("repeater_monitor_nodes", "last_rssi", "INTEGER"),
                ("repeater_monitor_nodes", "last_snr", "REAL"),
                ("repeater_monitor_nodes", "last_noise_floor", "INTEGER"),
                ("repeater_monitor_nodes", "last_tx_queue_len", "INTEGER"),
                ("repeater_monitor_nodes", "last_nb_recv", "INTEGER"),
                ("repeater_monitor_nodes", "last_nb_sent", "INTEGER"),
                ("repeater_monitor_nodes", "last_recv_errors", "INTEGER"),
                ("repeater_monitor_nodes", "last_sent_flood", "INTEGER"),
                ("repeater_monitor_nodes", "last_sent_direct", "INTEGER"),
                ("repeater_monitor_nodes", "last_recv_flood", "INTEGER"),
                ("repeater_monitor_nodes", "last_recv_direct", "INTEGER"),
                ("repeater_monitor_nodes", "last_full_events", "INTEGER"),
                ("repeater_monitor_nodes", "last_direct_dups", "INTEGER"),
                ("repeater_monitor_nodes", "last_flood_dups", "INTEGER"),
                ("repeater_monitor_samples", "airtime_seconds", "INTEGER"),
                ("repeater_monitor_samples", "rx_airtime_seconds", "INTEGER"),
                ("repeater_monitor_samples", "last_rssi", "INTEGER"),
                ("repeater_monitor_samples", "last_snr", "REAL"),
                ("repeater_monitor_samples", "noise_floor", "INTEGER"),
                ("repeater_monitor_samples", "tx_queue_len", "INTEGER"),
                ("repeater_monitor_samples", "nb_recv", "INTEGER"),
                ("repeater_monitor_samples", "nb_sent", "INTEGER"),
                ("repeater_monitor_samples", "recv_errors", "INTEGER"),
                ("repeater_monitor_samples", "sent_flood", "INTEGER"),
                ("repeater_monitor_samples", "sent_direct", "INTEGER"),
                ("repeater_monitor_samples", "recv_flood", "INTEGER"),
                ("repeater_monitor_samples", "recv_direct", "INTEGER"),
                ("repeater_monitor_samples", "full_events", "INTEGER"),
                ("repeater_monitor_samples", "direct_dups", "INTEGER"),
                ("repeater_monitor_samples", "flood_dups", "INTEGER"),
            ):
                try:
                    cursor.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    )
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def _append_command_log(
        self,
        target: RepeaterTarget,
        message: str,
        level: str = "info",
    ) -> None:
        """Persist a short, user-facing trace of work performed against a repeater."""
        try:
            with sqlite3.connect(self.db_path, timeout=60) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO repeater_monitor_command_log
                        (node_key, logged_at, level, message)
                    VALUES (?, ?, ?, ?)
                    """,
                    (target.node_key, time.time(), level, message),
                )
                # Keep enough history for diagnosis without allowing an unbounded table.
                cursor.execute(
                    """
                    DELETE FROM repeater_monitor_command_log
                    WHERE node_key = ? AND id NOT IN (
                        SELECT id
                        FROM repeater_monitor_command_log
                        WHERE node_key = ?
                        ORDER BY logged_at DESC, id DESC
                        LIMIT 250
                    )
                    """,
                    (target.node_key, target.node_key),
                )
                conn.commit()
        except sqlite3.Error as exc:
            self.logger.warning(
                "Unable to persist repeater command log for %s: %s",
                target.display_name,
                exc,
            )

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                if not self.bot.connected or not getattr(self.bot, "meshcore", None):
                    self._write_status(state="waiting_for_connection")
                    await asyncio.sleep(30)
                    continue
                # Manual Poll Now / Clock Sync work has priority, including a
                # trigger written before this service instance started.
                self._consume_refresh_trigger()
                targets = self._load_targets()
                self._ensure_staggered_schedule(targets)
                refresh_node_key = self._pending_refresh_node_key
                refresh_action = self._pending_refresh_action
                self._pending_refresh_node_key = None
                self._pending_refresh_action = "poll"
                if refresh_action == "clock_sync" and refresh_node_key:
                    await self._clock_sync_by_node_key(refresh_node_key, targets)
                elif not self._polling_enabled() and not refresh_node_key:
                    self._write_status(
                        state="suspended",
                        polling_enabled=False,
                        current_target=None,
                        current_node_key=None,
                        requested_node_key=None,
                    )
                else:
                    await self._poll_all_targets(
                        target_node_key=refresh_node_key,
                        loaded_targets=targets,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._write_status(state="error", error=str(exc))
                self.logger.error("Repeater monitor cycle failed: %s", exc, exc_info=True)
            await self._wait_for_next_poll_window()

    def _polling_enabled(self) -> bool:
        """Return the persistent runtime switch for all repeater polling."""
        path = Path(self.polling_control_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except (OSError, json.JSONDecodeError, TypeError):
            self.logger.warning(
                "Unable to read repeater polling control file %s; leaving polling enabled",
                path,
            )
            return True
        return bool(payload.get("polling_enabled", True))

    async def _poll_all_targets(
        self,
        target_node_key: Optional[str] = None,
        loaded_targets: Optional[List[RepeaterTarget]] = None,
    ) -> None:
        targets = list(loaded_targets) if loaded_targets is not None else self._load_targets()
        if not targets:
            self.logger.warning(
                "Repeater monitor is enabled but no targets were loaded from %s",
                self.nodes_file,
            )
            return

        if target_node_key:
            matching_targets = [
                target for target in targets
                if self._find_matching_key([target.node_key], target_node_key)
            ]
            if not matching_targets:
                self.logger.warning(
                    "Repeater monitor manual single-device refresh target not found: %s",
                    target_node_key,
                )
                self._write_status(
                    state="idle",
                    requested_node_key=target_node_key,
                    requested_target_found=False,
                    updated_at=time.time(),
                )
                return
            targets = matching_targets
        else:
            due_targets = [
                target for target in targets
                if time.time() >= self._next_target_update_times.get(target.node_key, 0.0)
            ]
            if not due_targets:
                self._write_status(
                    state="idle",
                    total_targets=len(targets),
                    current_index=0,
                    current_target=None,
                    next_scheduled_poll_at=self._next_scheduled_poll_at(),
                    estimated_cycle_duration_seconds=self._estimate_cycle_duration_seconds(len(targets)),
                    configured_poll_interval_seconds=self.poll_interval_seconds,
                    min_cycle_gap_seconds=self.min_cycle_gap_seconds,
                )
                return
            due_targets.sort(key=lambda target: self._next_target_update_times.get(target.node_key, 0.0))
            targets = [due_targets[0]]

        await self.bot.meshcore.ensure_contacts()

        cycle_started_at = time.time()
        total_target_count = len(loaded_targets) if loaded_targets is not None else len(self._load_targets())
        estimated_cycle_duration_seconds = self._estimate_cycle_duration_seconds(total_target_count)
        if total_target_count > 0 and estimated_cycle_duration_seconds > self.poll_interval_seconds:
            self.logger.warning(
                "Repeater monitor estimated cycle duration %.1fs exceeds poll interval %.1fs; "
                "next scheduled cycle will wait until the current cycle finishes plus the configured gap",
                estimated_cycle_duration_seconds,
                float(self.poll_interval_seconds),
            )
        if target_node_key:
            self.logger.info(
                "Repeater monitor polling single target %s (%s)",
                targets[0].display_name,
                targets[0].node_key[:12],
            )
        else:
            self.logger.info(
                "Repeater monitor polling scheduled target %s (%s)",
                targets[0].display_name,
                targets[0].node_key[:12],
            )
        self._write_status(
            state="polling",
            total_targets=total_target_count,
            current_index=0,
            current_target=None,
            requested_node_key=target_node_key,
            last_cycle_started_at=cycle_started_at,
            estimated_cycle_duration_seconds=estimated_cycle_duration_seconds,
            configured_poll_interval_seconds=self.poll_interval_seconds,
            min_cycle_gap_seconds=self.min_cycle_gap_seconds,
        )
        for index, target in enumerate(targets, start=1):
            if not self._running:
                return
            self._write_status(
                state="polling",
                total_targets=total_target_count,
                current_index=index,
                current_target=target.display_name,
                current_node_key=target.node_key,
                requested_node_key=target_node_key,
                last_cycle_started_at=cycle_started_at,
                estimated_cycle_duration_seconds=estimated_cycle_duration_seconds,
                configured_poll_interval_seconds=self.poll_interval_seconds,
                min_cycle_gap_seconds=self.min_cycle_gap_seconds,
            )
            self.logger.info(
                "Repeater monitor polling %s/%s: %s (%s)",
                index,
                len(targets),
                target.display_name,
                target.node_key[:12],
            )
            await self._poll_target(target, force=bool(target_node_key))
            await asyncio.sleep(self.inter_node_delay_seconds)

        self._purge_old_samples()
        self._last_cycle_duration_seconds = max(0.0, time.time() - cycle_started_at)
        self._last_cycle_completed_monotonic = time.monotonic()
        self._write_status(
            state="idle",
            total_targets=total_target_count,
            current_index=0,
            current_target=None,
            requested_node_key=target_node_key,
            last_cycle_completed_at=time.time(),
            last_cycle_duration_seconds=self._last_cycle_duration_seconds,
            estimated_cycle_duration_seconds=estimated_cycle_duration_seconds,
            configured_poll_interval_seconds=self.poll_interval_seconds,
            min_cycle_gap_seconds=self.min_cycle_gap_seconds,
            next_scheduled_poll_at=self._next_scheduled_poll_at(),
        )

    def _load_targets(self) -> List[RepeaterTarget]:
        manual_targets, disabled_keys = self._load_target_overrides()
        targets: List[RepeaterTarget] = list(manual_targets)
        seen_keys = [target.node_key for target in manual_targets]

        if self.auto_discover_contacts:
            auto_targets = self._load_auto_discovered_targets(disabled_keys)
            for target in auto_targets:
                if self._find_matching_key(seen_keys, target.node_key):
                    continue
                targets.append(target)
                seen_keys.append(target.node_key)

        return targets

    def _ensure_staggered_schedule(self, targets: List[RepeaterTarget]) -> None:
        signature = tuple(sorted(target.node_key for target in targets))
        if signature == self._target_schedule_signature:
            return

        self._target_schedule_signature = signature
        active_keys = set(signature)
        self._next_target_update_times = {
            key: due_at
            for key, due_at in self._next_target_update_times.items()
            if key in active_keys
        }

        if not targets:
            return

        spacing_seconds = max(
            float(self.poll_interval_seconds) / max(len(targets), 1),
            max(self.inter_node_delay_seconds, 1.0),
        )
        jitter_seconds = min(spacing_seconds * 0.35, 300.0)
        base_time = time.time()
        shuffled_targets = list(targets)
        random.shuffle(shuffled_targets)

        for index, target in enumerate(shuffled_targets):
            due_at = base_time + (index * spacing_seconds) + random.uniform(0.0, jitter_seconds)
            self._next_target_update_times[target.node_key] = due_at

        self.logger.info(
            "Repeater monitor redistributed %s target(s) across the next %.1f minutes (spacing %.1f min)",
            len(targets),
            float(self.poll_interval_seconds) / 60.0,
            spacing_seconds / 60.0,
        )

    def _read_refresh_trigger_marker(self) -> Optional[int]:
        path = Path(self.refresh_trigger_path)
        try:
            return path.stat().st_mtime_ns
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def _consume_refresh_trigger(self) -> bool:
        marker = self._read_refresh_trigger_marker()
        if marker is None or marker == self._last_refresh_trigger_marker:
            return False
        self._last_refresh_trigger_marker = marker
        self._pending_refresh_node_key = None
        self._pending_refresh_action = "poll"
        path = Path(self.refresh_trigger_path)
        try:
            payload_text = path.read_text(encoding="utf-8").strip()
            if payload_text:
                try:
                    payload = json.loads(payload_text)
                except json.JSONDecodeError:
                    payload = {"node_key": payload_text}
                node_key = (payload.get("node_key") or "").strip().lower()
                if node_key:
                    self._pending_refresh_node_key = node_key
                requested_action = str(payload.get("action") or "poll").strip().lower()
                if requested_action == "clock_sync":
                    self._pending_refresh_action = requested_action
        except OSError:
            pass
        else:
            # A trigger is a queued job, not persistent configuration.  Once
            # read, remove it so restarting the bot cannot replay a completed
            # or deliberately aborted Poll Now request.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                self.logger.warning("Unable to consume repeater trigger %s: %s", path, exc)
        self._write_status(
            state=(
                "clock_sync_queued"
                if self._pending_refresh_action == "clock_sync"
                else "refresh_queued"
            ),
            requested_node_key=self._pending_refresh_node_key,
            requested_action=self._pending_refresh_action,
        )
        return True

    def _write_status(self, **status_fields: Any) -> None:
        payload = {"updated_at": time.time()}
        payload.update(status_fields)
        path = Path(self.status_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    async def _wait_for_next_poll_window(self) -> None:
        now_monotonic = time.monotonic()
        cycle_gap_seconds = max(self.min_cycle_gap_seconds, 0.0)
        minimum_gap_deadline = now_monotonic + cycle_gap_seconds
        next_scheduled_poll_at = self._next_scheduled_poll_at()
        if next_scheduled_poll_at is None:
            scheduled_deadline = now_monotonic + max(self.refresh_check_interval_seconds, 5.0)
        else:
            scheduled_deadline = now_monotonic + max(0.1, next_scheduled_poll_at - time.time())
        refresh_requested = False

        while self._running:
            if self._consume_refresh_trigger():
                refresh_requested = True
                self.logger.info(
                    "Repeater monitor manual %s trigger received; waiting %.1fs minimum gap",
                    self._pending_refresh_action,
                    cycle_gap_seconds,
                )
                self._write_status(
                    state=(
                        "clock_sync_queued"
                        if self._pending_refresh_action == "clock_sync"
                        else "refresh_queued"
                    ),
                    requested_node_key=self._pending_refresh_node_key,
                    requested_action=self._pending_refresh_action,
                    next_allowed_poll_at=time.time()
                    + max(0.0, minimum_gap_deadline - time.monotonic()),
                    configured_poll_interval_seconds=self.poll_interval_seconds,
                    min_cycle_gap_seconds=self.min_cycle_gap_seconds,
                    last_cycle_duration_seconds=self._last_cycle_duration_seconds,
                )

            now_monotonic = time.monotonic()
            if refresh_requested and now_monotonic >= minimum_gap_deadline:
                return
            if now_monotonic >= max(scheduled_deadline, minimum_gap_deadline):
                return

            next_deadline = max(scheduled_deadline, minimum_gap_deadline)
            if refresh_requested:
                next_deadline = min(next_deadline, minimum_gap_deadline)
            remaining = next_deadline - now_monotonic
            await asyncio.sleep(min(self.refresh_check_interval_seconds, max(remaining, 0.1)))

    def _next_scheduled_poll_at(self) -> Optional[float]:
        if not self._next_target_update_times:
            return None
        return min(self._next_target_update_times.values())

    def _estimate_cycle_duration_seconds(self, target_count: int) -> float:
        timeout_seconds = max(self.command_timeout_seconds, 5.0)
        # Normal request pair + one flood-path fallback.  Login-assisted data
        # is deliberately omitted: it is only attempted after repeated misses.
        request_types = 2 if self.collect_temperature else 1
        estimated_per_target = (
            timeout_seconds * request_types * 2
        ) + self.inter_node_delay_seconds
        return max(0.0, target_count * estimated_per_target)

    def _load_target_overrides(self) -> Tuple[List[RepeaterTarget], List[str]]:
        path = Path(self.nodes_file)
        if not path.exists():
            self.logger.warning("Repeater monitor nodes file not found: %s", path)
            return [], []

        targets: List[RepeaterTarget] = []
        disabled_keys: List[str] = []
        with path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                fields = next(csv.reader([line], skipinitialspace=True))
                if not fields:
                    continue

                node_key = (fields[0] or "").strip().lower()
                display_name = (fields[1] or "").strip() if len(fields) > 1 else ""
                enabled = self._parse_target_enabled(fields[2] if len(fields) > 2 else "")
                fixed_out_path = self._parse_fixed_out_path(fields[3] if len(fields) > 3 else "")
                if not node_key:
                    continue
                if not display_name:
                    display_name = node_key[:12]
                if enabled:
                    targets.append(
                        RepeaterTarget(
                            node_key=node_key,
                            display_name=display_name,
                            fixed_out_path=fixed_out_path,
                        )
                    )
                else:
                    disabled_keys.append(node_key)

        return targets, disabled_keys

    def _load_auto_discovery_origin(self) -> Tuple[Optional[float], Optional[float]]:
        latitude = self.bot.config.getfloat(
            self.config_section,
            "auto_discovery_latitude",
            fallback=None,
        )
        longitude = self.bot.config.getfloat(
            self.config_section,
            "auto_discovery_longitude",
            fallback=None,
        )
        if latitude is None or longitude is None:
            latitude = self.bot.config.getfloat("Bot", "bot_latitude", fallback=None)
            longitude = self.bot.config.getfloat("Bot", "bot_longitude", fallback=None)
        return latitude, longitude

    def _load_auto_discovered_targets(self, disabled_keys: List[str]) -> List[RepeaterTarget]:
        if self.auto_discovery_latitude is None or self.auto_discovery_longitude is None:
            self.logger.warning(
                "Repeater monitor auto-discovery is enabled but no origin coordinates are configured"
            )
            return []

        try:
            with sqlite3.connect(self.db_path, timeout=60) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT
                        public_key,
                        name,
                        latitude,
                        longitude,
                        role,
                        COALESCE(last_advert_timestamp, last_heard) AS last_seen
                    FROM complete_contact_tracking
                    WHERE role IN ('repeater', 'roomserver')
                    AND public_key IS NOT NULL
                    AND latitude IS NOT NULL
                    AND longitude IS NOT NULL
                    AND latitude != 0
                    AND longitude != 0
                    ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC, name COLLATE NOCASE
                    """
                )
                rows = cursor.fetchall()
        except sqlite3.Error as exc:
            self.logger.warning("Repeater monitor auto-discovery query failed: %s", exc)
            return []

        targets: List[RepeaterTarget] = []
        for row in rows:
            node_key = (row["public_key"] or "").strip().lower()
            if not node_key:
                continue
            if self._find_matching_key(disabled_keys, node_key):
                continue

            distance_km = calculate_distance(
                self.auto_discovery_latitude,
                self.auto_discovery_longitude,
                float(row["latitude"]),
                float(row["longitude"]),
            )
            if distance_km > self.auto_discovery_radius_km:
                continue

            display_name = (row["name"] or "").strip() or node_key[:12]
            if self._find_matching_key([target.node_key for target in targets], node_key):
                continue
            targets.append(RepeaterTarget(node_key=node_key, display_name=display_name))

        if targets:
            self.logger.info(
                "Repeater monitor auto-discovered %s nearby repeater target(s) within %.1f km",
                len(targets),
                self.auto_discovery_radius_km,
            )
        return targets

    def _parse_target_enabled(self, raw_value: str) -> bool:
        if raw_value is None:
            return True
        value = str(raw_value).strip().lower()
        if not value:
            return True
        return value not in {"0", "false", "disabled", "disable", "off", "no", "skip"}

    def _parse_fixed_out_path(self, raw_value: str) -> Optional[str]:
        value = (raw_value or "").strip().lower().replace(",", "")
        if not value:
            return None
        if len(value) % 2 != 0:
            self.logger.warning(
                "Ignoring invalid fixed repeater path '%s' (must be even-length hex)",
                raw_value,
            )
            return None
        if any(ch not in "0123456789abcdef" for ch in value):
            self.logger.warning(
                "Ignoring invalid fixed repeater path '%s' (must be hex)",
                raw_value,
            )
            return None
        return value

    def _find_matching_key(self, keys: List[str], candidate_key: str) -> Optional[str]:
        candidate = (candidate_key or "").strip().lower()
        for key in keys:
            normalized = (key or "").strip().lower()
            if not normalized:
                continue
            if candidate.startswith(normalized) or normalized.startswith(candidate):
                return normalized
        return None

    async def _poll_target(self, target: RepeaterTarget, force: bool = False) -> None:
        now = time.time()
        poll_started_monotonic = time.monotonic()
        if not force and now < self._next_target_update_times.get(target.node_key, 0.0):
            return
        self._append_command_log(
            target,
            f"Poll started ({'manual Poll Now' if force else 'scheduled check'})",
        )
        self._append_command_log(target, "Resolving repeater contact")
        contact = await self._resolve_contact(target.node_key)

        if contact is None:
            self._append_command_log(target, "Contact lookup failed; poll stopped", "error")
            self._mark_target_failure(target, "contact_not_found")
            self._store_result(
                target=target,
                collected_at=now,
                login_ok=False,
                status_ok=False,
                clock_ok=False,
                login_attempts=0,
                poll_duration_seconds=time.monotonic() - poll_started_monotonic,
                error_text="contact_not_found",
            )
            return

        self._append_command_log(
            target,
            f"Contact resolved as {contact.get('adv_name') or target.display_name}",
            "success",
        )

        login_ok = False
        login_attempts = 0
        last_error: Optional[str] = None
        status_payload = None
        telemetry_payload = None
        resolved_public_key: Optional[str] = contact.get("public_key")
        last_contact_name: Optional[str] = contact.get("adv_name")
        failure_count = self._consecutive_failures.get(target.node_key, 0)

        if (
            not force
            and failure_count >= self.path_reset_after_failures
            and self.path_refresh_on_retry
        ):
            await self._refresh_contact_path(contact, target)
        if not force:
            self._prime_contact_for_anon_requests(contact, target)

        request_timeout_seconds = (
            self.manual_command_timeout_seconds
            if force
            else self.command_timeout_seconds
        )
        request_attempt_budget = max(
            1,
            (
                self.manual_data_request_attempts
                if force
                else self.data_request_attempts
            ),
        )
        request_attempts_used = 0

        reset_before_login = False
        if force:
            self._append_command_log(
                target,
                "Manual Poll Now: resetting the saved route once before authentication",
            )
            reset_before_login = await self._reset_contact_path(contact)
            self._append_command_log(
                target,
                (
                    "Saved route cleared; the login will establish a fresh connection"
                    if reset_before_login
                    else "Route reset was sent but the companion returned no confirmation"
                ),
                "success" if reset_before_login else "warning",
            )

        # Match the Companion app and the original known-good monitor flow:
        # a manual poll is a login operation followed by status retrieval.  A
        # previous airtime optimisation reversed these steps and reduced login
        # recovery to one attempt.  That made a transient login failure fatal
        # even though real-world Companion tests commonly succeed on attempts
        # two or three.
        if force and self.poll_login_attempts > 0:
            for attempt in range(1, self.poll_login_attempts + 1):
                login_attempts = attempt
                login_ok, login_error = await self._attempt_login(
                    contact,
                    target,
                    attempt,
                    timeout_seconds=request_timeout_seconds,
                    attempts_total=self.poll_login_attempts,
                )
                if login_error:
                    last_error = login_error
                if login_ok:
                    # A later successful retry supersedes an earlier timeout.
                    # Without clearing this, a fully successful poll is stored
                    # and displayed with the stale error "login_failed".
                    last_error = None
                    resolved_public_key = contact.get("public_key")
                    last_contact_name = contact.get("adv_name")
                    self._last_login_times[target.node_key] = time.time()
                    break
                if attempt < self.poll_login_attempts:
                    retry_delay = self.manual_data_request_retry_delay_seconds
                    self._append_command_log(
                        target,
                        f"Login was silent; retrying the same fresh connection in {retry_delay:.1f}s",
                        "warning",
                    )
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)

        # Status and telemetry are authenticated operations in the Companion
        # workflow. Do not burn another four radio timeouts after every login
        # attempt has already failed.
        if not force or login_ok:
            (
                status_payload,
                telemetry_payload,
                request_error,
            ) = await self._collect_repeater_data(
                contact=contact,
                target=target,
                status_payload=status_payload,
                telemetry_payload=None,
                request_attempts=request_attempt_budget if force else 1,
                request_retry_delay_seconds=(
                    self.manual_data_request_retry_delay_seconds if force else 0
                ),
                request_timeout_seconds=request_timeout_seconds,
                request_attempt_offset=request_attempts_used,
                request_attempt_total=request_attempt_budget,
            )
            request_attempts_used += request_attempt_budget if force else 1
            if request_error:
                last_error = request_error
        if force and login_ok:
            try:
                self._append_command_log(target, "Sending logout command")
                await self.bot.meshcore.commands.send_logout(contact)
                self._append_command_log(target, "Logout command completed", "success")
            except Exception:
                self._append_command_log(target, "Logout command failed", "warning")

        if (
            status_payload is None
            and telemetry_payload is None
            and not force
            and target.fixed_out_path is None
            and not reset_before_login
        ):
            self._append_command_log(target, "Running reset_path before retry")
            reset_before_login = await self._reset_contact_path(contact)
            self._append_command_log(
                target,
                "reset_path completed" if reset_before_login else "reset_path failed",
                "success" if reset_before_login else "warning",
            )
            # A manual recovery has a strict configured request budget: try the
            # live route once, then (after resetting to flood) authenticate and
            # use the remaining attempts. Scheduled checks
            # reserve one final request for login-assisted recovery when due.
            if reset_before_login and not force:
                login_recovery_due = (
                    self.poll_login_attempts > 0
                    and self._should_attempt_login(target.node_key)
                )
                remaining_attempts = request_attempt_budget - request_attempts_used
                flood_attempts = max(
                    0,
                    remaining_attempts - (1 if login_recovery_due else 0),
                )
                if flood_attempts:
                    (
                        status_payload,
                        telemetry_payload,
                        request_error,
                    ) = await self._collect_repeater_data(
                        contact=contact,
                        target=target,
                        status_payload=status_payload,
                        telemetry_payload=telemetry_payload,
                        request_attempts=flood_attempts,
                        request_retry_delay_seconds=self.data_request_retry_delay_seconds,
                        request_timeout_seconds=request_timeout_seconds,
                        request_attempt_offset=request_attempts_used,
                        request_attempt_total=request_attempt_budget,
                    )
                    request_attempts_used += flood_attempts
                    if request_error:
                        last_error = request_error
            elif reset_before_login:
                self._append_command_log(
                    target,
                    "Current route was silent; using flood routing for the login-assisted retry",
                )

        should_try_poll_login = (
            not force
            and
            status_payload is None
            and telemetry_payload is None
            and self.poll_login_attempts > 0
            and request_attempts_used < request_attempt_budget
            and (force or self._should_attempt_login(target.node_key))
        )
        if should_try_poll_login:
            for attempt in range(1, self.poll_login_attempts + 1):
                login_attempts = attempt
                if attempt > 1 and not reset_before_login and target.fixed_out_path is not None:
                    self._append_command_log(target, "Running reset_path before login retry")
                    reset_before_login = await self._reset_contact_path(contact)
                login_ok, login_error = await self._attempt_login(
                    contact,
                    target,
                    attempt,
                    timeout_seconds=request_timeout_seconds,
                    attempts_total=self.poll_login_attempts,
                )
                if login_error:
                    last_error = login_error
                if not login_ok:
                    continue

                resolved_public_key = contact.get("public_key")
                last_contact_name = contact.get("adv_name")
                self._last_login_times[target.node_key] = time.time()
                self._prime_contact_for_anon_requests(contact, target)
                (
                    status_payload,
                    telemetry_payload,
                    request_error,
                ) = await self._collect_repeater_data(
                    contact=contact,
                    target=target,
                    status_payload=status_payload,
                    telemetry_payload=telemetry_payload,
                    request_attempts=request_attempt_budget - request_attempts_used,
                    request_retry_delay_seconds=(
                        self.manual_data_request_retry_delay_seconds
                        if force
                        else self.data_request_retry_delay_seconds
                    ),
                    request_timeout_seconds=request_timeout_seconds,
                    request_attempt_offset=request_attempts_used,
                    request_attempt_total=request_attempt_budget,
                )
                request_attempts_used = request_attempt_budget
                if request_error:
                    last_error = request_error
                try:
                    self._append_command_log(target, "Sending logout command")
                    await self.bot.meshcore.commands.send_logout(contact)
                    self._append_command_log(target, "Logout command completed", "success")
                except Exception:
                    self._append_command_log(target, "Logout command failed", "warning")
                    pass
                if status_payload is not None or telemetry_payload is not None:
                    break

        battery_mv = status_payload.get("bat") if status_payload else None
        telemetry_battery_mv, telemetry_battery_percent = self._extract_battery_from_telemetry(
            telemetry_payload
        )
        temperature_c = self._extract_temperature_from_telemetry(telemetry_payload)
        if temperature_c is not None:
            self._append_command_log(
                target,
                f"Temperature published: {temperature_c:.1f} °C",
                "success",
            )
        if battery_mv is None:
            battery_mv = telemetry_battery_mv
        battery_percent = self._battery_percent(battery_mv)
        if battery_percent is None:
            battery_percent = telemetry_battery_percent
        uptime_seconds = status_payload.get("uptime") if status_payload else None
        airtime_seconds = status_payload.get("airtime") if status_payload else None
        rx_airtime_seconds = status_payload.get("rx_airtime") if status_payload else None
        last_rssi = status_payload.get("last_rssi") if status_payload else None
        last_snr = status_payload.get("last_snr") if status_payload else None
        noise_floor = status_payload.get("noise_floor") if status_payload else None
        tx_queue_len = status_payload.get("tx_queue_len") if status_payload else None
        nb_recv = status_payload.get("nb_recv") if status_payload else None
        nb_sent = status_payload.get("nb_sent") if status_payload else None
        recv_errors = status_payload.get("recv_errors") if status_payload else None
        sent_flood = status_payload.get("sent_flood") if status_payload else None
        sent_direct = status_payload.get("sent_direct") if status_payload else None
        recv_flood = status_payload.get("recv_flood") if status_payload else None
        recv_direct = status_payload.get("recv_direct") if status_payload else None
        full_events = status_payload.get("full_evts") if status_payload else None
        direct_dups = status_payload.get("direct_dups") if status_payload else None
        flood_dups = status_payload.get("flood_dups") if status_payload else None
        status_ok = status_payload is not None
        telemetry_ok = telemetry_payload is not None

        clock_epoch, clock_drift_seconds = self._latest_advert_clock_snapshot(
            resolved_public_key=resolved_public_key,
            node_key=target.node_key,
            display_name=target.display_name,
        )
        clock_ok = (
            clock_drift_seconds is not None
            and abs(clock_drift_seconds) <= self.clock_drift_threshold_seconds
        )

        if status_ok or telemetry_ok or clock_epoch is not None:
            self._mark_target_success(target)
        else:
            self._mark_target_failure(target, last_error or "status_response_missing")

        if not status_ok and last_error is None:
            last_error = "telemetry_only" if telemetry_ok else "status_response_missing"
        elif status_ok and clock_epoch is None and last_error is None:
            last_error = "advert_clock_unavailable"

        poll_duration_seconds = time.monotonic() - poll_started_monotonic
        self.logger.info(
            "Repeater monitor poll finished for %s in %.1fs (status=%s telemetry=%s login_ok=%s login_attempts=%s error=%s)",
            target.display_name,
            poll_duration_seconds,
            status_ok,
            telemetry_ok,
            login_ok,
            login_attempts,
            last_error or "none",
        )
        outcome = "success" if status_ok or telemetry_ok else "error"
        self._append_command_log(
            target,
            (
                f"Poll finished in {poll_duration_seconds:.1f}s "
                f"(status={'ok' if status_ok else 'missing'}, "
                f"telemetry={'ok' if telemetry_ok else 'missing'}, "
                f"result={last_error or 'ok'})"
            ),
            outcome,
        )

        self._store_result(
            target=target,
            collected_at=now,
            login_ok=login_ok,
            status_ok=status_ok,
            clock_ok=clock_ok,
            login_attempts=login_attempts,
            poll_duration_seconds=poll_duration_seconds,
            battery_mv=battery_mv,
            battery_percent=battery_percent,
            temperature_c=temperature_c,
            uptime_seconds=uptime_seconds,
            airtime_seconds=airtime_seconds,
            rx_airtime_seconds=rx_airtime_seconds,
            last_rssi=last_rssi,
            last_snr=last_snr,
            noise_floor=noise_floor,
            tx_queue_len=tx_queue_len,
            nb_recv=nb_recv,
            nb_sent=nb_sent,
            recv_errors=recv_errors,
            sent_flood=sent_flood,
            sent_direct=sent_direct,
            recv_flood=recv_flood,
            recv_direct=recv_direct,
            full_events=full_events,
            direct_dups=direct_dups,
            flood_dups=flood_dups,
            clock_epoch=clock_epoch,
            clock_drift_seconds=clock_drift_seconds,
            error_text=last_error,
            resolved_public_key=resolved_public_key,
            last_contact_name=last_contact_name,
        )

    async def _send_cli_command_with_reply(
        self,
        contact: Dict[str, Any],
        command_text: str,
        timeout_seconds: float,
        reply_matcher: Optional[Callable[[str], bool]] = None,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Send a remote CLI command and capture its matching CLI reply."""
        loop = asyncio.get_running_loop()
        reply_future = loop.create_future()
        expected_prefix = str(contact.get("public_key") or "")[:12].lower()

        def handle_reply(event: Any) -> None:
            payload = getattr(event, "payload", {}) or {}
            prefix = str(payload.get("pubkey_prefix") or "").lower()
            reply_text = str(payload.get("text") or "").strip()
            if (
                not reply_future.done()
                and prefix == expected_prefix
                and payload.get("txt_type") == 1
                and (reply_matcher is None or reply_matcher(reply_text))
            ):
                reply_future.set_result(reply_text)

        subscription = self.bot.meshcore.subscribe(
            EventType.CONTACT_MSG_RECV,
            handle_reply,
        )
        try:
            # Match the MeshCore app's acknowledged admin-text path. Repeater
            # firmware treats plain text from an authenticated admin as CLI,
            # while preserving this explicit Pi timestamp instead of replacing
            # it with the companion's transient unique clock.
            sent_result = await self.bot.meshcore.commands.send_msg(
                contact,
                command_text,
                timestamp=int(time.time()),
            )
            if (
                sent_result is None
                or getattr(sent_result, "type", None) == EventType.ERROR
            ):
                return sent_result, None
            try:
                reply_text = await asyncio.wait_for(
                    reply_future,
                    timeout=max(timeout_seconds, 1.0),
                )
            except asyncio.TimeoutError:
                reply_text = None
            return sent_result, reply_text
        finally:
            self.bot.meshcore.unsubscribe(subscription)

    @staticmethod
    def _clock_reply_epoch(reply_text: Optional[str]) -> Optional[int]:
        """Parse the repeater CLI's minute-resolution UTC clock response."""
        match = re.search(
            r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*UTC",
            reply_text or "",
        )
        if not match:
            return None
        hour, minute, day, month, year = map(int, match.groups())
        try:
            return int(
                datetime(
                    year,
                    month,
                    day,
                    hour,
                    minute,
                    tzinfo=timezone.utc,
                ).timestamp()
            )
        except ValueError:
            return None

    async def _clock_sync_by_node_key(
        self,
        requested_node_key: str,
        targets: List[RepeaterTarget],
    ) -> bool:
        """Reset, re-login, sync, and immediately advertise a repeater clock."""
        target = next(
            (
                candidate
                for candidate in targets
                if self._find_matching_key([candidate.node_key], requested_node_key)
            ),
            None,
        )
        if target is None:
            self._write_status(
                state="idle",
                requested_action="clock_sync",
                requested_node_key=requested_node_key,
                requested_target_found=False,
                clock_sync_success=False,
            )
            return False

        self._append_command_log(target, "Clock Sync started; resolving repeater contact")
        self._write_status(
            state="clock_sync",
            requested_action="clock_sync",
            requested_node_key=target.node_key,
            current_node_key=target.node_key,
            current_target=target.display_name,
        )
        try:
            await self.bot.meshcore.ensure_contacts()
            contact = await self._resolve_contact(target.node_key)
        except Exception as exc:
            self._append_command_log(target, f"Clock Sync contact lookup failed: {exc}", "error")
            contact = None
        if contact is None:
            self._append_command_log(target, "Clock Sync contact lookup failed", "error")
            self._write_status(
                state="idle",
                requested_action="clock_sync",
                requested_node_key=target.node_key,
                clock_sync_success=False,
                error="contact_not_found",
            )
            return False

        self._prime_contact_for_anon_requests(contact, target)
        login_ok = False
        clock_sync_success = False
        try:
            # Companion firmware replaces remote CLI timestamps with its own RTC
            # for replay protection. Validate that source before resetting the
            # repeater, otherwise `clock sync` can faithfully propagate a bad
            # companion time.
            host_epoch = int(time.time())
            companion_time_result = await self.bot.meshcore.commands.get_time()
            if (
                companion_time_result is None
                or getattr(companion_time_result, "type", None)
                != EventType.CURRENT_TIME
            ):
                self._append_command_log(
                    target,
                    "Clock Sync stopped: companion clock could not be read",
                    "error",
                )
                return False
            companion_epoch = int(companion_time_result.payload.get("time", 0))
            companion_drift = companion_epoch - host_epoch
            if companion_drift > 5:
                self._append_command_log(
                    target,
                    "Companion clock is "
                    f"{companion_drift}s ahead of the Pi; correcting its RTC, "
                    "rebooting it to clear replay state, and reconnecting",
                    "warning",
                )
                repair_clock = getattr(
                    self.bot, "repair_future_radio_clock", None
                )
                if repair_clock is None or not await repair_clock():
                    self._append_command_log(
                        target,
                        "Clock Sync stopped: companion clock recovery failed",
                        "error",
                    )
                    return False
                self._append_command_log(
                    target,
                    "Companion clock corrected and radio connection restored",
                    "success",
                )
                # The old contact belongs to the pre-reboot MeshCore instance.
                # Resolve it again from the freshly connected companion.
                contact = await self._resolve_contact(target)
                if contact is None:
                    self._append_command_log(
                        target,
                        "Clock Sync stopped: contact missing after companion reconnect",
                        "error",
                    )
                    return False
                self._prime_contact_for_anon_requests(contact, target)
                companion_time_result = await self.bot.meshcore.commands.get_time()
                companion_epoch = int(
                    (getattr(companion_time_result, "payload", {}) or {}).get(
                        "time", 0
                    )
                )
                host_epoch = int(time.time())
                companion_drift = companion_epoch - host_epoch
                if abs(companion_drift) > 5:
                    self._append_command_log(
                        target,
                        "Clock Sync stopped: repaired companion clock still differs "
                        f"from the Pi by {companion_drift}s",
                        "error",
                    )
                    return False
            if companion_drift < -5:
                companion_set_result = await self.bot.meshcore.commands.set_time(
                    host_epoch
                )
                if (
                    companion_set_result is None
                    or getattr(companion_set_result, "type", None) != EventType.OK
                ):
                    self._append_command_log(
                        target,
                        "Clock Sync stopped: companion clock could not be advanced",
                        "error",
                    )
                    return False
                companion_epoch = host_epoch
            self._append_command_log(
                target,
                "Verified companion clock source: "
                f"{datetime.fromtimestamp(companion_epoch).astimezone().isoformat()}",
                "success",
            )

            for attempt in range(1, self.login_retry_attempts + 1):
                login_ok, _ = await self._attempt_login(
                    contact,
                    target,
                    attempt,
                    timeout_seconds=self.manual_command_timeout_seconds,
                    password="",
                    require_admin=True,
                )
                if login_ok:
                    break
                if attempt == 1:
                    self._append_command_log(
                        target,
                        "Initial login timed out; resetting the saved path to flood",
                        "warning",
                    )
                    await self._reset_contact_path(contact)
                if attempt < self.login_retry_attempts:
                    await asyncio.sleep(self.retry_delay_seconds)

            if not login_ok:
                self._append_command_log(target, "Clock Sync stopped: login failed", "error")
                return False

            pre_reboot_server_epoch = getattr(
                self,
                "_last_login_server_timestamps",
                {},
            ).get(target.node_key)
            if not await self._reset_repeater_clock_if_needed(
                contact,
                target,
                pre_reboot_server_epoch,
                # Login can take several flood retries. Compare the repeater's
                # authenticated timestamp with the current trusted host time,
                # not the companion snapshot captured before those retries.
                int(time.time()),
            ):
                return False

            self._append_command_log(
                target,
                "Resetting the command route to flood for reliable delivery",
            )
            if await self._reset_contact_path(contact):
                self._append_command_log(
                    target,
                    "Clock Sync command route set to flood",
                    "success",
                )
            else:
                self._append_command_log(
                    target,
                    "Could not reset the command route; using the current route",
                    "warning",
                )
            self._append_command_log(
                target,
                'Waiting 2s so the clock-sync timestamp follows the login timestamp',
            )
            await asyncio.sleep(2)
            self._append_command_log(
                target,
                'Sending "clock sync" command from verified companion clock',
            )
            reply_timeout = max(self.manual_command_timeout_seconds, 30.0)
            sync_result, sync_reply = await self._send_cli_command_with_reply(
                contact,
                "clock sync",
                reply_timeout,
                lambda reply: reply.startswith("OK - clock set:")
                or "clock cannot go backwards" in reply.lower(),
            )
            sync_sent = (
                sync_result is not None
                and getattr(sync_result, "type", None) != EventType.ERROR
            )
            if not sync_sent:
                self._append_command_log(target, '"clock sync" command failed', "error")
                return False
            if not sync_reply:
                self._append_command_log(
                    target,
                    '"clock sync" was transmitted but its acknowledgement was not received; '
                    "continuing with repeater clock read-back",
                    "warning",
                )
            elif not sync_reply.startswith("OK - clock set:"):
                self._append_command_log(
                    target,
                    f'"clock sync" replied: {sync_reply}; validating the repeater clock directly',
                    "warning",
                )
            else:
                self._append_command_log(
                    target,
                    f'"clock sync" accepted: {sync_reply}',
                    "success",
                )

            await asyncio.sleep(2)
            self._append_command_log(
                target,
                "Reading the repeater clock from a fresh authenticated login",
            )
            login_ok = False
            for attempt in range(1, self.login_retry_attempts + 1):
                login_ok, _ = await self._attempt_login(
                    contact,
                    target,
                    attempt,
                    timeout_seconds=self.manual_command_timeout_seconds,
                    password="",
                    require_admin=True,
                )
                if login_ok:
                    break
                if attempt == 1:
                    await self._reset_contact_path(contact)
                if attempt < self.login_retry_attempts:
                    await asyncio.sleep(self.retry_delay_seconds)
            if not login_ok:
                self._append_command_log(
                    target,
                    "Clock Sync stopped: authenticated clock read-back failed",
                    "error",
                )
                return False
            remote_epoch = getattr(
                self,
                "_last_login_server_timestamps",
                {},
            ).get(target.node_key)
            if remote_epoch is None:
                self._append_command_log(
                    target,
                    "Clock Sync stopped: repeater login did not publish its clock",
                    "error",
                )
                return False
            remote_drift = remote_epoch - int(time.time())
            if abs(remote_drift) > 90:
                if remote_drift < -90:
                    recovered_epoch = await self._recover_replay_locked_clock(
                        contact,
                        target,
                        reply_timeout,
                        pre_recovery_epoch=remote_epoch,
                    )
                    if recovered_epoch is None:
                        return False
                    remote_epoch = recovered_epoch
                    remote_drift = remote_epoch - int(time.time())
                if abs(remote_drift) > 90:
                    self._append_command_log(
                        target,
                        "Clock Sync stopped: repeater verification is "
                        f"{remote_drift}s from Pi time",
                        "error",
                    )
                    return False
            remote_local = datetime.fromtimestamp(remote_epoch).astimezone().isoformat()
            self._append_command_log(
                target,
                f"Repeater clock verified at {remote_local} (authenticated read-back)",
                "success",
            )

            await asyncio.sleep(max(self.clock_sync_advert_delay_seconds, 2.0))

            advert_requested_at = time.time()
            advert_confirmed = False
            for advert_attempt in range(1, 3):
                attempt_suffix = f" (attempt {advert_attempt}/2)"
                self._append_command_log(
                    target,
                    f'Sending immediate "advert" command{attempt_suffix}',
                )
                advert_result, advert_reply = await self._send_cli_command_with_reply(
                    contact,
                    "advert",
                    reply_timeout,
                    lambda reply: reply.startswith("OK - Advert sent"),
                )
                advert_sent = (
                    advert_result is not None
                    and getattr(advert_result, "type", None) != EventType.ERROR
                )
                self._append_command_log(
                    target,
                    f'Immediate "advert" command transmitted{attempt_suffix}'
                    if advert_sent
                    else f'Immediate "advert" command failed{attempt_suffix}',
                    "success" if advert_sent else "warning",
                )
                if advert_reply and advert_reply.startswith("OK - Advert sent"):
                    self._append_command_log(
                        target,
                        f"Repeater confirmed immediate advert: {advert_reply}",
                        "success",
                    )
                    advert_confirmed = True
                    break
                if self._advert_observed_since(target.node_key, advert_requested_at):
                    self._append_command_log(
                        target,
                        "Immediate advert confirmed by receiving the repeater's fresh advertisement",
                        "success",
                    )
                    advert_confirmed = True
                    break
                if advert_attempt < 2:
                    self._append_command_log(
                        target,
                        "Immediate advert was not confirmed; retrying after 2s",
                        "warning",
                    )
                    await asyncio.sleep(2)

            if not advert_confirmed:
                self._append_command_log(
                    target,
                    "Immediate advert was not confirmed after 2 attempts",
                    "error",
                )
                return False

            self._append_command_log(
                target,
                "Clock Sync completed and immediate advert requested",
                "success",
            )
            clock_sync_success = True
            return True
        except Exception as exc:
            self._append_command_log(target, f"Clock Sync failed: {exc}", "error")
            self.logger.warning("Clock Sync failed for %s: %s", target.display_name, exc)
            return False
        finally:
            if login_ok:
                try:
                    self._append_command_log(target, "Sending logout command")
                    await self.bot.meshcore.commands.send_logout(contact)
                    self._append_command_log(target, "Logout command completed", "success")
                except Exception:
                    self._append_command_log(target, "Logout command failed", "warning")
            # The detailed command log carries command-level failures; this final
            # state releases the public button even if a command raised an error.
            self._write_status(
                state="idle",
                requested_action="clock_sync",
                requested_node_key=target.node_key,
                current_target=target.display_name,
                clock_sync_finished_at=time.time(),
                login_ok=login_ok,
                clock_sync_success=clock_sync_success,
            )

    async def _reset_repeater_clock_if_needed(
        self,
        contact: Dict[str, Any],
        target: RepeaterTarget,
        remote_epoch: Optional[int],
        source_epoch: int,
    ) -> bool:
        """Reset only clocks that cannot be advanced with ``clock sync``."""
        if remote_epoch is not None and remote_epoch <= source_epoch + 5:
            drift = remote_epoch - source_epoch
            self._append_command_log(
                target,
                "Repeater clock is behind or within the 5s forward-sync tolerance "
                f"(drift={drift}s); skipping clkreboot and syncing directly",
                "success",
            )
            return True

        self._append_command_log(
            target,
            "Repeater clock is ahead or unavailable; clkreboot is required before sync",
            "warning",
        )
        self._append_command_log(
            target,
            "Resetting the clock-reboot route to flood for reliable delivery",
        )
        await self._reset_contact_path(contact)
        self._append_command_log(
            target,
            "Waiting 2s so the clock-reboot timestamp follows the login timestamp",
        )
        await asyncio.sleep(2)
        self._append_command_log(target, 'Sending "clkreboot" command')
        reboot_result = await self.bot.meshcore.commands.send_msg(
            contact,
            "clkreboot",
            timestamp=int(time.time()),
        )
        reboot_sent = (
            reboot_result is not None
            and getattr(reboot_result, "type", None) != EventType.ERROR
        )
        self._append_command_log(
            target,
            '"clkreboot" command sent' if reboot_sent else '"clkreboot" command failed',
            "success" if reboot_sent else "error",
        )
        if not reboot_sent:
            return False

        if self.clock_sync_command_delay_seconds > 0:
            self._append_command_log(
                target,
                f"Waiting {self.clock_sync_command_delay_seconds:.1f}s for repeater reboot",
            )
            await asyncio.sleep(self.clock_sync_command_delay_seconds)

        login_ok = False
        self._append_command_log(target, "Re-authenticating after repeater reboot")
        for attempt in range(1, self.login_retry_attempts + 1):
            login_ok, _ = await self._attempt_login(
                contact,
                target,
                attempt,
                timeout_seconds=self.manual_command_timeout_seconds,
                password="",
                require_admin=True,
            )
            if login_ok:
                break
            if attempt == 1:
                self._append_command_log(
                    target,
                    "Initial post-reboot login timed out; resetting path to flood",
                    "warning",
                )
                await self._reset_contact_path(contact)
            if attempt < self.login_retry_attempts:
                await asyncio.sleep(self.retry_delay_seconds)
        if not login_ok:
            self._append_command_log(
                target,
                "Clock Sync stopped: post-reboot login failed",
                "error",
            )
            return False

        post_reboot_epoch = getattr(
            self,
            "_last_login_server_timestamps",
            {},
        ).get(target.node_key)
        clock_reset_epoch = 1715770351
        if (
            remote_epoch is not None
            and post_reboot_epoch is not None
            and remote_epoch > clock_reset_epoch + 60
            and post_reboot_epoch >= remote_epoch - 5
        ):
            self._append_command_log(
                target,
                "Clock Sync stopped: clkreboot did not reset the repeater clock. "
                "Its admin replay counter is locked ahead; reboot it physically or "
                "send clkreboot from a different admin device before retrying.",
                "error",
            )
            return False
        return True

    async def _recover_replay_locked_clock(
        self,
        contact: Dict[str, Any],
        target: RepeaterTarget,
        reply_timeout: float,
        pre_recovery_epoch: Optional[int] = None,
    ) -> Optional[int]:
        """Clear a future per-admin replay counter and return verified epoch."""
        self._append_command_log(
            target,
            "Clock command was ignored despite admin login; attempting one-time "
            "replay-counter recovery",
            "warning",
        )
        await self._reset_contact_path(contact)
        reboot_result = await self.bot.meshcore.commands.send_msg(
            contact,
            "clkreboot",
            timestamp=MAX_REPLAY_TIMESTAMP,
        )
        if reboot_result is None or getattr(reboot_result, "type", None) == EventType.ERROR:
            self._append_command_log(
                target,
                "Replay recovery clkreboot could not be transmitted",
                "error",
            )
            return None
        self._append_command_log(
            target,
            "Maximum-timestamp clkreboot transmitted; waiting for repeater restart",
            "success",
        )
        await asyncio.sleep(max(self.clock_sync_command_delay_seconds, 10.0))
        await self._reset_contact_path(contact)

        login_ok = False
        for attempt in range(1, self.login_retry_attempts + 1):
            login_ok, _ = await self._attempt_login(
                contact,
                target,
                attempt,
                timeout_seconds=self.manual_command_timeout_seconds,
                password="",
                require_admin=True,
            )
            if login_ok:
                break
            if attempt < self.login_retry_attempts:
                await asyncio.sleep(self.retry_delay_seconds)
        if not login_ok:
            self._append_command_log(
                target,
                "Replay recovery failed: repeater did not return after clkreboot",
                "error",
            )
            return None

        reset_epoch = getattr(
            self,
            "_last_login_server_timestamps",
            {},
        ).get(target.node_key)
        # The firmware's fallback RTC epoch varies by build.  A successful
        # clkreboot is proven by a substantial backward move from the fresh
        # authenticated pre-reboot reading, not by one hard-coded date.
        reset_not_confirmed = reset_epoch is None
        if pre_recovery_epoch is not None and reset_epoch is not None:
            reset_not_confirmed = reset_epoch >= pre_recovery_epoch - 60
        elif reset_epoch is not None:
            reset_not_confirmed = reset_epoch > int(time.time()) - 300
        if reset_not_confirmed:
            self._append_command_log(
                target,
                "Replay recovery failed: clkreboot did not reset the repeater. "
                "A physical reboot or a different admin identity is required.",
                "error",
            )
            return None

        await asyncio.sleep(2)
        await self._reset_contact_path(contact)
        self._append_command_log(
            target,
            'Replay counter cleared; sending normal "clock sync"',
        )
        sync_result, _ = await self._send_cli_command_with_reply(
            contact,
            "clock sync",
            reply_timeout,
            lambda reply: reply.startswith("OK - clock set:")
            or "clock cannot go backwards" in reply.lower(),
        )
        if sync_result is None or getattr(sync_result, "type", None) == EventType.ERROR:
            self._append_command_log(
                target,
                "Replay recovery failed: normal clock sync could not be transmitted",
                "error",
            )
            return None

        await asyncio.sleep(2)
        login_ok, _ = await self._attempt_login(
            contact,
            target,
            1,
            timeout_seconds=self.manual_command_timeout_seconds,
            password="",
            require_admin=True,
        )
        if not login_ok:
            self._append_command_log(
                target,
                "Replay recovery failed: final authenticated read-back timed out",
                "error",
            )
            return None
        verified_epoch = getattr(
            self,
            "_last_login_server_timestamps",
            {},
        ).get(target.node_key)
        if verified_epoch is None:
            return None
        drift = verified_epoch - int(time.time())
        if abs(drift) > 90:
            self._append_command_log(
                target,
                f"Replay recovery failed: repeater remains {drift}s from Pi time",
                "error",
            )
            return None
        self._append_command_log(
            target,
            f"Replay counter cleared and repeater clock verified (drift={drift}s)",
            "success",
        )
        return verified_epoch

    def _advert_observed_since(self, node_key: str, requested_at: float) -> bool:
        """Return true when contact tracking saw a fresh advert for this node."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """SELECT last_advert_timestamp
                       FROM complete_contact_tracking
                       WHERE lower(public_key) = lower(?)
                       ORDER BY last_advert_timestamp DESC
                       LIMIT 1""",
                    (node_key,),
                ).fetchone()
            if not row or not row[0]:
                return False
            observed = datetime.fromisoformat(str(row[0]))
            return observed.timestamp() >= requested_at - 2
        except (sqlite3.Error, TypeError, ValueError, OSError):
            return False

    def _should_attempt_login(self, node_key: str) -> bool:
        failure_count = self._consecutive_failures.get(node_key, 0)
        if failure_count < self.login_after_failures:
            return False
        last_login_time = self._last_login_times.get(node_key, 0.0)
        return (time.time() - last_login_time) >= self.login_cooldown_seconds

    def _mark_target_success(self, target: RepeaterTarget) -> None:
        self._consecutive_failures[target.node_key] = 0
        self._next_target_update_times[target.node_key] = time.time() + max(
            float(self.poll_interval_seconds),
            60.0,
        )

    def _mark_target_failure(self, target: RepeaterTarget, reason: str) -> None:
        failure_count = self._consecutive_failures.get(target.node_key, 0) + 1
        self._consecutive_failures[target.node_key] = failure_count
        delay = self._failure_backoff_seconds(failure_count)
        self._next_target_update_times[target.node_key] = time.time() + delay
        self.logger.info(
            "Repeater monitor backing off %s for %.1fs after failure %s (%s)",
            target.display_name,
            delay,
            failure_count,
            reason,
        )

    def _failure_backoff_seconds(self, failure_count: int) -> float:
        update_interval = max(float(self.poll_interval_seconds), 60.0)
        maximum_backoff = max(update_interval, self.max_failure_backoff_seconds)
        # A silent repeater should create less traffic, not be retried more
        # aggressively than a healthy one. First failure waits one normal
        # interval; subsequent failures exponentially approach the ceiling.
        exponent = max(0, failure_count - 1)
        return min(update_interval * (self.backoff_base ** exponent), maximum_backoff)

    async def _attempt_login(
        self,
        contact: Dict[str, Any],
        target: RepeaterTarget,
        attempt: int,
        timeout_seconds: Optional[float] = None,
        password: str = "",
        require_admin: bool = False,
        attempts_total: Optional[int] = None,
    ) -> Tuple[bool, Optional[str]]:
        displayed_attempts_total = (
            self.login_retry_attempts if attempts_total is None else attempts_total
        )
        self._append_command_log(
            target,
            f"Sending login command (attempt {attempt}/{displayed_attempts_total})",
        )
        try:
            login_result = await self.bot.meshcore.commands.send_login_sync(
                contact,
                password,
                timeout=self.command_timeout_seconds if timeout_seconds is None else timeout_seconds,
            )
        except Exception as exc:
            self._append_command_log(
                target,
                f"Login command raised an error: {exc}",
                "error",
            )
            self.logger.warning(
                "Repeater monitor login exception for %s (attempt %s/%s): %s",
                target.display_name,
                attempt,
                self.login_retry_attempts,
                exc,
            )
            return False, f"login_exception:{exc}"

        if login_result is None:
            self._append_command_log(target, "Login command timed out", "warning")
            return False, "login_failed"

        login_ok = getattr(login_result, "type", None) == EventType.LOGIN_SUCCESS
        login_payload = getattr(login_result, "payload", {}) or {}
        expected_prefix = str(contact.get("public_key") or target.node_key)[:12].lower()
        response_prefix = str(login_payload.get("pubkey_prefix") or "").lower()
        if login_ok and response_prefix and response_prefix != expected_prefix:
            # send_login_sync in meshcore 2.3.8 waits for any LOGIN_SUCCESS on
            # the busy radio. Never treat another user's/repeater's concurrent
            # login as proof that this target authenticated.
            self._append_command_log(
                target,
                f"Ignored login response from another repeater ({response_prefix})",
                "warning",
            )
            return False, "login_response_target_mismatch"
        server_timestamp = login_payload.get("server_timestamp")
        if login_ok and server_timestamp is not None:
            try:
                server_timestamp = int(server_timestamp)
                timestamps = getattr(self, "_last_login_server_timestamps", None)
                if timestamps is None:
                    timestamps = self._last_login_server_timestamps = {}
                timestamps[target.node_key] = server_timestamp
                server_time = datetime.fromtimestamp(
                    server_timestamp,
                    tz=timezone.utc,
                ).isoformat()
                self._append_command_log(
                    target,
                    f"Repeater login clock reports {server_time}",
                )
            except (TypeError, ValueError, OSError):
                pass
        if login_ok and require_admin and not login_payload.get("is_admin", False):
            self._append_command_log(
                target,
                "Login accepted with guest access, but Clock Sync requires the bot "
                "to be in this repeater's admin ACL",
                "error",
            )
            return False, "admin_access_required"
        self._append_command_log(
            target,
            (
                "Admin login accepted"
                if login_ok and require_admin
                else "Login accepted"
                if login_ok
                else "Login rejected"
            ),
            "success" if login_ok else "warning",
        )
        return login_ok, None if login_ok else "login_failed"

    async def _collect_repeater_data(
        self,
        *,
        contact: Dict[str, Any],
        target: RepeaterTarget,
        status_payload: Optional[Dict[str, Any]],
        telemetry_payload: Optional[Any],
        request_attempts: int,
        request_retry_delay_seconds: float,
        request_timeout_seconds: float,
        request_attempt_offset: int = 0,
        request_attempt_total: Optional[int] = None,
    ) -> Tuple[
        Optional[Dict[str, Any]],
        Optional[Any],
        Optional[str],
    ]:
        last_error: Optional[str] = None
        displayed_attempts_total = request_attempt_total or request_attempts
        for request_attempt in range(1, request_attempts + 1):
            displayed_attempt = request_attempt_offset + request_attempt
            if status_payload is None:
                self._append_command_log(
                    target,
                    f"Sending status command (attempt {displayed_attempt}/{displayed_attempts_total}, timeout {request_timeout_seconds:.1f}s)",
                )
                status_started_monotonic = time.monotonic()
                try:
                    candidate_status_payload = await self.bot.meshcore.commands.req_status_sync(
                        contact,
                        timeout=request_timeout_seconds,
                    )
                    status_elapsed_seconds = time.monotonic() - status_started_monotonic
                    if candidate_status_payload is not None:
                        status_payload = candidate_status_payload
                        last_error = None
                        self.logger.info(
                            "Repeater monitor status request for %s succeeded in %.1fs (attempt %s/%s, timeout %.1fs)",
                            target.display_name,
                            status_elapsed_seconds,
                            displayed_attempt,
                            displayed_attempts_total,
                            request_timeout_seconds,
                        )
                        self._append_command_log(
                            target,
                            f"Status response received in {status_elapsed_seconds:.1f}s",
                            "success",
                        )
                    else:
                        self.logger.info(
                            "Repeater monitor status request for %s timed out in %.1fs (attempt %s/%s, timeout %.1fs)",
                            target.display_name,
                            status_elapsed_seconds,
                            displayed_attempt,
                            displayed_attempts_total,
                            request_timeout_seconds,
                        )
                        self._append_command_log(
                            target,
                            f"Status command timed out after {status_elapsed_seconds:.1f}s",
                            "warning",
                        )
                except Exception as exc:
                    status_elapsed_seconds = time.monotonic() - status_started_monotonic
                    last_error = f"status_exception:{exc}"
                    self.logger.warning(
                        "Repeater monitor status failed for %s in %.1fs (data attempt %s/%s, timeout %.1fs): %s",
                        target.display_name,
                        status_elapsed_seconds,
                        displayed_attempt,
                        displayed_attempts_total,
                        request_timeout_seconds,
                        exc,
                    )
                    self._append_command_log(
                        target,
                        f"Status command failed after {status_elapsed_seconds:.1f}s: {exc}",
                        "error",
                    )

            # Temperature is supplementary.  Only ask for it after the
            # repeater has answered status, otherwise every silent device
            # doubles its airtime and leaves another response window pending.
            if (
                self.collect_temperature
                and status_payload is not None
                and telemetry_payload is None
            ):
                self._append_command_log(
                    target,
                    f"Sending telemetry command (attempt {displayed_attempt}/{displayed_attempts_total}, timeout {request_timeout_seconds:.1f}s)",
                )
                telemetry_started_monotonic = time.monotonic()
                try:
                    candidate_telemetry_payload = await self.bot.meshcore.commands.req_telemetry_sync(
                        contact,
                        timeout=request_timeout_seconds,
                    )
                    telemetry_elapsed_seconds = time.monotonic() - telemetry_started_monotonic
                    if candidate_telemetry_payload is not None:
                        telemetry_payload = candidate_telemetry_payload
                        last_error = None
                        self.logger.info(
                            "Repeater monitor telemetry request for %s succeeded in %.1fs (attempt %s/%s, timeout %.1fs)",
                            target.display_name,
                            telemetry_elapsed_seconds,
                            displayed_attempt,
                            displayed_attempts_total,
                            request_timeout_seconds,
                        )
                        self._append_command_log(
                            target,
                            f"Telemetry response received in {telemetry_elapsed_seconds:.1f}s",
                            "success",
                        )
                        break
                    else:
                        self.logger.info(
                            "Repeater monitor telemetry request for %s timed out in %.1fs (attempt %s/%s, timeout %.1fs)",
                            target.display_name,
                            telemetry_elapsed_seconds,
                            displayed_attempt,
                            displayed_attempts_total,
                            request_timeout_seconds,
                        )
                        self._append_command_log(
                            target,
                            f"Telemetry command timed out after {telemetry_elapsed_seconds:.1f}s",
                            "warning",
                        )
                except Exception as exc:
                    telemetry_elapsed_seconds = time.monotonic() - telemetry_started_monotonic
                    last_error = f"telemetry_exception:{exc}"
                    self.logger.warning(
                        "Repeater monitor telemetry failed for %s in %.1fs (data attempt %s/%s, timeout %.1fs): %s",
                        target.display_name,
                        telemetry_elapsed_seconds,
                        displayed_attempt,
                        displayed_attempts_total,
                        request_timeout_seconds,
                        exc,
                    )
                    self._append_command_log(
                        target,
                        f"Telemetry command failed after {telemetry_elapsed_seconds:.1f}s: {exc}",
                        "error",
                    )

            if request_attempt < request_attempts:
                self._append_command_log(
                    target,
                    f"Waiting {request_retry_delay_seconds:.1f}s before the next command attempt",
                )
                await asyncio.sleep(request_retry_delay_seconds)

        if status_payload is None and telemetry_payload is None and last_error is None:
            last_error = "status_response_missing"

        return (
            status_payload,
            telemetry_payload,
            last_error,
        )

    def _extract_battery_from_telemetry(
        self,
        telemetry_payload: Optional[Any],
    ) -> Tuple[Optional[int], Optional[int]]:
        if not isinstance(telemetry_payload, list):
            return None, None

        battery_mv: Optional[int] = None
        battery_percent: Optional[int] = None
        for entry in telemetry_payload:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type") or "").strip().lower()
            value = entry.get("value")

            if entry_type == "voltage" and isinstance(value, (int, float)):
                if 0 < float(value) < 20:
                    battery_mv = int(round(float(value) * 1000))
            elif entry_type == "percentage" and isinstance(value, (int, float)):
                battery_percent = max(0, min(100, int(round(float(value)))))

        return battery_mv, battery_percent

    def _extract_temperature_from_telemetry(
        self,
        telemetry_payload: Optional[Any],
    ) -> Optional[float]:
        """Return the first Cayenne LPP temperature value published by a repeater."""
        if not isinstance(telemetry_payload, list):
            return None

        for entry in telemetry_payload:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type") or "").strip().lower()
            value = entry.get("value")
            if entry_type in {"temperature", "temp"} and isinstance(value, (int, float)):
                return round(float(value), 2)
        return None

    async def _resolve_contact(self, node_key: str) -> Optional[Dict[str, Any]]:
        contact = self.bot.meshcore.get_contact_by_key_prefix(node_key)
        if contact:
            return contact

        contact = self.bot.meshcore.get_contact_by_name(node_key)
        if contact:
            return contact

        try:
            await self.bot.meshcore.ensure_contacts(follow=True)
        except Exception:
            return None

        contact = self.bot.meshcore.get_contact_by_key_prefix(node_key)
        if contact:
            return contact
        return self.bot.meshcore.get_contact_by_name(node_key)

    async def _refresh_contact_path(
        self,
        contact: Dict[str, Any],
        target: RepeaterTarget,
    ) -> None:
        self._append_command_log(target, "Sending reset_path after repeated failures")
        try:
            reset_result = await self.bot.meshcore.commands.reset_path(contact)
            reset_ok = reset_result is not None and getattr(reset_result, "type", None) != EventType.ERROR
            self._append_command_log(
                target,
                "reset_path completed" if reset_ok else "reset_path returned no confirmation",
                "success" if reset_ok else "warning",
            )
        except Exception as exc:
            self._append_command_log(target, f"reset_path failed: {exc}", "error")
        self._append_command_log(target, "Sending path discovery command")
        try:
            discovery_result = await self.bot.meshcore.commands.send_path_discovery_sync(
                contact,
                timeout=self.command_timeout_seconds,
            )
            self._append_command_log(
                target,
                "Path discovery completed" if discovery_result is not None else "Path discovery timed out",
                "success" if discovery_result is not None else "warning",
            )
        except Exception as exc:
            self._append_command_log(target, f"Path discovery failed: {exc}", "error")

    async def _reset_contact_path(self, contact: Dict[str, Any]) -> bool:
        try:
            res = await self.bot.meshcore.commands.reset_path(contact)
        except Exception:
            return False
        if res is None or getattr(res, "type", None) == EventType.ERROR:
            return False
        contact["out_path"] = ""
        contact["out_path_len"] = -1
        contact["out_path_hash_mode"] = self._configured_path_hash_mode()
        return True

    def _configured_path_hash_mode(self) -> int:
        try:
            return max(
                0,
                min(2, self.bot.config.getint("Bot", "path_hash_mode", fallback=0)),
            )
        except (AttributeError, TypeError, ValueError):
            return 0

    def _prime_contact_for_anon_requests(
        self,
        contact: Dict[str, Any],
        target: RepeaterTarget,
    ) -> bool:
        out_path: Optional[str] = None
        out_path_len: Optional[int] = None
        out_bytes_per_hop: Optional[int] = None
        path_hash_mode = self._configured_path_hash_mode()

        current_path = (contact.get("out_path") or "").strip().lower()
        current_len = contact.get("out_path_len")
        current_hash_mode = contact.get("out_path_hash_mode")
        current_has_direct_path = (
            bool(current_path)
            and isinstance(current_len, int)
            and current_len > 0
            and current_len != -1
        )

        if target.fixed_out_path:
            out_path = target.fixed_out_path
            out_bytes_per_hop = path_hash_mode + 1
            if len(out_path) % (out_bytes_per_hop * 2) != 0:
                self._append_command_log(
                    target,
                    f"Ignoring fixed route {out_path}: it is not aligned to "
                    f"{out_bytes_per_hop}-byte routing",
                    "error",
                )
                return False
            out_path_len = max(1, len(out_path) // (out_bytes_per_hop * 2))
        elif current_has_direct_path and current_hash_mode == path_hash_mode:
            # Prefer the route the companion currently knows for this contact.
            # A live device route is safer than any path retained in our DB.
            return False
        elif current_has_direct_path:
            # Changing the companion globally to 2-byte paths does not migrate
            # existing contacts. A stale one-byte contact can still complete a
            # flood login while encoding an unusable one-byte binary reply
            # path. Promote it only when our recently observed route has the
            # exact byte width required by the configured mode.
            tracked = self._lookup_tracked_contact_path(target.node_key)
            if tracked:
                candidate_path = str(tracked.get("out_path") or "").strip().lower()
                candidate_len = tracked.get("out_path_len")
                bytes_per_hop = path_hash_mode + 1
                if (
                    isinstance(candidate_len, int)
                    and candidate_len > 0
                    and len(candidate_path) == candidate_len * bytes_per_hop * 2
                ):
                    out_path = candidate_path
                    out_path_len = candidate_len
                    out_bytes_per_hop = bytes_per_hop
                    self._append_command_log(
                        target,
                        f"Replacing stale {current_hash_mode + 1}-byte route {current_path} "
                        f"with observed {bytes_per_hop}-byte route {out_path}",
                        "warning",
                    )
                else:
                    return False
            else:
                return False
        else:
            # Keep flood routing after reset/login. Re-introducing a path from
            # contact tracking here can silently replace a route that just
            # worked with an older observed path. Path discovery or a future
            # live contact update may establish a new direct route safely.
            return False

        if not isinstance(out_path_len, int) or out_path_len <= 0:
            bytes_per_hop = max(1, path_hash_mode + 1)
            out_path_len = max(1, len(out_path) // (bytes_per_hop * 2))

        if (
            current_path == out_path
            and current_len == out_path_len
            and current_hash_mode == path_hash_mode
        ):
            return False

        contact["out_path"] = out_path
        contact["out_path_len"] = out_path_len
        contact["out_path_hash_mode"] = path_hash_mode
        self.logger.info(
            "Repeater monitor primed anon path for %s via %s (len=%s, bytes_per_hop=%s)",
            contact.get("adv_name") or target.node_key[:12],
            out_path,
            out_path_len,
            out_bytes_per_hop if out_bytes_per_hop else path_hash_mode + 1,
        )
        return True

    def _lookup_tracked_contact_path(self, node_key: str) -> Optional[Dict[str, Any]]:
        candidate = (node_key or "").strip().lower()
        if not candidate:
            return None

        try:
            with sqlite3.connect(self.db_path, timeout=60) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                row = cursor.execute(
                    """
                    SELECT public_key, out_path, out_path_len, out_bytes_per_hop
                    FROM complete_contact_tracking
                    WHERE lower(public_key) = ?
                    OR lower(public_key) LIKE ?
                    ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                    LIMIT 1
                    """,
                    (candidate, f"{candidate}%"),
                ).fetchone()
        except sqlite3.Error as exc:
            self.logger.warning("Repeater monitor path lookup failed for %s: %s", candidate[:12], exc)
            return None

        return dict(row) if row else None

    def _battery_percent(self, battery_mv: Optional[int]) -> Optional[int]:
        if battery_mv is None:
            return None
        percent = round(((battery_mv - 3300) / 900) * 100)
        return max(0, min(100, percent))

    def _latest_advert_clock_snapshot(
        self,
        *,
        resolved_public_key: Optional[str],
        node_key: str,
        display_name: str,
    ) -> Tuple[Optional[int], Optional[float]]:
        public_key = (resolved_public_key or "").strip()
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            row = cursor.execute(
                """
                SELECT raw_advert_data, last_advert_timestamp, last_heard
                FROM complete_contact_tracking
                WHERE role IN ('repeater', 'roomserver')
                  AND raw_advert_data IS NOT NULL
                  AND (
                        lower(public_key) = lower(?)
                     OR lower(public_key) LIKE lower(?) || '%'
                     OR lower(?) LIKE lower(public_key) || '%'
                     OR lower(name) = lower(?)
                     OR lower(name) = lower(?)
                  )
                ORDER BY COALESCE(last_advert_timestamp, last_heard) DESC
                LIMIT 1
                """,
                (
                    public_key,
                    public_key,
                    public_key,
                    (node_key or "").strip(),
                    (display_name or "").strip(),
                ),
            ).fetchone()

        if row is None:
            return None, None

        try:
            advert_data = json.loads(row["raw_advert_data"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, None

        advert_epoch = advert_data.get("advert_time")
        if advert_epoch is None:
            return None, None

        try:
            advert_epoch = int(advert_epoch)
        except (TypeError, ValueError):
            return None, None

        heard_at = self._parse_db_timestamp(row["last_advert_timestamp"] or row["last_heard"])
        if heard_at is None:
            return advert_epoch, None

        return advert_epoch, float(advert_epoch - heard_at)

    def _parse_db_timestamp(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()
        if not text:
            return None

        try:
            return float(text)
        except ValueError:
            pass

        normalized = text.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).timestamp()
        except ValueError:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue
        return None

    def _store_result(
        self,
        *,
        target: RepeaterTarget,
        collected_at: float,
        login_ok: bool,
        status_ok: bool,
        clock_ok: bool,
        login_attempts: int,
        battery_mv: Optional[int] = None,
        battery_percent: Optional[int] = None,
        temperature_c: Optional[float] = None,
        uptime_seconds: Optional[int] = None,
        airtime_seconds: Optional[int] = None,
        rx_airtime_seconds: Optional[int] = None,
        last_rssi: Optional[int] = None,
        last_snr: Optional[float] = None,
        noise_floor: Optional[int] = None,
        tx_queue_len: Optional[int] = None,
        nb_recv: Optional[int] = None,
        nb_sent: Optional[int] = None,
        recv_errors: Optional[int] = None,
        sent_flood: Optional[int] = None,
        sent_direct: Optional[int] = None,
        recv_flood: Optional[int] = None,
        recv_direct: Optional[int] = None,
        full_events: Optional[int] = None,
        direct_dups: Optional[int] = None,
        flood_dups: Optional[int] = None,
        clock_epoch: Optional[int] = None,
        clock_drift_seconds: Optional[float] = None,
        poll_duration_seconds: Optional[float] = None,
        error_text: Optional[str] = None,
        resolved_public_key: Optional[str] = None,
        last_contact_name: Optional[str] = None,
    ) -> None:
        last_success_at = collected_at if (
            status_ok
            or battery_mv is not None
            or battery_percent is not None
            or temperature_c is not None
            or clock_epoch is not None
        ) else None
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO repeater_monitor_samples (
                    node_key, resolved_public_key, display_name, collected_at,
                    login_ok, status_ok, clock_ok, battery_mv, battery_percent, temperature_c, uptime_seconds,
                    airtime_seconds, rx_airtime_seconds, last_rssi, last_snr, noise_floor,
                    tx_queue_len, nb_recv, nb_sent, recv_errors, sent_flood, sent_direct,
                    recv_flood, recv_direct, full_events, direct_dups, flood_dups,
                    clock_epoch, clock_drift_seconds, poll_duration_seconds, login_attempts, error_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target.node_key,
                    resolved_public_key,
                    target.display_name,
                    collected_at,
                    int(login_ok),
                    int(status_ok),
                    int(clock_ok),
                    battery_mv,
                    battery_percent,
                    temperature_c,
                    uptime_seconds,
                    airtime_seconds,
                    rx_airtime_seconds,
                    last_rssi,
                    last_snr,
                    noise_floor,
                    tx_queue_len,
                    nb_recv,
                    nb_sent,
                    recv_errors,
                    sent_flood,
                    sent_direct,
                    recv_flood,
                    recv_direct,
                    full_events,
                    direct_dups,
                    flood_dups,
                    clock_epoch,
                    clock_drift_seconds,
                    poll_duration_seconds,
                    login_attempts,
                    error_text,
                ),
            )
            cursor.execute(
                """
                INSERT INTO repeater_monitor_nodes (
                    node_key, display_name, resolved_public_key, last_contact_name,
                    last_attempt_at, last_success_at, last_login_ok, last_status_ok,
                    last_clock_ok, last_battery_mv, last_battery_percent, last_temperature_c, last_uptime_seconds,
                    last_airtime_seconds, last_rx_airtime_seconds, last_rssi, last_snr,
                    last_noise_floor, last_tx_queue_len, last_nb_recv, last_nb_sent,
                    last_recv_errors, last_sent_flood, last_sent_direct, last_recv_flood,
                    last_recv_direct, last_full_events, last_direct_dups, last_flood_dups,
                    last_clock_epoch, last_clock_drift_seconds, last_poll_duration_seconds, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_key) DO UPDATE SET
                    display_name=excluded.display_name,
                    resolved_public_key=excluded.resolved_public_key,
                    last_contact_name=excluded.last_contact_name,
                    last_attempt_at=excluded.last_attempt_at,
                    last_success_at=COALESCE(excluded.last_success_at, repeater_monitor_nodes.last_success_at),
                    last_login_ok=excluded.last_login_ok,
                    last_status_ok=excluded.last_status_ok,
                    last_clock_ok=excluded.last_clock_ok,
                    last_battery_mv=COALESCE(excluded.last_battery_mv, repeater_monitor_nodes.last_battery_mv),
                    last_battery_percent=COALESCE(excluded.last_battery_percent, repeater_monitor_nodes.last_battery_percent),
                    last_temperature_c=COALESCE(excluded.last_temperature_c, repeater_monitor_nodes.last_temperature_c),
                    last_uptime_seconds=COALESCE(excluded.last_uptime_seconds, repeater_monitor_nodes.last_uptime_seconds),
                    last_airtime_seconds=COALESCE(excluded.last_airtime_seconds, repeater_monitor_nodes.last_airtime_seconds),
                    last_rx_airtime_seconds=COALESCE(excluded.last_rx_airtime_seconds, repeater_monitor_nodes.last_rx_airtime_seconds),
                    last_rssi=COALESCE(excluded.last_rssi, repeater_monitor_nodes.last_rssi),
                    last_snr=COALESCE(excluded.last_snr, repeater_monitor_nodes.last_snr),
                    last_noise_floor=COALESCE(excluded.last_noise_floor, repeater_monitor_nodes.last_noise_floor),
                    last_tx_queue_len=COALESCE(excluded.last_tx_queue_len, repeater_monitor_nodes.last_tx_queue_len),
                    last_nb_recv=COALESCE(excluded.last_nb_recv, repeater_monitor_nodes.last_nb_recv),
                    last_nb_sent=COALESCE(excluded.last_nb_sent, repeater_monitor_nodes.last_nb_sent),
                    last_recv_errors=COALESCE(excluded.last_recv_errors, repeater_monitor_nodes.last_recv_errors),
                    last_sent_flood=COALESCE(excluded.last_sent_flood, repeater_monitor_nodes.last_sent_flood),
                    last_sent_direct=COALESCE(excluded.last_sent_direct, repeater_monitor_nodes.last_sent_direct),
                    last_recv_flood=COALESCE(excluded.last_recv_flood, repeater_monitor_nodes.last_recv_flood),
                    last_recv_direct=COALESCE(excluded.last_recv_direct, repeater_monitor_nodes.last_recv_direct),
                    last_full_events=COALESCE(excluded.last_full_events, repeater_monitor_nodes.last_full_events),
                    last_direct_dups=COALESCE(excluded.last_direct_dups, repeater_monitor_nodes.last_direct_dups),
                    last_flood_dups=COALESCE(excluded.last_flood_dups, repeater_monitor_nodes.last_flood_dups),
                    last_clock_epoch=COALESCE(excluded.last_clock_epoch, repeater_monitor_nodes.last_clock_epoch),
                    last_clock_drift_seconds=COALESCE(excluded.last_clock_drift_seconds, repeater_monitor_nodes.last_clock_drift_seconds),
                    last_poll_duration_seconds=excluded.last_poll_duration_seconds,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    target.node_key,
                    target.display_name,
                    resolved_public_key,
                    last_contact_name,
                    collected_at,
                    last_success_at,
                    int(login_ok),
                    int(status_ok),
                    int(clock_ok),
                    battery_mv,
                    battery_percent,
                    temperature_c,
                    uptime_seconds,
                    airtime_seconds,
                    rx_airtime_seconds,
                    last_rssi,
                    last_snr,
                    noise_floor,
                    tx_queue_len,
                    nb_recv,
                    nb_sent,
                    recv_errors,
                    sent_flood,
                    sent_direct,
                    recv_flood,
                    recv_direct,
                    full_events,
                    direct_dups,
                    flood_dups,
                    clock_epoch,
                    clock_drift_seconds,
                    poll_duration_seconds,
                    error_text,
                    collected_at,
                ),
            )
            conn.commit()

    def _purge_old_samples(self) -> None:
        cutoff = time.time() - (self.retention_days * 86400)
        with sqlite3.connect(self.db_path, timeout=60) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM repeater_monitor_samples WHERE collected_at < ?",
                (cutoff,),
            )
            conn.commit()
