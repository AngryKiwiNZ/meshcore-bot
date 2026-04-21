#!/usr/bin/env python3
"""
Telemetry Leaderboard command for the MeshCore Bot.

Displays network-wide telemetry records and extreme metrics.
"""

from typing import Optional, Any

from .base_command import BaseCommand
from ..models import MeshMessage


class TelemetryCommand(BaseCommand):
    """Handle the telemetry command to display leaderboard records."""

    name = "telemetry"
    keywords = ["telemetry", "leaderboard", "records"]
    description = (
        "Display network telemetry records and leaderboard. "
        "Use 'telemetry reset' (admin only) to reset records."
    )
    category = "analytics"

    short_description = "Show network telemetry leaderboard"
    usage = "telemetry [reset]"
    examples = ["telemetry", "telemetry reset"]
    parameters = [
        {"name": "action", "description": "Optional: 'reset' to clear all records (admin only)"}
    ]

    def __init__(self, bot: Any):
        """Initialize the telemetry command."""
        super().__init__(bot)
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration settings for telemetry command."""
        self.telemetry_enabled = self.get_config_value(
            "Telemetry_Command",
            "enabled",
            fallback=True,
            value_type="bool",
        )
        self.log_records = self.get_config_value(
            "Telemetry_Command",
            "log_records",
            fallback=True,
            value_type="bool",
        )

    def _get_leaderboard(self):
        """Get the telemetry leaderboard instance from the bot."""
        if hasattr(self.bot, "telemetry_leaderboard"):
            return self.bot.telemetry_leaderboard
        return None

    async def execute(self, message: MeshMessage) -> bool:
        """Execute the telemetry command and send the resulting response."""
        content = message.content.strip()
        if self._command_prefix:
            if content.startswith(self._command_prefix):
                content = content[len(self._command_prefix):].strip()
        elif content.startswith("!"):
            content = content[1:].strip()

        content = self._strip_mentions(content)

        parts = content.split(maxsplit=1)
        args = parts[1] if len(parts) > 1 else ""

        response = await self.handle(message, args)
        if not response:
            response = "❌ Telemetry leaderboard is unavailable"
        return await self.send_response(message, response)

    async def handle(self, message: Optional[MeshMessage] = None, args: str = "") -> Optional[str]:
        """Handle the telemetry command."""
        if not self.telemetry_enabled:
            return "❌ Telemetry command is disabled"

        leaderboard = self._get_leaderboard()
        args = args.strip().lower() if args else ""

        if leaderboard is None:
            return "❌ Telemetry leaderboard is unavailable"

        if args == "reset":
            if not self._is_admin(message):
                return "❌ You don't have permission to reset the leaderboard"

            leaderboard.reset()
            return "✅ Telemetry leaderboard has been reset"

        return leaderboard.format_leaderboard()

    def _is_admin(self, message: Optional[MeshMessage]) -> bool:
        """Check if the sender is an admin."""
        if message is None:
            return False

        admin_nodes = self.get_config_value("Security", "admin_nodes", fallback="", value_type="str")
        admin_list = [node.strip() for node in admin_nodes.split(",") if node.strip()]

        return str(message.sender_id) in admin_list or message.sender_id in admin_list


def get_command_class():
    """Return the command class for plugin loading."""
    return TelemetryCommand
