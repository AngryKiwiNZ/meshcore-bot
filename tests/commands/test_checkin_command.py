"""Tests for modules.commands.checkin_command."""

import configparser
import time
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.checkin_command import CheckinCommand
from modules.db_manager import DBManager
from tests.conftest import mock_message


@pytest.fixture
def checkin_bot(mock_logger, tmp_path):
    """Create a bot with a real DB manager for check-in command tests."""
    config = configparser.ConfigParser()
    config.add_section("Connection")
    config.set("Connection", "connection_type", "serial")
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.set("Bot", "timezone", "UTC")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general,emergency")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.set("Keywords", "ping", "Pong!")
    config.add_section("Checkin_Command")
    config.set("Checkin_Command", "enabled", "true")
    config.set("Checkin_Command", "default_status", "safe")
    config.set("Checkin_Command", "max_list_entries", "6")
    config.set("Checkin_Command", "retention_hours", "72")
    config.set("Checkin_Command", "recent_window_days", "3")

    bot = MagicMock()
    bot.logger = mock_logger
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general", "emergency"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    bot.db_manager = DBManager(bot, str(tmp_path / "checkin.db"))
    return bot


class TestCheckinCommand:
    """Tests for CheckinCommand."""

    def test_can_execute_when_enabled(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        msg = mock_message(content="checkin", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, checkin_bot):
        checkin_bot.config.set("Checkin_Command", "enabled", "false")
        cmd = CheckinCommand(checkin_bot)
        msg = mock_message(content="checkin", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_records_default_status(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        msg = mock_message(
            content="checkin",
            is_dm=False,
            channel="emergency",
            sender_id="Alice",
            timestamp=1_700_000_000,
        )

        result = await cmd.execute(msg)

        assert result is True
        response = checkin_bot.command_manager.send_response.call_args[0][1]
        assert "Check-in saved for Alice" in response
        assert "safe" in response

        rows = checkin_bot.db_manager.execute_query(
            "SELECT display_name, status_text, channel FROM checkins"
        )
        assert len(rows) == 1
        assert rows[0]["display_name"] == "Alice"
        assert rows[0]["status_text"] == "safe"
        assert rows[0]["channel"] == "emergency"

    @pytest.mark.asyncio
    async def test_rollcall_lists_recent_checkins(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        now = int(time.time())
        await cmd.execute(
            mock_message(
                content="checkin safe at home",
                sender_id="Alice",
                channel="emergency",
                timestamp=now - 120,
            )
        )
        await cmd.execute(
            mock_message(
                content="checkin all good",
                sender_id="Bob",
                channel="general",
                timestamp=now - 30,
            )
        )

        checkin_bot.command_manager.send_response.reset_mock()
        result = await cmd.execute(mock_message(content="rollcall", sender_id="NetControl", is_dm=True))

        assert result is True
        response = checkin_bot.command_manager.send_response.call_args[0][1]
        assert response.startswith("Recent check-ins (max 72hrs):")
        lines = response.splitlines()
        assert len(lines) >= 3
        assert lines[1].startswith("1. Bob - ")
        assert "all good" in lines[1]
        assert lines[2].startswith("2. Alice - ")
        assert "safe at home" in lines[2]

    @pytest.mark.asyncio
    async def test_rollcall_chunks_long_lists_with_numbering(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        now = int(time.time())

        for idx in range(1, 7):
            await cmd.execute(
                mock_message(
                    content=f"checkin status entry number {idx} with extra text",
                    sender_id=f"Operator{idx}",
                    channel="emergency",
                    timestamp=now - idx,
                    is_dm=True,
                )
            )

        checkin_bot.command_manager.send_response.reset_mock()
        checkin_bot.command_manager.send_response_chunked.reset_mock()

        result = await cmd.execute(mock_message(content="rollcall", sender_id="NetControl", is_dm=True))

        assert result is True
        checkin_bot.command_manager.send_response.assert_not_called()
        checkin_bot.command_manager.send_response_chunked.assert_awaited_once()

        chunks = checkin_bot.command_manager.send_response_chunked.await_args.args[1]
        assert len(chunks) >= 2
        assert chunks[0].startswith(f"1/{len(chunks)} ")
        assert chunks[-1].startswith(f"{len(chunks)}/{len(chunks)} ")
        assert all(len(chunk) <= 125 for chunk in chunks)

    def test_build_numbered_chunks_splits_overlong_single_line(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)

        chunks = cmd.build_numbered_chunks(
            [
                "Nelson, NZ: Tue: Sunny and bright with light winds and scattered coastal cloud "
                "before clearing later in the afternoon and evening"
            ],
            125,
        )

        assert len(chunks) >= 2
        assert chunks[0].startswith(f"1/{len(chunks)} ")
        assert chunks[-1].startswith(f"{len(chunks)}/{len(chunks)} ")
        assert all(len(chunk) <= 125 for chunk in chunks)

    @pytest.mark.asyncio
    async def test_lookup_returns_latest_status(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        now = int(time.time())
        await cmd.execute(
            mock_message(
                content="checkin safe",
                sender_id="Jay",
                channel="emergency",
                timestamp=now - 3600,
            )
        )
        await cmd.execute(
            mock_message(
                content="checkin need batteries",
                sender_id="Jay",
                channel="emergency",
                timestamp=now - 60,
            )
        )

        checkin_bot.command_manager.send_response.reset_mock()
        result = await cmd.execute(mock_message(content="checkin last Jay", sender_id="Ops", is_dm=True))

        assert result is True
        response = checkin_bot.command_manager.send_response.call_args[0][1]
        assert "Jay last checked in" in response
        assert "need batteries" in response

    @pytest.mark.asyncio
    async def test_remove_deletes_users_checkins(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        now = int(time.time())
        await cmd.execute(
            mock_message(
                content="checkin safe",
                sender_id="Alice",
                channel="emergency",
                timestamp=now - 60,
            )
        )

        checkin_bot.command_manager.send_response.reset_mock()
        result = await cmd.execute(
            mock_message(content="checkin remove", sender_id="Alice", is_dm=True, timestamp=now)
        )

        assert result is True
        response = checkin_bot.command_manager.send_response.call_args[0][1]
        assert "Removed 1 check-in entry for Alice." == response
        rows = checkin_bot.db_manager.execute_query("SELECT * FROM checkins")
        assert rows == []

    @pytest.mark.asyncio
    async def test_expired_entries_are_removed_after_72_hours(self, checkin_bot):
        cmd = CheckinCommand(checkin_bot)
        now = int(time.time())
        old_timestamp = now - (73 * 3600)
        await cmd.execute(
            mock_message(
                content="checkin old status",
                sender_id="Alice",
                channel="emergency",
                timestamp=old_timestamp,
            )
        )

        checkin_bot.command_manager.send_response.reset_mock()
        result = await cmd.execute(
            mock_message(content="checkin list", sender_id="NetControl", is_dm=True, timestamp=now)
        )

        assert result is True
        response = checkin_bot.command_manager.send_response.call_args[0][1]
        assert response == "No check-ins recorded in the last 3 day(s)."
        rows = checkin_bot.db_manager.execute_query("SELECT * FROM checkins")
        assert rows == []
