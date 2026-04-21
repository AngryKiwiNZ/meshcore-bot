"""Tests for MetService RSS mode in alert command."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from modules.commands.alert_command import AlertCommand


def _make_bot(provider="metservice"):
    bot = MagicMock()
    bot.logger = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.logger.debug = Mock()
    bot.db_manager = Mock()

    cfg = configparser.ConfigParser()
    cfg.add_section("Alert_Command")
    cfg.set("Alert_Command", "enabled", "true")
    cfg.set("Alert_Command", "provider", provider)
    bot.config = cfg

    bot.command_manager = Mock()
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)

    return bot


def _make_message(content="alert"):
    msg = MagicMock()
    msg.content = content
    msg.sender_id = "user1"
    msg.channel_index = 0
    msg.is_dm = True
    return msg


@pytest.mark.asyncio
async def test_metservice_alert_no_nelson_match_returns_expected_text():
    bot = _make_bot()
    cmd = AlertCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)

    rss = """<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0'><channel>
      <item><title>A</title><description>Heavy rain warning for Wellington.</description></item>
      <item><title>B</title><description>Wind watch for Canterbury.</description></item>
    </channel></rss>"""

    mock_resp = Mock(ok=True, status_code=200, text=rss)
    msg = _make_message("alert")
    with patch("modules.commands.alert_command.requests.get", return_value=mock_resp):
        await cmd.execute(msg)

    cmd.send_response.assert_awaited_once_with(
        msg,
        "There are no alerts for the Nelson region at present out of 2 current alerts",
    )


@pytest.mark.asyncio
async def test_metservice_alert_returns_only_nelson_descriptions():
    bot = _make_bot()
    cmd = AlertCommand(bot)
    cmd.send_response = AsyncMock(return_value=True)

    rss = """<?xml version='1.0' encoding='UTF-8'?>
    <rss version='2.0'><channel>
      <item><title>A</title><description>Heavy rain warning for Nelson and Tasman.</description></item>
      <item><title>B</title><description>Wind watch for Canterbury.</description></item>
      <item><title>C</title><description>Marine warning near Nelson coast.</description></item>
    </channel></rss>"""

    mock_resp = Mock(ok=True, status_code=200, text=rss)
    msg = _make_message("alert")
    with patch("modules.commands.alert_command.requests.get", return_value=mock_resp):
        await cmd.execute(msg)

    sent = cmd.send_response.await_args.args[1]
    assert "Nelson alerts (2/3)" in sent
    assert "Heavy rain warning for Nelson and Tasman." in sent
    assert "Marine warning near Nelson coast." in sent
    assert "Canterbury" not in sent
