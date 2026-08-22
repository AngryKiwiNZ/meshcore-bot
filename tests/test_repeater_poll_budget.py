import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.service_plugins.repeater_monitor_service import (
    RepeaterMonitorService,
    RepeaterTarget,
)


def _poll_service(tmp_path):
    service = RepeaterMonitorService.__new__(RepeaterMonitorService)
    service.db_path = str(tmp_path / "bot.db")
    service.logger = logging.getLogger("test_repeater_poll_budget")
    service._next_target_update_times = {}
    service._consecutive_failures = {}
    service._last_login_times = {}
    service.path_reset_after_failures = 3
    service.path_refresh_on_retry = True
    service.poll_login_attempts = 1
    service.command_timeout_seconds = 10
    service.manual_command_timeout_seconds = 10
    service.data_request_attempts = 4
    service.data_request_retry_delay_seconds = 3
    service.manual_data_request_attempts = 4
    service.manual_data_request_retry_delay_seconds = 3
    service.collect_temperature = True
    service.bot = SimpleNamespace(
        config=SimpleNamespace(getint=lambda *_args, **_kwargs: 1),
        meshcore=SimpleNamespace(commands=SimpleNamespace(send_logout=AsyncMock())),
    )
    service.clock_drift_threshold_seconds = 120
    service._append_command_log = Mock()
    service._resolve_contact = AsyncMock(
        return_value={"public_key": "ab" * 32, "adv_name": "Test", "out_path": "01"}
    )
    service._prime_contact_for_anon_requests = Mock()
    service._refresh_contact_path = AsyncMock()
    service._reset_contact_path = AsyncMock(return_value=True)
    service._attempt_login = AsyncMock(return_value=(True, None))
    service._should_attempt_login = Mock(return_value=False)
    service._latest_advert_clock_snapshot = Mock(return_value=(None, None))
    service._mark_target_success = Mock()
    service._mark_target_failure = Mock()
    service._store_result = Mock()
    return service


@pytest.mark.asyncio
async def test_scheduled_poll_has_only_one_route_fallback(tmp_path):
    service = _poll_service(tmp_path)
    service._collect_repeater_data = AsyncMock(
        side_effect=[(None, None, "missing"), (None, None, "missing")]
    )
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    await service._poll_target(target, force=False)

    assert service._collect_repeater_data.await_count == 2
    service._reset_contact_path.assert_awaited_once()
    service._attempt_login.assert_not_awaited()
    assert sum(
        call.kwargs["request_attempts"]
        for call in service._collect_repeater_data.await_args_list
    ) == 4


@pytest.mark.asyncio
async def test_manual_poll_uses_full_status_budget_after_login(tmp_path):
    service = _poll_service(tmp_path)
    service._collect_repeater_data = AsyncMock(return_value=(None, None, "missing"))
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    await service._poll_target(target, force=True)

    assert service._collect_repeater_data.await_count == 1
    service._reset_contact_path.assert_awaited_once()
    service._attempt_login.assert_awaited_once()
    service.bot.meshcore.commands.send_logout.assert_awaited_once()
    assert service._collect_repeater_data.await_args.kwargs["request_attempts"] == 4


@pytest.mark.asyncio
async def test_manual_poll_retries_login_before_requesting_status(tmp_path):
    service = _poll_service(tmp_path)
    service.poll_login_attempts = 4
    service.retry_delay_seconds = 0
    service._attempt_login = AsyncMock(
        side_effect=[
            (False, "login_failed"),
            (False, "login_failed"),
            (True, None),
        ]
    )
    service._collect_repeater_data = AsyncMock(
        return_value=({"bat": 4100}, None, None)
    )
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    await service._poll_target(target, force=True)

    assert service._attempt_login.await_count == 3
    service._reset_contact_path.assert_awaited_once()
    service._refresh_contact_path.assert_not_awaited()
    service._collect_repeater_data.assert_awaited_once()
    assert service._collect_repeater_data.await_args.kwargs["request_attempts"] == 4
    stored = service._store_result.call_args.kwargs
    assert stored["login_ok"] is True
    assert stored["status_ok"] is True
    assert stored["error_text"] == "advert_clock_unavailable"
    assert stored["error_text"] != "login_failed"


@pytest.mark.asyncio
async def test_login_response_for_another_repeater_is_rejected(tmp_path):
    service = _poll_service(tmp_path)
    service.login_retry_attempts = 4
    service._attempt_login = RepeaterMonitorService._attempt_login.__get__(service)
    service._last_login_server_timestamps = {}
    service.command_timeout_seconds = 30
    service.bot.meshcore.commands.send_login_sync = AsyncMock(
        return_value=SimpleNamespace(
            type=__import__("meshcore").EventType.LOGIN_SUCCESS,
            payload={"pubkey_prefix": "cd" * 6},
        )
    )
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    login_ok, error = await service._attempt_login(
        {"public_key": "ab" * 32},
        target,
        attempt=1,
        attempts_total=4,
    )

    assert login_ok is False
    assert error == "login_response_target_mismatch"


