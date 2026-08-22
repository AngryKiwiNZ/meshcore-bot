import configparser
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from meshcore import EventType
from modules.core import MeshCoreBot


def _bot(raw_mode: str, current_modes: list[int]):
    bot = MeshCoreBot.__new__(MeshCoreBot)
    bot.config = configparser.ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "path_hash_mode", raw_mode)
    bot.logger = logging.getLogger("test_core_path_hash_mode")
    commands = SimpleNamespace(
        get_path_hash_mode=AsyncMock(side_effect=current_modes),
        set_path_hash_mode=AsyncMock(
            return_value=SimpleNamespace(type=EventType.OK)
        ),
    )
    bot.meshcore = SimpleNamespace(is_connected=True, commands=commands)
    return bot, commands


@pytest.mark.asyncio
async def test_matching_path_hash_mode_is_left_unchanged():
    bot, commands = _bot("1", [1])

    assert await bot.ensure_device_path_hash_mode() is True
    commands.set_path_hash_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_path_hash_mode_is_updated_and_verified():
    bot, commands = _bot("1", [0, 1])

    assert await bot.ensure_device_path_hash_mode() is True
    commands.set_path_hash_mode.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_blank_path_hash_mode_does_not_query_device():
    bot, commands = _bot("", [0])

    assert await bot.ensure_device_path_hash_mode() is True
    commands.get_path_hash_mode.assert_not_awaited()
