#!/usr/bin/env python3
"""
Telemetry Leaderboard command for the MeshCore Bot
Displays network-wide telemetry records and extreme metrics
"""

from typing import Optional, Any
from .base_command import BaseCommand
from ..models import MeshMessage


class TelemetryCommand(BaseCommand):
    """Handles the telemetry command to display leaderboard and records.
    
    Shows extreme metrics from across the mesh network including battery levels,
    temperatures, speeds, altitudes, and signal quality records.
    """
    
    # Plugin metadata
    name = "telemetry"
    keywords = ['telemetry', 'leaderboard', 'records']
    description = "Display network telemetry records and leaderboard. Use 'telemetry reset' (admin only) to reset records."
    category = "analytics"
    
    # Documentation
    short_description = "Show network telemetry leaderboard"
    usage = "telemetry [reset]"
    examples = ["telemetry", "telemetry reset"]
    parameters = [
        {"name": "action", "description": "Optional: 'reset' to clear all records (admin only)"}
    ]
    
    def __init__(self, bot: Any):
        """Initialize the telemetry command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self._load_config()
    
    def _load_config(self) -> None:
        """Load configuration settings for telemetry command."""
        self.telemetry_enabled = self.get_config_value(
            'Telemetry_Command',
            'enabled',
            fallback=True,
            value_type='bool'
        )
        self.log_records = self.get_config_value(
            'Telemetry_Command',
            'log_records',
            fallback=True,
            value_type='bool'
        )
    
    def _get_leaderboard(self):
        """Get the telemetry leaderboard instance from the bot."""
        if hasattr(self.bot, 'telemetry_leaderboard'):
            return self.bot.telemetry_leaderboard
        return None
    
    async def handle(self, message: Optional[MeshMessage] = None, args: str = "") -> Optional[str]:
        """Handle the telemetry command.
        
        Args:
            message: The mesh message (optional)
            args: Command arguments
            
        Returns:
            str: Response message with telemetry leaderboard or status
        """
        if not self.telemetry_enabled:
            return "❌ Telemetry command is disabled"
        
        leaderboard = self._get_leaderboard()
        args = args.strip().lower() if args else ""
        
        # Handle reset command (admin only)
        if args == "reset":
            if not self._is_admin(message):
                return "❌ You don't have permission to reset the leaderboard"
            
            leaderboard.reset()
            return "✅ Telemetry leaderboard has been reset"
        
        # Return formatted leaderboard
        return leaderboard.format_leaderboard()
    
    def _is_admin(self, message: Optional[MeshMessage]) -> bool:
        """Check if the sender is an admin.
        
        Args:
            message: The mesh message
            
        Returns:
            bool: True if admin, False otherwise
        """
        if message is None:
            return False
        
        # Check admin list from configuration
        admin_nodes = self.get_config_value('Security', 'admin_nodes', fallback="", value_type='str')
        admin_list = [node.strip() for node in admin_nodes.split(',') if node.strip()]
        
        return str(message.sender_id) in admin_list or message.sender_id in admin_list


# Make sure the command is discoverable
def get_command_class():
    """Return the command class for plugin loading."""
    return TelemetryCommand
