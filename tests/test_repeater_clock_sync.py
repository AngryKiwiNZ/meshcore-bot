import json
import logging
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from meshcore import EventType

from modules.service_plugins.repeater_monitor_service import (
    RepeaterMonitorService,
    RepeaterTarget,
)


def test_fresh_observed_advert_confirms_missing_cli_reply(tmp_path):
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = str(tmp_path / "bot.db")
    node_key = "34" * 32
    requested_at = time.time()
    with sqlite3.connect(service.db_path) as conn:
        conn.execute(
            """CREATE TABLE complete_contact_tracking (
                   public_key TEXT,
                   last_advert_timestamp TEXT
               )"""
        )
        conn.execute(
            "INSERT INTO complete_contact_tracking VALUES (?, ?)",
            (node_key, datetime.fromtimestamp(requested_at + 1).isoformat()),
        )

    assert service._advert_observed_since(node_key, requested_at) is True


@pytest.mark.asyncio
async def test_tiny_future_drift_skips_clkreboot():
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service._append_command_log = Mock()
    commands = Mock()
    commands.send_msg = AsyncMock()
    service.bot = Mock(meshcore=Mock(commands=commands))
    target = RepeaterTarget(node_key="56" * 32, display_name="Near Clock")

    assert await service._reset_repeater_clock_if_needed(
        {"public_key": target.node_key},
        target,
        1_004,
        1_000,
    ) is True
    commands.send_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_clock_sync_login_rejects_non_admin_access():
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.command_timeout_seconds = 5
    service.login_retry_attempts = 2
    service._append_command_log = Mock()
    commands = Mock()
    commands.send_login_sync = AsyncMock(
        return_value=SimpleNamespace(
            type=EventType.LOGIN_SUCCESS,
            payload={"permissions": 0, "is_admin": False},
        )
    )
    service.bot = Mock(meshcore=Mock(commands=commands))
    target = RepeaterTarget(node_key="12" * 32, display_name="Guest Repeater")

    login_ok, error = await service._attempt_login(
        {"public_key": target.node_key},
        target,
        1,
        password="",
        require_admin=True,
    )

    assert login_ok is False
    assert error == "admin_access_required"
    assert any(
        "admin ACL" in call.args[1]
        for call in service._append_command_log.call_args_list
    )


@pytest.mark.asyncio
async def test_clock_sync_logs_in_and_sends_commands_in_order(tmp_path):
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = str(tmp_path / "bot.db")
    service.status_path = str(tmp_path / "status.json")
    service.logger = logging.getLogger("test_repeater_clock_sync")
    service.login_retry_attempts = 2
    service.retry_delay_seconds = 0
    service.command_timeout_seconds = 5
    service.manual_command_timeout_seconds = 15
    service.clock_sync_command_delay_seconds = 0
    service.clock_sync_advert_delay_seconds = 0
    service._init_tables()

    node_key = "ab" * 32
    target = RepeaterTarget(node_key=node_key, display_name="Test Repeater")

    contact = {
        "public_key": node_key,
        "adv_name": "Test Repeater",
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
    }
    commands = Mock()
    commands.get_time = AsyncMock(
        return_value=SimpleNamespace(
            type=EventType.CURRENT_TIME,
            payload={"time": int(time.time())},
        )
    )
    commands.set_time = AsyncMock(return_value=SimpleNamespace(type=EventType.OK))
    host_epoch = int(time.time())
    commands.send_login_sync = AsyncMock(
        side_effect=[
            SimpleNamespace(
                type=EventType.LOGIN_SUCCESS,
                payload={
                    "permissions": 1,
                    "is_admin": True,
                    "server_timestamp": host_epoch - 300,
                },
            ),
            SimpleNamespace(
                type=EventType.LOGIN_SUCCESS,
                payload={
                    "permissions": 1,
                    "is_admin": True,
                    "server_timestamp": host_epoch,
                },
            ),
        ]
    )
    commands.send_cmd = AsyncMock(return_value=SimpleNamespace(type=EventType.MSG_SENT))
    commands.send_msg = AsyncMock(return_value=SimpleNamespace(type=EventType.MSG_SENT))
    commands.reset_path = AsyncMock(return_value=SimpleNamespace(type=EventType.OK))
    commands.send_logout = AsyncMock(return_value=SimpleNamespace(type=EventType.OK))
    meshcore = Mock()
    meshcore.commands = commands
    meshcore.ensure_contacts = AsyncMock()
    service.bot = Mock(meshcore=meshcore)
    service._resolve_contact = AsyncMock(return_value=contact)
    current_utc = time.gmtime()
    clock_reply = time.strftime("%H:%M - %d/%m/%Y UTC", current_utc)
    sent_event = SimpleNamespace(type=EventType.MSG_SENT)
    service._send_cli_command_with_reply = AsyncMock(
        side_effect=[
            (sent_event, None),
            (sent_event, "OK - Advert sent"),
        ]
    )

    assert await service._clock_sync_by_node_key(node_key, [target]) is True

    assert commands.send_login_sync.await_count == 2
    assert all(
        call.args == (contact, "") and call.kwargs == {"timeout": 15}
        for call in commands.send_login_sync.await_args_list
    )
    commands.send_cmd.assert_not_awaited()
    commands.reset_path.assert_awaited_once_with(contact)
    assert [call.args[1] for call in service._send_cli_command_with_reply.await_args_list] == [
        "clock sync",
        "advert",
    ]
    commands.get_time.assert_awaited_once()
    commands.set_time.assert_not_awaited()
    commands.send_logout.assert_awaited_once_with(contact)
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "idle"
    assert status["clock_sync_success"] is True

    with sqlite3.connect(service.db_path) as conn:
        messages = [
            row[0]
            for row in conn.execute(
                """SELECT message FROM repeater_monitor_command_log
                   WHERE node_key = ? ORDER BY id""",
                (node_key,),
            )
        ]
    assert any("skipping clkreboot and syncing directly" in message for message in messages)
    assert any(message == 'Sending "clock sync" command from verified companion clock' for message in messages)
    assert any("continuing with repeater clock read-back" in message for message in messages)
    assert any(message.startswith('Repeater clock verified at ') for message in messages)
    assert any(
        message.startswith('Sending immediate "advert" command')
        for message in messages
    )
    assert any(message.startswith('Repeater confirmed immediate advert: ') for message in messages)


