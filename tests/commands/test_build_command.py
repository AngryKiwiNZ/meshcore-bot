"""Tests for modules.commands.build_command."""

import pytest

from modules.commands.build_command import BuildCommand
from tests.conftest import command_mock_bot, mock_message


class TestBuildCommand:
    """Tests for the repeater build helper menu."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)
        msg = mock_message(content="build", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "false")
        cmd = BuildCommand(command_mock_bot)
        msg = mock_message(content="build", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_returns_menu_prompt(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)

        result = await cmd.execute(mock_message(content="build", is_dm=True))

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        response = call_args[0][1]
        assert response == BuildCommand.MENU_PROMPT
        assert call_args[1]["skip_user_rate_limit"] is True

    @pytest.mark.asyncio
    async def test_option_requires_active_menu(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)

        result = await cmd.execute(mock_message(content="1", is_dm=True))

        assert result is False
        command_mock_bot.command_manager.send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_option_one_returns_expected_command(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)
        msg = mock_message(content="build", is_dm=True, sender_id="Builder")

        await cmd.execute(msg)
        command_mock_bot.command_manager.send_response.reset_mock()

        result = await cmd.execute(mock_message(content="1", is_dm=True, sender_id="Builder"))

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        response = call_args[0][1]
        assert response == BuildCommand.MENU_RESPONSES["1"]
        assert call_args[1]["skip_user_rate_limit"] is True

    @pytest.mark.asyncio
    async def test_option_four_returns_clock_sync_commands(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)

        await cmd.execute(mock_message(content="build", is_dm=True, sender_id="Builder"))
        command_mock_bot.command_manager.send_response.reset_mock()

        result = await cmd.execute(mock_message(content="4", is_dm=True, sender_id="Builder"))

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args[0][1] == (
            'To manually update the clock, Enter via CLI "clkreboot", then "clock sync"'
        )
        assert call_args[1]["skip_user_rate_limit"] is True

    @pytest.mark.asyncio
    async def test_exit_clears_active_menu(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)

        await cmd.execute(mock_message(content="build", is_dm=True, sender_id="Builder"))
        command_mock_bot.command_manager.send_response.reset_mock()

        result = await cmd.execute(mock_message(content="5", is_dm=True, sender_id="Builder"))

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        response = call_args[0][1]
        assert response == BuildCommand.EXIT_MESSAGE
        assert call_args[1]["skip_user_rate_limit"] is True
        assert cmd.matches_keyword(mock_message(content="1", is_dm=True, sender_id="Builder")) is False

    def test_numeric_match_only_when_menu_is_active(self, command_mock_bot):
        command_mock_bot.config.add_section("Build_Command")
        command_mock_bot.config.set("Build_Command", "enabled", "true")
        cmd = BuildCommand(command_mock_bot)

        assert cmd.matches_keyword(mock_message(content="2", is_dm=True, sender_id="Builder")) is False

        cmd._active_menus["Builder"] = 9999999999
        assert cmd.matches_keyword(mock_message(content="2", is_dm=True, sender_id="Builder")) is True
