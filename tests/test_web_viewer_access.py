#!/usr/bin/env python3

import logging
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

from modules.db_manager import ensure_game_schema, ensure_llm_schema
from modules.service_plugins.repeater_monitor_service import RepeaterMonitorService, RepeaterTarget
from modules.web_viewer.app import BotDataViewer


def _build_viewer(monkeypatch, tmp_path, access_password="secret", read_access_password=None):
    config_path = tmp_path / "config.ini"
    db_path = tmp_path / "viewer.db"
    config_lines = [
        "[Bot]",
        f"db_path = {db_path}",
        "bot_name = Test Bot",
        "",
        "[Web_Viewer]",
        "session_secret = test-session-secret",
        "catchup_channels = Public,#community",
    ]
    if access_password is not None:
        config_lines.append(f"access_password = {access_password}")
    if read_access_password is not None:
        config_lines.append(f"read_access_password = {read_access_password}")
    config_lines.extend([
        "",
        "[Repeater_Monitor]",
        "enabled = true",
        f"refresh_trigger_file = {tmp_path / 'repeater-refresh.trigger'}",
        f"status_file = {tmp_path / 'repeater-status.json'}",
        f"polling_control_file = {tmp_path / 'repeater-control.json'}",
        f"nodes_file = {tmp_path / 'repeater-nodes.txt'}",
        "",
        "[LLM]",
        f"profiles_dir = {tmp_path / 'profiles'}",
        "max_profile_chars = 20",
    ])
    config_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")

    def fake_setup_logging(self):
        self.logger = logging.getLogger(f"test_web_viewer_{id(self)}")
        self.logger.handlers.clear()
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def fake_init_databases(self):
        self.db_manager = Mock()
        self.repeater_manager = Mock()
        self.mesh_graph = Mock()

    monkeypatch.setattr(BotDataViewer, "_setup_logging", fake_setup_logging)
    monkeypatch.setattr(BotDataViewer, "_init_databases", fake_init_databases)
    monkeypatch.setattr(BotDataViewer, "_start_database_polling", lambda self: None)
    monkeypatch.setattr(BotDataViewer, "_start_cleanup_scheduler", lambda self: None)
    monkeypatch.setattr(BotDataViewer, "_get_version_info", lambda self: {})

    viewer = BotDataViewer(config_path=str(config_path))
    with sqlite3.connect(db_path) as conn:
        ensure_llm_schema(conn)
        ensure_game_schema(conn)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS complete_contact_tracking (
                   public_key TEXT PRIMARY KEY,
                   name TEXT,
                   role TEXT
               )"""
        )
    viewer._optimize_database = lambda: {"success": True}
    return viewer


def test_public_pages_and_read_apis_work_without_login(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()

    response = client.get("/")
    assert response.status_code == 200
    assert b"read-only mode" in response.data.lower()
    assert b'href="/cache"' not in response.data

    cache_page = client.get("/cache", follow_redirects=False)
    assert cache_page.status_code == 302
    assert "/login" in cache_page.headers["Location"]
    cache_api = client.get("/api/cache")
    assert cache_api.status_code == 401
    assert b'href="/catchup"' in response.data

    health_response = client.get("/api/health")
    assert health_response.status_code == 200
    assert health_response.get_json()["status"] == "healthy"


def test_public_catchup_only_returns_configured_channel_messages(
    monkeypatch,
    tmp_path,
):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    local_now = datetime.now(viewer._catchup_timezone())
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    message_time = int((day_start + timedelta(hours=12)).timestamp())
    with sqlite3.connect(viewer.db_path) as conn:
        conn.execute(
            """
            CREATE TABLE message_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                sender_id TEXT NOT NULL,
                channel TEXT,
                content TEXT NOT NULL,
                is_dm BOOLEAN NOT NULL,
                hops INTEGER,
                snr REAL,
                rssi INTEGER,
                path TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO message_stats
                (timestamp, sender_id, channel, content, is_dm)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (message_time, "Alice", "Public", "Public update", 0),
                (message_time + 1, "Bob", "#community", "Community update", 0),
                (message_time + 2, "Carol", "#private", "Private channel", 0),
                (message_time + 3, "Dave", "Public", "Private DM", 1),
            ],
        )

    client = viewer.app.test_client()
    page = client.get("/catchup")
    assert page.status_code == 200
    assert b"Channel Catch-up" in page.data
    assert b'id="public-stream"' in page.data
    assert b'id="community-stream"' in page.data

    response = client.get(
        f"/api/catchup/messages?date={local_now.date().isoformat()}"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert [message["content"] for message in payload["messages"]] == [
        "Public update",
        "Community update",
    ]
    assert {message["channel"] for message in payload["messages"]} == {
        "Public",
        "#community",
    }

    too_old = local_now.date() - timedelta(days=7)
    assert client.get(f"/api/catchup/messages?date={too_old.isoformat()}").status_code == 400


def test_write_api_requires_login_when_password_is_set(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()

    response = client.post("/api/optimize-database")

    assert response.status_code == 401
    assert response.get_json()["error"] == "write_access_required"


def test_repeater_poll_now_is_available_without_login_but_other_refresh_is_not(
    monkeypatch,
    tmp_path,
):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = viewer.db_path
    service._init_tables()
    client = viewer.app.test_client()
    node_key = "ef" * 32
    service._store_result(
        target=RepeaterTarget(node_key=node_key, display_name="Public Action Repeater"),
        collected_at=time.time(),
        login_ok=False,
        status_ok=False,
        clock_ok=False,
        login_attempts=0,
    )

    poll_response = client.post(f"/api/repeater-monitor/refresh/{node_key}")

    assert poll_response.status_code == 200
    assert poll_response.get_json()["node_key"] == node_key
    status = client.get("/api/repeater-monitor/status").get_json()
    assert status["state"] == "refresh_queued"
    assert status["requested_node_key"] == node_key

    all_repeaters_response = client.post("/api/repeater-monitor/refresh")
    assert all_repeaters_response.status_code == 401
    assert all_repeaters_response.get_json()["error"] == "write_access_required"

    clock_sync_response = client.post(f"/api/repeater-monitor/clock-sync/{node_key}")
    assert clock_sync_response.status_code == 200
    trigger_payload = json.loads(
        Path(viewer.repeater_monitor_refresh_trigger_path).read_text(encoding="utf-8")
    )
    assert trigger_payload["action"] == "clock_sync"


def test_admin_can_suspend_scheduled_polling_while_public_poll_now_remains_available(
    monkeypatch,
    tmp_path,
):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = viewer.db_path
    service._init_tables()
    client = viewer.app.test_client()
    node_key = "ef" * 32

    unauthenticated_toggle = client.post(
        "/api/repeater-monitor/polling-enabled",
        json={"enabled": False},
    )
    assert unauthenticated_toggle.status_code == 401
    assert b'id="global-polling-toggle"' not in client.get("/repeaters").data

    client.post(
        "/login",
        data={"password": "secret", "next": "/repeaters"},
    )
    assert b'id="global-polling-toggle"' in client.get("/repeaters").data
    toggle = client.post(
        "/api/repeater-monitor/polling-enabled",
        json={"enabled": False},
    )
    assert toggle.status_code == 200
    assert toggle.get_json()["polling_enabled"] is False

    client.get("/logout")
    manual_poll = client.post(f"/api/repeater-monitor/refresh/{node_key}")
    assert manual_poll.status_code == 200
    assert manual_poll.get_json()["node_key"] == node_key
    assert client.get("/api/repeater-monitor/status").get_json()["polling_enabled"] is False

    # Clock Sync remains an explicitly requested maintenance action.
    assert client.post(f"/api/repeater-monitor/clock-sync/{node_key}").status_code == 404


def test_signed_loopback_stream_write_bypasses_browser_login(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()

    denied = client.post("/api/stream_data", json={"type": "invalid", "data": {}})
    assert denied.status_code == 401

    accepted_by_auth = client.post(
        "/api/stream_data",
        json={"type": "invalid", "data": {}},
        headers={"X-MeshCore-Internal-Token": viewer._internal_stream_token()},
    )
    assert accepted_by_auth.status_code == 400


def test_login_unlocks_write_access(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()

    login_response = client.post(
        "/login",
        data={"password": "secret", "next": "/cache"},
        follow_redirects=False,
    )
    assert login_response.status_code == 302
    assert login_response.headers["Location"].endswith("/cache")

    write_response = client.post("/api/optimize-database")
    assert write_response.status_code == 200
    assert write_response.get_json()["success"] is True
    assert b'href="/cache"' in client.get("/").data


def test_write_api_stays_open_when_no_password_is_configured(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="")
    client = viewer.app.test_client()

    response = client.post("/api/optimize-database")

    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_repeater_detail_returns_last_25_commands_and_poll_now_sets_queued_state(
    monkeypatch,
    tmp_path,
):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = viewer.db_path
    service._init_tables()

    node_key = "ab" * 32
    assert service._extract_temperature_from_telemetry([
        {"channel": 3, "type": "temperature", "value": 18.75},
    ]) == 18.75
    service._store_result(
        target=RepeaterTarget(node_key=node_key, display_name="Test Repeater"),
        collected_at=time.time(),
        login_ok=False,
        status_ok=False,
        clock_ok=False,
        login_attempts=0,
        temperature_c=18.75,
    )
    with sqlite3.connect(viewer.db_path) as conn:
        for column_name, column_type in (
            ("last_advert_timestamp", "TEXT"),
            ("last_heard", "TEXT"),
            ("raw_advert_data", "TEXT"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE complete_contact_tracking ADD COLUMN {column_name} {column_type}"
                )
            except sqlite3.OperationalError:
                pass
        conn.executemany(
            """
            INSERT INTO repeater_monitor_command_log
                (node_key, logged_at, level, message)
            VALUES (?, ?, 'info', ?)
            """,
            [
                (node_key, float(index), f"command {index}")
                for index in range(30)
            ],
        )

    client = viewer.app.test_client()
    page = client.get(f"/repeaters/{node_key}")
    assert page.status_code == 200
    assert b"Repeater Command Log" in page.data
    assert b"Poll Now" in page.data
    assert b"Clock Sync" in page.data
    assert b'id="toggle-poll-history"' in page.data
    assert b'id="repeater-admin-password"' not in page.data

    overview_page = client.get("/repeaters")
    assert overview_page.status_code == 200
    assert b"Bot Activity Now" in overview_page.data
    assert b'id="overview-activity-log"' in overview_page.data

    detail = client.get(f"/api/repeater-monitor/node/{node_key}")
    assert detail.status_code == 200
    command_log = detail.get_json()["command_log"]
    assert len(command_log) == 25
    assert command_log[0]["message"] == "command 5"
    assert command_log[-1]["message"] == "command 29"
    assert detail.get_json()["node"]["last_temperature_c"] == 18.75
    assert detail.get_json()["temperature_series"][0]["y"] == 18.75

    clock_sync = client.post(f"/api/repeater-monitor/clock-sync/{node_key}")
    assert clock_sync.status_code == 200
    trigger_payload = json.loads(
        Path(viewer.repeater_monitor_refresh_trigger_path).read_text(encoding="utf-8")
    )
    assert trigger_payload["action"] == "clock_sync"
    assert trigger_payload["node_key"] == node_key

    Path(viewer.repeater_monitor_status_path).write_text(
        json.dumps({
            "state": "polling",
            "current_target": "Other Repeater",
            "current_node_key": "ef" * 32,
        }),
        encoding="utf-8",
    )
    queued = client.post(f"/api/repeater-monitor/refresh/{node_key}")
    assert queued.status_code == 200
    status = client.get("/api/repeater-monitor/status").get_json()
    assert status["state"] == "refresh_queued"
    assert status["requested_node_key"] == node_key
    assert status["active_state"] == "polling"
    assert status["active_target"] == "Other Repeater"
    assert len(status["activity_log"]) == 15
    assert status["activity_log"][-1]["display_name"] == "Test Repeater"
    assert "Poll Now requested" in status["activity_log"][-1]["message"]

    updated_log = client.get(f"/api/repeater-monitor/node/{node_key}").get_json()["command_log"]
    assert len(updated_log) == 25
    assert "Poll Now requested" in updated_log[-1]["message"]


def test_llm_console_and_api_are_private_even_for_read_only_viewers(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="write-secret", read_access_password="read-secret")
    client = viewer.app.test_client()

    page = client.get("/llm", follow_redirects=False)
    assert page.status_code == 302
    assert "/login" in page.headers["Location"]

    api = client.get("/api/llm/status")
    assert api.status_code == 401
    assert api.get_json()["error"] == "authentication_required"

    client.post("/login", data={"password": "read-secret", "next": "/llm"})
    read_page = client.get("/llm", follow_redirects=False)
    assert read_page.status_code == 302
    read_api = client.get("/api/llm/status")
    assert read_api.status_code == 401
    assert read_api.get_json()["error"] == "write_authentication_required"

    public_page = client.get("/")
    assert b"/llm" not in public_page.data


def test_login_unlocks_llm_console_and_navigation(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()
    client.post("/login", data={"password": "secret", "next": "/llm"})

    page = client.get("/llm")
    assert page.status_code == 200
    assert b"LLM Conversations" in page.data

    public_page = client.get("/")
    assert b"/llm" in public_page.data


def test_games_console_is_private_and_manages_switches(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()

    assert client.get("/games", follow_redirects=False).status_code == 302
    denied = client.get("/api/games/status")
    assert denied.status_code == 401

    client.post("/login", data={"password": "secret", "next": "/games"})
    page = client.get("/games")
    assert page.status_code == 200
    assert b"DM Games" in page.data

    saved = client.post(
        "/api/games/settings",
        json={"games": {"lemonade": False, "blackjack": True, "mastermind": True}},
    )
    assert saved.status_code == 200
    status = client.get("/api/games/status").get_json()
    assert status["games"]["lemonade"]["enabled"] is False
    assert status["games"]["blackjack"]["enabled"] is True


def test_llm_console_fails_closed_without_configured_password(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="", read_access_password="")
    client = viewer.app.test_client()

    assert client.get("/llm").status_code == 403
    assert client.get("/api/llm/status").status_code == 403
    assert client.get("/games").status_code == 403
    assert client.get("/api/games/status").status_code == 403


def test_llm_profile_limit_settings_and_prefixed_history_clear(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()
    client.post("/login", data={"password": "secret", "next": "/llm"})
    key = "ab" * 32

    too_large = client.post("/api/llm/contacts", json={
        "public_key": key,
        "contact_name": "Alice",
        "radio_id": "bot",
        "interaction_mode": "automatic",
        "enabled": True,
        "profile": "x" * 21,
    })
    assert too_large.status_code == 400

    invalid_scope = client.post("/api/llm/contacts", json={
        "public_key": key,
        "contact_name": "Alice",
        "radio_id": "bot",
        "interaction_mode": "automatic",
        "profile_scope": "everywhere",
        "enabled": True,
        "profile": "Alice context",
    })
    assert invalid_scope.status_code == 400

    saved = client.post("/api/llm/contacts", json={
        "public_key": key,
        "contact_name": "Alice",
        "radio_id": "bot",
        "interaction_mode": "automatic",
        "profile_scope": "all",
        "enabled": True,
        "profile": "Alice context",
    })
    assert saved.status_code == 200
    with sqlite3.connect(viewer.db_path) as conn:
        scope = conn.execute(
            "SELECT profile_scope FROM llm_contacts WHERE public_key = ?", (key,)
        ).fetchone()[0]
    assert scope == "all"

    settings = client.post("/api/llm/settings", json={
        "max_initial_delay_seconds": 12,
        "history_retention_days": 14,
        "globally_enabled": False,
    })
    assert settings.status_code == 200
    assert settings.get_json()["globally_enabled"] is False

    with sqlite3.connect(viewer.db_path) as conn:
        conn.execute(
            """INSERT INTO llm_messages(public_key, role, content)
               VALUES (?, 'user', 'hello')""",
            (f"bot:dm:{key}",),
        )
    cleared = client.delete(f"/api/llm/contacts/{key}/history")
    assert cleared.status_code == 200
    assert cleared.get_json()["deleted"] == 1


def test_deepseek_key_is_stored_privately_and_never_returned(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    viewer.bot_root = tmp_path
    viewer.config["LLM"]["deepseek_api_key_file"] = "data/deepseek-test-key"
    client = viewer.app.test_client()
    client.post("/login", data={"password": "secret", "next": "/llm"})
    api_key = "deepseek-test-value-not-a-real-secret"

    saved = client.post("/api/llm/settings", json={
        "provider": "deepseek",
        "deepseek_model": "deepseek-v4-flash",
        "deepseek_api_key": api_key,
        "fallback_to_ollama": True,
    })

    assert saved.status_code == 200
    payload = saved.get_json()
    assert payload["deepseek_key_configured"] is True
    assert api_key not in saved.get_data(as_text=True)
    secret_path = tmp_path / "data" / "deepseek-test-key"
    assert secret_path.read_text(encoding="utf-8").strip() == api_key
    assert os.stat(secret_path).st_mode & 0o777 == 0o600

    loaded = client.get("/api/llm/settings")
    assert loaded.status_code == 200
    assert loaded.get_json()["deepseek_key_configured"] is True
    assert api_key not in loaded.get_data(as_text=True)


def test_llm_conversation_archive_lists_and_reads_recent_transcript(monkeypatch, tmp_path):
    viewer = _build_viewer(monkeypatch, tmp_path, access_password="secret")
    client = viewer.app.test_client()
    client.post("/login", data={"password": "secret", "next": "/llm"})
    key = "cd" * 32
    conversation_key = f"bot:dm:{key}"
    now = time.time()
    with sqlite3.connect(viewer.db_path) as conn:
        conn.execute(
            """INSERT INTO complete_contact_tracking(public_key, name, role)
               VALUES (?, 'Alice', 'companion')""",
            (key,),
        )
        conn.executemany(
            """INSERT INTO llm_messages
               (public_key, role, content, message_timestamp, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (conversation_key, "user", "Hello there", now - 20, now - 20),
                (conversation_key, "assistant", "Hi Alice", None, now - 10),
                (conversation_key, "user", "Too old", now - 31 * 86400, now - 31 * 86400),
            ],
        )

    listing = client.get("/api/llm/conversations")
    assert listing.status_code == 200
    conversations = listing.get_json()["conversations"]
    assert len(conversations) == 1
    assert conversations[0]["display_name"] == "Alice"
    assert conversations[0]["message_count"] == 2

    transcript = client.get(
        "/api/llm/conversation", query_string={"key": conversation_key}
    )
    assert transcript.status_code == 200
    messages = transcript.get_json()["messages"]
    assert [message["content"] for message in messages] == ["Hello there", "Hi Alice"]
