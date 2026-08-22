"""CommandManager integration tests for the build command."""

from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from modules.command_manager import CommandManager
from modules.commands.build_command import BuildCommand
from tests.conftest import mock_message


def test_build_keyword_match_requires_active_menu():
    bot = Mock()
    bot.logger = Mock()
    bot.bot_root = Path("/tmp")
    bot._local_root = None
    bot.config = ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general,test")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Keywords")
    bot.config.add_section("Build_Command")
    bot.config.set("Build_Command", "enabled", "true")
    bot.translator = Mock()
    bot.translator.translate = Mock(
        side_effect=lambda key, **kw: f"{key}: {' '.join(str(v) for v in kw.values())}"
    )
    bot.meshcore = None
    bot.rate_limiter = Mock()
    bot.rate_limiter.can_send = Mock(return_value=True)
    bot.bot_tx_rate_limiter = Mock()
    bot.bot_tx_rate_limiter.wait_for_tx = Mock()
    bot.tx_delay_ms = 0

    with patch("modules.command_manager.PluginLoader") as mock_loader_class:
        mock_loader = mock_loader_class.return_value
        build_command = BuildCommand(bot)
        mock_loader.load_all_plugins.return_value = {"build": build_command}
        manager = CommandManager(bot)

    build_matches = manager.check_keywords(mock_message(content="build", is_dm=True))
    option_matches = manager.check_keywords(mock_message(content="1", is_dm=True))

    assert build_matches == [("build", None)]
    assert option_matches == []


def test_llm_trigger_check_preserves_sender_for_active_build_menu():
    bot = Mock()
    bot.logger = Mock()
    bot.bot_root = Path("/tmp")
    bot._local_root = None
    bot.config = ConfigParser()
    bot.config.add_section("Bot")
    bot.config.set("Bot", "bot_name", "TestBot")
    bot.config.add_section("Channels")
    bot.config.set("Channels", "monitor_channels", "general,test")
    bot.config.set("Channels", "respond_to_dms", "true")
    bot.config.add_section("Keywords")
    bot.config.add_section("Build_Command")
    bot.config.set("Build_Command", "enabled", "true")
    bot.translator = Mock()
    bot.meshcore = None
    bot.rate_limiter = Mock()
    bot.bot_tx_rate_limiter = Mock()
    bot.tx_delay_ms = 0

    with patch("modules.command_manager.PluginLoader") as mock_loader_class:
        build_command = BuildCommand(bot)
        mock_loader_class.return_value.load_all_plugins.return_value = {
            "build": build_command
        }
        manager = CommandManager(bot)

    sender = mock_message(
        content="build", is_dm=True, sender_id="Builder", sender_pubkey="ab" * 32
    )
    build_command._activate_menu(sender)

    option = mock_message(
        content="4", is_dm=True, sender_id="Builder", sender_pubkey="ab" * 32
    )
    other_sender = mock_message(
        content="4", is_dm=True, sender_id="SomeoneElse", sender_pubkey="cd" * 32
    )

    assert manager.is_command_trigger("4", message=option) is True
    assert manager.is_command_trigger("4", message=other_sender) is False