@pytest.mark.asyncio
async def test_manual_poll_resets_route_once_before_collecting(tmp_path):
    service = _poll_service(tmp_path)
    reset_counts_at_collection = []

    async def collect(**_kwargs):
        reset_counts_at_collection.append(service._reset_contact_path.await_count)
        return None, None, "missing"

    service._collect_repeater_data = AsyncMock(side_effect=collect)
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    await service._poll_target(target, force=True)

    assert reset_counts_at_collection == [1]
    service._reset_contact_path.assert_awaited_once()
    service._attempt_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_poll_does_not_request_data_after_all_logins_fail(tmp_path):
    service = _poll_service(tmp_path)
    service.poll_login_attempts = 4
    service.manual_data_request_retry_delay_seconds = 0
    service._attempt_login = AsyncMock(return_value=(False, "login_failed"))
    service._collect_repeater_data = AsyncMock()
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    await service._poll_target(target, force=True)

    assert service._attempt_login.await_count == 4
    service._reset_contact_path.assert_awaited_once()
    service._refresh_contact_path.assert_not_awaited()
    service._collect_repeater_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_any_telemetry_reply_stops_fallbacks(tmp_path):
    service = _poll_service(tmp_path)
    telemetry = [{"type": "temperature", "value": 21.5}]
    service._collect_repeater_data = AsyncMock(return_value=(None, telemetry, None))
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    await service._poll_target(target, force=False)

    service._collect_repeater_data.assert_awaited_once()
    service._reset_contact_path.assert_not_awaited()
    service._attempt_login.assert_not_awaited()


def test_failed_repeaters_back_off_instead_of_retrying_early(tmp_path):
    service = _poll_service(tmp_path)
    service.poll_interval_seconds = 14_400
    service.backoff_base = 2
    service.max_failure_backoff_seconds = 86_400

    assert service._failure_backoff_seconds(1) == 14_400
    assert service._failure_backoff_seconds(2) == 28_800
    assert service._failure_backoff_seconds(4) == 86_400


def test_flood_contact_is_not_replaced_with_stale_tracked_path(tmp_path):
    service = _poll_service(tmp_path)
    service._prime_contact_for_anon_requests = (
        RepeaterMonitorService._prime_contact_for_anon_requests.__get__(service)
    )
    service._lookup_tracked_contact_path = Mock(
        return_value={"out_path": "7d02", "out_path_len": 2, "out_bytes_per_hop": 2}
    )
    contact = {
        "public_key": "ab" * 32,
        "adv_name": "Test",
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": 0,
    }
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    assert service._prime_contact_for_anon_requests(contact, target) is False
    service._lookup_tracked_contact_path.assert_not_called()
    assert contact["out_path_len"] == -1
    assert contact["out_path"] == ""


def test_one_byte_contact_is_promoted_to_observed_two_byte_route(tmp_path):
    service = _poll_service(tmp_path)
    service._prime_contact_for_anon_requests = (
        RepeaterMonitorService._prime_contact_for_anon_requests.__get__(service)
    )
    service._lookup_tracked_contact_path = Mock(
        return_value={"out_path": "0262", "out_path_len": 1, "out_bytes_per_hop": 1}
    )
    contact = {
        "public_key": "ab" * 32,
        "adv_name": "Endurance",
        "out_path": "02",
        "out_path_len": 1,
        "out_path_hash_mode": 0,
    }
    target = RepeaterTarget(node_key="ab" * 32, display_name="Endurance")

    assert service._prime_contact_for_anon_requests(contact, target) is True
    assert contact["out_path"] == "0262"
    assert contact["out_path_len"] == 1
    assert contact["out_path_hash_mode"] == 1


def test_fixed_two_byte_route_counts_hops_not_bytes(tmp_path):
    service = _poll_service(tmp_path)
    service._prime_contact_for_anon_requests = (
        RepeaterMonitorService._prime_contact_for_anon_requests.__get__(service)
    )
    contact = {
        "public_key": "ab" * 32,
        "adv_name": "Endurance",
        "out_path": "02",
        "out_path_len": 1,
        "out_path_hash_mode": 0,
    }
    target = RepeaterTarget(
        node_key="ab" * 32,
        display_name="Endurance",
        fixed_out_path="0262",
    )

    assert service._prime_contact_for_anon_requests(contact, target) is True
    assert contact["out_path"] == "0262"
    assert contact["out_path_len"] == 1
    assert contact["out_path_hash_mode"] == 1


@pytest.mark.asyncio
async def test_status_only_mode_does_not_transmit_telemetry_request(tmp_path):
    service = _poll_service(tmp_path)
    service.collect_temperature = False
    service.bot.meshcore.commands.req_status_sync = AsyncMock(
        return_value={"bat": 4000}
    )
    service.bot.meshcore.commands.req_telemetry_sync = AsyncMock()
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    status, telemetry, error = await service._collect_repeater_data(
        contact={"public_key": "ab" * 32},
        target=target,
        status_payload=None,
        telemetry_payload=None,
        request_attempts=1,
        request_retry_delay_seconds=0,
        request_timeout_seconds=10,
    )

    assert status == {"bat": 4000}
    assert telemetry is None
    assert error is None
    service.bot.meshcore.commands.req_telemetry_sync.assert_not_awaited()


@pytest.mark.asyncio
async def test_temperature_request_is_skipped_when_status_is_silent(tmp_path):
    service = _poll_service(tmp_path)
    service.collect_temperature = True
    service.bot.meshcore.commands.req_status_sync = AsyncMock(return_value=None)
    service.bot.meshcore.commands.req_telemetry_sync = AsyncMock()
    target = RepeaterTarget(node_key="ab" * 32, display_name="Test")

    status, telemetry, error = await service._collect_repeater_data(
        contact={"public_key": "ab" * 32},
        target=target,
        status_payload=None,
        telemetry_payload=None,
        request_attempts=1,
        request_retry_delay_seconds=0,
        request_timeout_seconds=10,
    )

    assert status is None
    assert telemetry is None
    assert error == "status_response_missing"
    service.bot.meshcore.commands.req_telemetry_sync.assert_not_awaited()
