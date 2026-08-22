#!/usr/bin/env python3
"""Build repeater helper menu for the MeshCore Bot."""

import time
from typing import Dict

from .base_command import BaseCommand
from ..models import MeshMessage


class BuildCommand(BaseCommand):
    """Provides a small submenu of helpful repeater build commands."""

    name = "build"
    keywords = ["build", "1", "2", "3", "4", "5", "exit", "cancel"]
    description = "Shows helpful commands for building a repeater node."
    category = "basic"

    short_description = "Helpful repeater build commands"
    usage = "build"
    examples = ["build", "1", "2", "3", "4", "5"]

    MENU_PROMPT = (
        "To build a new repeater you'll need these four commands. "
        "Send 1 for 2byte Hop, 2 for powersavings, 3 for Repeat, "
        "4 for clocksync, or 5 to exit."
    )
    MENU_RESPONSES = {
        "1": 'To enable 2 btye hop enter via CLI "set path.hash.mode 1"',
        "2": 'To enable power savings (ESP32 boards) enter via CLI "powersaving on"',
        "3": 'To disable repeater mode enter via VLI "set repeat off" only if needed during setup.',
        "4": 'To manually update the clock, Enter via CLI "clkreboot", then "clock sync"',
    }
    EXIT_RESPONSES = {"5", "exit", "cancel"}
    EXIT_MESSAGE = "Exited build menu."

    def __init__(self, bot):
        super().__init__(bot)
        self.build_enabled = self.get_config_value(
            "Build_Command", "enabled", fallback=True, value_type="bool"
        )
        self.menu_timeout_seconds = self.get_config_value(
            "Build_Command", "menu_timeout_seconds", fallback=600, value_type="int"
        )
        self._active_menus: Dict[str, float] = {}

    def can_execute(self, message: MeshMessage) -> bool:
        if not self.build_enabled:
            return False
        return super().can_execute(message)

    def _menu_key(self, message: MeshMessage) -> str:
        return message.sender_pubkey or message.sender_id or ""

    def _normalize_content(self, message: MeshMessage) -> str:
        content = (message.content or "").strip()

        if self._command_prefix:
            if not content.startswith(self._command_prefix):
                return ""
            content = content[len(self._command_prefix):].strip()
        elif content.startswith("!"):
            content = content[1:].strip()

        if not self._check_mentions_ok(content):
            return ""

        content = self._strip_mentions(content)
        return " ".join(content.lower().split())

    def _prune_expired_menus(self) -> None:
        if not self._active_menus:
            return
        cutoff = time.time() - self.menu_timeout_seconds
        self._active_menus = {
            user_key: started_at
            for user_key, started_at in self._active_menus.items()
            if started_at >= cutoff
        }

    def _has_active_menu(self, message: MeshMessage) -> bool:
        self._prune_expired_menus()
        menu_key = self._menu_key(message)
        return bool(menu_key) and menu_key in self._active_menus

    def _activate_menu(self, message: MeshMessage) -> None:
        menu_key = self._menu_key(message)
        if menu_key:
            self._active_menus[menu_key] = time.time()

    def _clear_menu(self, message: MeshMessage) -> None:
        menu_key = self._menu_key(message)
        if menu_key:
            self._active_menus.pop(menu_key, None)

    def matches_keyword(self, message: MeshMessage) -> bool:
        if not self.build_enabled:
            return False

        content = self._normalize_content(message)
        if not content:
            return False

        if content == "build":
            return True

        return self._has_active_menu(message) and (
            content in self.MENU_RESPONSES or content in self.EXIT_RESPONSES
        )

    async def execute(self, message: MeshMessage) -> bool:
        content = self._normalize_content(message)
        if not content:
            return False

        if content == "build":
            self._activate_menu(message)
            return await self.send_response(message, self.MENU_PROMPT, skip_user_rate_limit=True)

        if content in self.MENU_RESPONSES and self._has_active_menu(message):
            self._activate_menu(message)
            return await self.send_response(
                message, self.MENU_RESPONSES[content], skip_user_rate_limit=True
            )

        if content in self.EXIT_RESPONSES and self._has_active_menu(message):
            self._clear_menu(message)
            return await self.send_response(message, self.EXIT_MESSAGE, skip_user_rate_limit=True)

        return False
