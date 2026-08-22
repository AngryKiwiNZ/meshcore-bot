import configparser
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from modules.game_service import GameService
from modules.message_handler import MessageHandler
from modules.models import MeshMessage


def make_service(tmp_path):
    config = configparser.ConfigParser()
    config["Games"] = {
        "enabled": "true",
        "session_expiry_days": "7",
        "max_reply_chunks": "2",
        "chunk_bytes": "135",
    }
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.db_manager.db_path = str(tmp_path / "bot.db")
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    bot.command_manager.is_command_trigger = Mock(return_value=False)
    return GameService(bot)


def dm(content="games", key=None):
    return MeshMessage(
        content=content,
        sender_id="Alice",
        sender_pubkey=key or ("ab" * 32),
        is_dm=True,
    )


@pytest.mark.asyncio
async def test_games_menu_and_lemonade_session_are_persistent(tmp_path):
    service = make_service(tmp_path)
    assert await service.handle_message(dm("games")) is True
    sent = service.bot.command_manager.send_response_chunked.await_args.args[1]
    assert "Lemonade Stand" in " ".join(sent)

    assert await service.handle_message(dm("lemonade")) is True
    session = service._get_session("ab" * 32)
    assert session["game_key"] == "lemonade"
    assert session["state"]["phase"] == "cups"

    reloaded = GameService(service.bot)
    assert reloaded._get_session("ab" * 32)["state"]["week"] == 1


@pytest.mark.asyncio
async def test_active_game_yields_known_normal_command(tmp_path):
    service = make_service(tmp_path)
    await service.handle_message(dm("mastermind"))
    service.bot.command_manager.is_command_trigger.return_value = True

    assert await service.handle_message(dm("wx Nelson")) is False
    assert service._get_session("ab" * 32)["game_key"] == "mastermind"


@pytest.mark.asyncio
async def test_mastermind_win_records_score_and_closes_session(tmp_path):
    service = make_service(tmp_path)
    await service.handle_message(dm("mastermind"))
    await service.handle_message(dm("n"))
    session = service._get_session("ab" * 32)
    session["state"]["secret"] = "RYGB"
    service._save_session(
        "ab" * 32, "Alice", "mastermind", session["state"]
    )

    assert await service.handle_message(dm("RYGB")) is True
    assert service._get_session("ab" * 32) is None
    with service._connect() as conn:
        score = conn.execute(
            "SELECT score FROM game_scores WHERE game_key='mastermind'"
        ).fetchone()
    assert score["score"] == 1


@pytest.mark.asyncio
async def test_blackjack_accepts_bet_and_survives_as_session(tmp_path):
    service = make_service(tmp_path)
    await service.handle_message(dm("blackjack"))
    await service.handle_message(dm("10"))
    session = service._get_session("ab" * 32)
    assert session["game_key"] == "blackjack"
    assert session["state"]["phase"] in {"play", "bet"}


@pytest.mark.asyncio
async def test_disabled_game_is_not_started(tmp_path):
    service = make_service(tmp_path)
    with service._connect() as conn:
        conn.execute(
            "UPDATE game_settings SET enabled=0 WHERE game_key='lemonade'"
        )
    assert await service.handle_message(dm("lemonade")) is True
    sent = service.bot.command_manager.send_response_chunked.await_args.args[1]
    assert "disabled" in " ".join(sent).lower()


def test_reply_chunking_is_limited_and_utf8_safe(tmp_path):
    service = make_service(tmp_path)
    chunks = service._chunks(["🍋 " + ("warm friendly lemonade stand " * 30)])
    assert 1 <= len(chunks) <= 2
    assert all(len(chunk.encode("utf-8")) <= 135 for chunk in chunks)


def test_old_sessions_expire(tmp_path):
    service = make_service(tmp_path)
    state = {"phase": "difficulty", "turn": 0}
    service._save_session("ab" * 32, "Alice", "mastermind", state)
    with service._connect() as conn:
        conn.execute(
            "UPDATE game_sessions SET updated_at=?",
            (time.time() - (8 * 86400),),
        )
    assert service._purge_expired() == 1
    assert service._get_session("ab" * 32) is None


@pytest.mark.asyncio
async def test_message_handler_routes_dm_game_before_llm():
    handler = object.__new__(MessageHandler)
    handler.multitest_listener = None
    handler.logger = Mock()
    handler.should_process_message = Mock(return_value=True)
    bot = Mock()
    bot.command_manager.commands = {}
    bot.game_service.handle_message = AsyncMock(return_value=True)
    bot.llm_service.handle_message = AsyncMock(return_value=True)
    handler.bot = bot
    message = dm("10")

    await handler.process_message(message)

    bot.game_service.handle_message.assert_awaited_once_with(message)
    bot.llm_service.handle_message.assert_not_awaited()
