"""Tests for the configured public-channel submenu."""

import pytest

from modules.commands.channels_command import ChannelsCommand
from tests.conftest import command_mock_bot, mock_message


def configure_channels(bot):
    bot.config.add_section('Channels_Command')
    bot.config.set('Channels_Command', 'enabled', 'true')
    bot.config.add_section('Channels_List')
    bot.config.set('Channels_List', 'nelson', 'Nelson community chat.')
    bot.config.set('Channels_List', 'quakes', 'GeoNet earthquake alerts.')


@pytest.mark.asyncio
async def test_channels_opens_lettered_menu(command_mock_bot):
    configure_channels(command_mock_bot)
    command = ChannelsCommand(command_mock_bot)
    message = mock_message(content='channels', sender_id='TestUser', is_dm=True)

    assert await command.execute(message) is True
    response = command_mock_bot.command_manager.send_response.call_args[0][1]
    assert 'A #nelson' in response
    assert 'B #quakes' in response
    assert 'Reply A-E for details' in response


@pytest.mark.asyncio
async def test_letter_returns_description_only_for_active_user(command_mock_bot):
    configure_channels(command_mock_bot)
    command = ChannelsCommand(command_mock_bot)
    await command.execute(mock_message(content='channels', sender_id='TestUser', is_dm=True))
    command_mock_bot.command_manager.send_response.reset_mock()

    selection = mock_message(content='B', sender_id='TestUser', is_dm=True)
    assert command.matches_keyword(selection) is True
    assert await command.execute(selection) is True
    response = command_mock_bot.command_manager.send_response.call_args[0][1]
    assert response.startswith('#quakes: GeoNet earthquake alerts.')
    assert command.matches_keyword(mock_message(content='B', sender_id='Other', is_dm=True)) is False
