#!/usr/bin/env python3
"""
Check-in / roll-call command for the MeshCore Bot.

Lets users record a check-in status, view recent roll-call activity,
and look up the latest known status for a node or callsign.
"""

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from .base_command import BaseCommand
from ..models import MeshMessage
from ..utils import get_config_timezone, truncate_string


class CheckinCommand(BaseCommand):
    """Handle roll-call style check-ins for safety and community status."""

    name = "checkin"
    keywords = ["checkin", "check-in", "rollcall", "roll-call"]
    description = (
        "Record your status, list recent roll-call activity, or look up the latest "
        "check-in for a node. Useful for emergency check-ins and casual net roll calls."
    )
    category = "safety"

    short_description = "Record status and view recent roll-call check-ins"
    usage = "checkin [status text|list|last <node>|remove]"
    examples = [
        "checkin",
        "checkin safe at home",
        "checkin need supplies",
        "checkin list",
        "checkin last Jay",
        "checkin remove",
        "rollcall",
    ]
    parameters = [
        {"name": "status", "description": "Optional free-form status text to store with your check-in"},
        {"name": "list", "description": "Show the most recent unique check-ins"},
        {"name": "last <node>", "description": "Look up the latest check-in from a user or node"},
        {"name": "remove", "description": "Remove your saved check-ins from the roll-call list"},
    ]

    MAX_RESPONSE_LENGTH = 220
    def __init__(self, bot: Any):
        super().__init__(bot)
        self._load_config()
        self._init_checkin_table()

    def _load_config(self) -> None:
        """Load configuration settings for the check-in command."""
        self.enabled = self.get_config_value("Checkin_Command", "enabled", fallback=True, value_type="bool")
        self.default_status = self.get_config_value(
            "Checkin_Command", "default_status", fallback="safe", value_type="str"
        )
        self.max_list_entries = self.get_config_value(
            "Checkin_Command", "max_list_entries", fallback=6, value_type="int"
        )
        self.retention_hours = self.get_config_value(
            "Checkin_Command", "retention_hours", fallback=72, value_type="int"
        )
        self.recent_window_days = self.get_config_value(
            "Checkin_Command", "recent_window_days", fallback=3, value_type="int"
        )

    def can_execute(self, message: MeshMessage) -> bool:
        """Check if the command can execute."""
        if not self.enabled:
            return False
        return super().can_execute(message)

    def _init_checkin_table(self) -> None:
        """Create the check-in history table if needed."""
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS checkins (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        checkin_time INTEGER NOT NULL,
                        sender_id TEXT,
                        sender_pubkey TEXT,
                        display_name TEXT NOT NULL,
                        status_text TEXT NOT NULL,
                        channel TEXT,
                        is_dm BOOLEAN NOT NULL DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_checkins_time ON checkins(checkin_time DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_checkins_sender_id ON checkins(sender_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_checkins_sender_pubkey ON checkins(sender_pubkey)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_checkins_display_name ON checkins(display_name)"
                )
                conn.commit()
        except Exception as e:
            self.logger.error(f"Failed to initialize check-in table: {e}")
            raise

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the check-in command."""
        response = await self.handle(message)
        if isinstance(response, list):
            return await self.send_numbered_chunks(message, response)
        return await self.send_response(message, response)

    async def handle(self, message: MeshMessage) -> Union[str, List[str]]:
        """Handle the requested check-in action."""
        self._purge_expired_checkins(message.timestamp or int(time.time()))
        keyword, args = self._parse_message(message.content)
        args_lower = args.lower()

        if args_lower == "help":
            return self._get_help_summary()

        if args_lower.startswith("list") or (keyword in {"rollcall", "roll-call"} and not args):
            return self._handle_list(message)

        if args_lower.startswith("last "):
            query = args[5:].strip()
            if not query:
                return "Usage: checkin last <node>"
            return self._handle_lookup(query)

        if args_lower == "remove":
            return self._handle_remove(message)

        status_text = args.strip() or self.default_status
        return self._handle_checkin(message, status_text)

    def _parse_message(self, content: str) -> tuple[str, str]:
        """Return the normalized keyword and remaining argument string."""
        text = content.strip()
        if self._command_prefix and text.startswith(self._command_prefix):
            text = text[len(self._command_prefix):].strip()
        elif text.startswith("!"):
            text = text[1:].strip()

        text = self._strip_mentions(text)
        parts = text.split(maxsplit=1)
        keyword = parts[0].lower() if parts else self.name
        args = parts[1].strip() if len(parts) > 1 else ""
        return keyword, args

    def _handle_checkin(self, message: MeshMessage, status_text: str) -> str:
        """Store a new check-in event."""
        try:
            now_ts = message.timestamp or int(time.time())
            display_name = self._get_display_name(message)
            cleaned_status = truncate_string(status_text.strip(), 120)

            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO checkins
                    (checkin_time, sender_id, sender_pubkey, display_name, status_text, channel, is_dm)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now_ts,
                        message.sender_id,
                        message.sender_pubkey,
                        display_name,
                        cleaned_status,
                        message.channel,
                        1 if message.is_dm else 0,
                    ),
                )
                conn.commit()

            timestamp_label = self._format_local_timestamp(now_ts)
            response = f"Check-in saved for {display_name} at {timestamp_label}: {cleaned_status}"
            return truncate_string(response, self.MAX_RESPONSE_LENGTH)
        except Exception as e:
            self.logger.error(f"Error saving check-in: {e}")
            return "Unable to save your check-in right now."

    def _handle_list(self, message: MeshMessage) -> Union[str, List[str]]:
        """Return recent unique check-ins."""
        now_ts = int(time.time())
        window_start = now_ts - max(self.recent_window_days, 1) * 86400

        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT c.display_name, c.status_text, c.channel, c.is_dm, c.checkin_time
                    FROM checkins c
                    JOIN (
                        SELECT
                            COALESCE(NULLIF(sender_pubkey, ''), NULLIF(sender_id, ''), display_name) AS identity_key,
                            MAX(checkin_time) AS max_time
                        FROM checkins
                        WHERE checkin_time >= ?
                        GROUP BY identity_key
                    ) latest
                      ON COALESCE(NULLIF(c.sender_pubkey, ''), NULLIF(c.sender_id, ''), c.display_name) = latest.identity_key
                     AND c.checkin_time = latest.max_time
                    ORDER BY c.checkin_time DESC
                    LIMIT ?
                    """,
                    (window_start, max(self.max_list_entries, 1)),
                )
                rows = cursor.fetchall()
        except Exception as e:
            self.logger.error(f"Error listing check-ins: {e}")
            return "Unable to load recent check-ins right now."

        if not rows:
            return f"No check-ins recorded in the last {self.recent_window_days} day(s)."

        lines = ["Recent check-ins (max 72hrs):"]
        for index, row in enumerate(rows, start=1):
            entry = f"{index}. {row['display_name']} - {self._format_age(row['checkin_time'])}"
            if row["status_text"]:
                entry += f" - {truncate_string(row['status_text'], 28)}"
            lines.append(entry)

        full_response = "\n".join(lines)
        max_chunk_length = self.get_numbered_chunk_max_length(message)
        if len(full_response) <= max_chunk_length:
            return full_response

        return lines

    def _handle_lookup(self, query: str) -> str:
        """Look up the latest check-in by name, sender ID, or pubkey."""
        query_clean = query.strip()
        if not query_clean:
            return "Usage: checkin last <node>"

        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                exact = self._fetch_lookup_row(cursor, query_clean, exact=True)
                row = exact or self._fetch_lookup_row(cursor, query_clean, exact=False)
        except Exception as e:
            self.logger.error(f"Error looking up check-in '{query_clean}': {e}")
            return "Unable to look up that check-in right now."

        if not row:
            return f"No check-in found for {query_clean}."

        when = self._format_age(row["checkin_time"])
        where = "via DM" if row["is_dm"] else (f"on #{row['channel']}" if row["channel"] else "on the mesh")
        status = row["status_text"] or self.default_status
        response = f"{row['display_name']} last checked in {when} {where}: {status}"
        return truncate_string(response, self.MAX_RESPONSE_LENGTH)

    def _handle_remove(self, message: MeshMessage) -> str:
        """Remove saved check-ins for the requesting user."""
        display_name = self._get_display_name(message)
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                deleted = self._delete_for_sender(cursor, message)
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error removing check-in for {display_name}: {e}")
            return "Unable to remove your check-in right now."

        if deleted <= 0:
            return f"No saved check-in found for {display_name}."
        return f"Removed {deleted} check-in entr{'y' if deleted == 1 else 'ies'} for {display_name}."

    def _fetch_lookup_row(self, cursor: Any, query: str, exact: bool) -> Optional[Dict[str, Any]]:
        """Fetch the latest check-in row for a search query."""
        normalized = query.lower()
        if exact:
            sql = """
                SELECT display_name, status_text, channel, is_dm, checkin_time
                FROM checkins
                WHERE lower(display_name) = ?
                   OR lower(COALESCE(sender_id, '')) = ?
                   OR lower(COALESCE(sender_pubkey, '')) = ?
                ORDER BY checkin_time DESC
                LIMIT 1
            """
            params = (normalized, normalized, normalized)
        else:
            wildcard = f"%{normalized}%"
            sql = """
                SELECT display_name, status_text, channel, is_dm, checkin_time
                FROM checkins
                WHERE lower(display_name) LIKE ?
                   OR lower(COALESCE(sender_id, '')) LIKE ?
                   OR lower(COALESCE(sender_pubkey, '')) LIKE ?
                ORDER BY checkin_time DESC
                LIMIT 1
            """
            params = (wildcard, wildcard, wildcard)

        cursor.execute(sql, params)
        row = cursor.fetchone()
        return dict(row) if row else None

    def _purge_expired_checkins(self, now_ts: int) -> None:
        """Delete old check-ins outside the retention window."""
        if self.retention_hours <= 0:
            return
        cutoff = now_ts - (self.retention_hours * 3600)
        try:
            with self.bot.db_manager.connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM checkins WHERE checkin_time < ?", (cutoff,))
                conn.commit()
        except Exception as e:
            self.logger.error(f"Error purging expired check-ins: {e}")

    def _delete_for_sender(self, cursor: Any, message: MeshMessage) -> int:
        """Delete all check-ins belonging to the requesting sender."""
        sender_pubkey = (message.sender_pubkey or "").strip()
        sender_id = (message.sender_id or "").strip()
        display_name = self._get_display_name(message)

        if sender_pubkey:
            cursor.execute("DELETE FROM checkins WHERE sender_pubkey = ?", (sender_pubkey,))
            return cursor.rowcount

        if sender_id:
            cursor.execute("DELETE FROM checkins WHERE sender_id = ?", (sender_id,))
            return cursor.rowcount

        cursor.execute("DELETE FROM checkins WHERE display_name = ?", (display_name,))
        return cursor.rowcount

    def _get_display_name(self, message: MeshMessage) -> str:
        """Choose the best available display name for the sender."""
        return message.sender_id or message.sender_pubkey or "Unknown"

    def _format_local_timestamp(self, timestamp_value: int) -> str:
        """Format a UNIX timestamp in the configured timezone."""
        try:
            tz, _ = get_config_timezone(self.bot.config, self.logger)
            return datetime.fromtimestamp(timestamp_value, tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return datetime.fromtimestamp(timestamp_value).strftime("%Y-%m-%d %H:%M")

    def _format_age(self, timestamp_value: int) -> str:
        """Format an age string like '5m ago'."""
        delta = max(0, int(time.time()) - int(timestamp_value))
        if delta < 60:
            return "just now"
        if delta < 3600:
            return f"{delta // 60}m ago"
        if delta < 86400:
            return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"

    def _get_help_summary(self) -> str:
        """Return a compact help summary."""
        return (
            "Use 'checkin' to mark yourself safe, 'checkin <status>' to add a note, "
            "'checkin list' for recent roll-call, 'checkin last <node>' to look someone up, "
            "or 'checkin remove' to clear your entry."
        )
