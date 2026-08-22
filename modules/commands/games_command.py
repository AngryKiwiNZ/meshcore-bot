#!/usr/bin/env python3
"""Command metadata and fallback entry point for persistent DM games."""

from .base_command import BaseCommand
from ..models import MeshMessage


class GamesCommand(BaseCommand):
    name = "games"
    keywords = ["games"]
    description = "Play persistent DM games: Lemonade Stand, Blackjack, and Mastermind"
    category = "games"
    requires_dm = True
    short_description = "List and play the bot's DM games"
    usage = "games"
    examples = ["games", "game lemonade", "game blackjack", "game mastermind"]

    def __init__(self, bot):
        super().__init__(bot)
        self.games_enabled = self.get_config_value(
            "Games_Command", "enabled", fallback=True, value_type="bool"
        )

    def can_execute(self, message: MeshMessage) -> bool:
        return self.games_enabled and super().can_execute(message)

    def get_help_text(self) -> str:
        return (
            "DM the bot with 'games' to see Lemonade Stand, Blackjack, and "
            "Mastermind. Use 'game stop' to leave an active game."
        )

    async def execute(self, message: MeshMessage) -> bool:
        service = getattr(self.bot, "game_service", None)
        if not service:
            return await self.send_response(message, "Games are currently unavailable.")
        return await service.handle_message(message)
