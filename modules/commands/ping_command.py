#!/usr/bin/env python3
"""Ping command for the MeshCore Bot."""

import re
from typing import Optional
from .base_command import BaseCommand
from ..models import MeshMessage


class PingCommand(BaseCommand):
    """Handles the ping command.
    
    A simple diagnostic command that responds with 'Pong!' or a custom configured response
    to verify bot connectivity and responsiveness.
    """
    
    # Plugin metadata
    name = "ping"
    keywords = ['ping']
    description = "Responds to 'ping' with 'Pong!'"
    category = "basic"
    
    # Documentation
    short_description = "Get a quick 'pong'response from the bot"
    usage = "ping"
    examples = ["ping"]
    
    def __init__(self, bot):
        """Initialize the ping command.
        
        Args:
            bot: The bot instance.
        """
        super().__init__(bot)
        self.ping_enabled = self.get_config_value('Ping_Command', 'enabled', fallback=True, value_type='bool')
    
    def can_execute(self, message: MeshMessage) -> bool:
        """Check if this command can be executed with the given message.
        
        Args:
            message: The message triggering the command.
            
        Returns:
            bool: True if command is enabled and checks pass, False otherwise.
        """
        if not self.ping_enabled:
            return False
        return super().can_execute(message)
    
    def get_help_text(self) -> str:
        """Get help text for the ping command.
        
        Returns:
            str: The help text for this command.
        """
        return self.translate('commands.ping.description')
    
    def get_response_format(self) -> Optional[str]:
        """Force ping through command execution instead of plain keyword replies."""
        return None

    def _get_configured_response_text(self) -> Optional[str]:
        """Get the optional ping response text from config for use inside execute()."""
        if self.bot.config.has_section('Keywords'):
            format_str = self.bot.config.get('Keywords', 'ping', fallback=None)
            return self._strip_quotes_from_config(format_str) if format_str else None
        return None

    def _get_hops_label(self, message: MeshMessage) -> Optional[str]:
        """Return a compact hop-count label when routing data is available."""
        routing_info = getattr(message, "routing_info", None) or {}
        path_length = routing_info.get("path_length")
        if path_length is None:
            path_length = getattr(message, "hops", None)

        if path_length is None and getattr(message, "path", None):
            path_match = re.search(r"\((\d+)\s+hops?\)", message.path, flags=re.IGNORECASE)
            if path_match:
                path_length = int(path_match.group(1))
            elif "direct" in message.path.lower():
                path_length = 0

        if path_length is None:
            return None
        if int(path_length) <= 0:
            return "Direct"
        hop_count = int(path_length)
        return f"{hop_count} hop" if hop_count == 1 else f"{hop_count} hops"
    
    async def execute(self, message: MeshMessage) -> bool:
        """Execute the ping command.
        
        Args:
            message: The message that triggered the command.
            
        Returns:
            bool: True if the response was sent successfully, False otherwise.
        """
        response_text = self._get_configured_response_text() or "Pong!"
        response = f"🏓 {response_text}"

        hops_label = self._get_hops_label(message)
        if hops_label:
            response = f"{response} | {hops_label}"

        return await self.send_response(message, response)