@pytest.mark.asyncio
async def test_clock_sync_stops_when_future_companion_recovery_fails(tmp_path):
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = str(tmp_path / "bot.db")
    service.status_path = str(tmp_path / "status.json")
    service.logger = logging.getLogger("test_repeater_clock_sync_ahead")
    service.login_retry_attempts = 2
    service.retry_delay_seconds = 0
    service.command_timeout_seconds = 5
    service.manual_command_timeout_seconds = 15
    service.clock_sync_command_delay_seconds = 0
    service.clock_sync_advert_delay_seconds = 0
    service._init_tables()

    node_key = "cd" * 32
    target = RepeaterTarget(node_key=node_key, display_name="Test Repeater")
    contact = {
        "public_key": node_key,
        "adv_name": "Test Repeater",
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
    }
    commands = Mock()
    commands.get_time = AsyncMock(
        return_value=SimpleNamespace(
            type=EventType.CURRENT_TIME,
            payload={"time": int(time.time()) + 3600},
        )
    )
    commands.send_login_sync = AsyncMock()
    commands.send_cmd = AsyncMock()
    meshcore = Mock()
    meshcore.commands = commands
    meshcore.ensure_contacts = AsyncMock()
    service.bot = Mock(meshcore=meshcore)
    service.bot.repair_future_radio_clock = AsyncMock(return_value=False)
    service._resolve_contact = AsyncMock(return_value=contact)

    assert await service._clock_sync_by_node_key(node_key, [target]) is False
    service.bot.repair_future_radio_clock.assert_awaited_once()
    commands.send_login_sync.assert_not_awaited()
    commands.send_cmd.assert_not_awaited()

    with sqlite3.connect(service.db_path) as conn:
        messages = [
            row[0]
            for row in conn.execute(
                """SELECT message FROM repeater_monitor_command_log
                   WHERE node_key = ? ORDER BY id""",
                (node_key,),
            )
        ]
    assert any("companion clock recovery failed" in message for message in messages)


@pytest.mark.asyncio
async def test_clock_sync_stops_when_clkreboot_does_not_reset_remote_clock(tmp_path):
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = str(tmp_path / "bot.db")
    service.status_path = str(tmp_path / "status.json")
    service.logger = logging.getLogger("test_repeater_clock_sync_replay_locked")
    service.login_retry_attempts = 1
    service.retry_delay_seconds = 0
    service.command_timeout_seconds = 5
    service.manual_command_timeout_seconds = 15
    service.clock_sync_command_delay_seconds = 0
    service.clock_sync_advert_delay_seconds = 0
    service._last_login_server_timestamps = {}
    service._init_tables()

    node_key = "ef" * 32
    target = RepeaterTarget(node_key=node_key, display_name="Locked Repeater")
    contact = {
        "public_key": node_key,
        "adv_name": "Locked Repeater",
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
    }
    host_epoch = int(time.time())
    remote_future_epoch = host_epoch + 100_000
    commands = Mock()
    commands.get_time = AsyncMock(
        return_value=SimpleNamespace(
            type=EventType.CURRENT_TIME,
            payload={"time": host_epoch},
        )
    )
    commands.set_time = AsyncMock(return_value=SimpleNamespace(type=EventType.OK))
    commands.send_login_sync = AsyncMock(
        side_effect=[
            SimpleNamespace(
                type=EventType.LOGIN_SUCCESS,
                payload={
                    "is_admin": True,
                    "server_timestamp": remote_future_epoch,
                },
            ),
            SimpleNamespace(
                type=EventType.LOGIN_SUCCESS,
                payload={
                    "is_admin": True,
                    "server_timestamp": remote_future_epoch + 10,
                },
            ),
        ]
    )
    commands.send_cmd = AsyncMock(return_value=SimpleNamespace(type=EventType.MSG_SENT))
    commands.send_msg = AsyncMock(return_value=SimpleNamespace(type=EventType.MSG_SENT))
    commands.send_logout = AsyncMock(return_value=SimpleNamespace(type=EventType.OK))
    meshcore = Mock(commands=commands)
    meshcore.ensure_contacts = AsyncMock()
    service.bot = Mock(meshcore=meshcore)
    service._resolve_contact = AsyncMock(return_value=contact)
    service._send_cli_command_with_reply = AsyncMock()

    assert await service._clock_sync_by_node_key(node_key, [target]) is False

    assert [call.args[1] for call in commands.send_msg.await_args_list] == ["clkreboot"]
    assert commands.send_msg.await_args.kwargs["timestamp"] > 0
    service._send_cli_command_with_reply.assert_not_awaited()
    commands.send_logout.assert_awaited_once_with(contact)
    with sqlite3.connect(service.db_path) as conn:
        messages = [
            row[0]
            for row in conn.execute(
                """SELECT message FROM repeater_monitor_command_log
                   WHERE node_key = ? ORDER BY id""",
                (node_key,),
            )
        ]
    assert any("admin replay counter is locked ahead" in message for message in messages)
