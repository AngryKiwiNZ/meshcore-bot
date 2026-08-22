"""Tests for modules.commands.airplanes_command."""

from unittest.mock import Mock, patch

import pytest

from modules.commands.airplanes_command import AirplanesCommand
from tests.conftest import command_mock_bot, mock_message


class TestAirplanesCommand:
    """Tests for AirplanesCommand."""

    def test_matches_singular_airplane_alias(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        command_mock_bot.config.set("Airplanes_Command", "enabled", "true")
        cmd = AirplanesCommand(command_mock_bot)

        msg = mock_message(content="airplane", is_dm=True)

        assert cmd.matches_keyword(msg) is True

    def test_normalizes_default_airplanes_live_url_to_https(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        command_mock_bot.config.set("Airplanes_Command", "enabled", "true")
        command_mock_bot.config.set("Airplanes_Command", "api_url", "http://api.airplanes.live/v2/")

        cmd = AirplanesCommand(command_mock_bot)

        assert cmd.api_url == "https://api.airplanes.live/v2/"

    def test_splits_named_location_and_trailing_radius(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        cmd = AirplanesCommand(command_mock_bot)

        location, filters = cmd._split_location_and_filters(
            ["christchurch,", "new", "zealand", "50nm", "closest"]
        )

        assert location == "christchurch, new zealand"
        assert filters == ["radius=50", "closest"]

    @pytest.mark.asyncio
    async def test_named_location_is_geocoded_and_used(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        cmd = AirplanesCommand(command_mock_bot)
        cmd._fetch_aircraft_data = Mock(return_value={"ac": []})

        with patch(
            "modules.commands.airplanes_command.geocode_city_sync",
            return_value=(-43.5321, 172.6362, None),
        ) as geocode:
            result = await cmd.execute(
                mock_message(content="airplanes christchurch, new zealand", is_dm=True)
            )

        assert result is True
        geocode.assert_called_once()
        assert geocode.call_args.args[1] == "christchurch, new zealand"
        cmd._fetch_aircraft_data.assert_called_once_with(-43.5321, 172.6362, 25)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("radius_arg", ["100", "100nm"])
    async def test_radius_shorthand_uses_default_location(
        self, command_mock_bot, radius_arg
    ):
        command_mock_bot.config.add_section("Airplanes_Command")
        cmd = AirplanesCommand(command_mock_bot)
        cmd._get_companion_location = Mock(return_value=(-41.2706, 173.2840))
        cmd._fetch_aircraft_data = Mock(return_value={"ac": []})

        result = await cmd.execute(
            mock_message(content=f"airplanes {radius_arg}", is_dm=True)
        )

        assert result is True
        cmd._fetch_aircraft_data.assert_called_once_with(-41.2706, 173.2840, 100.0)

    @pytest.mark.asyncio
    async def test_named_location_with_trailing_radius(self, command_mock_bot):
        command_mock_bot.config.add_section("Airplanes_Command")
        cmd = AirplanesCommand(command_mock_bot)
        cmd._fetch_aircraft_data = Mock(return_value={"ac": []})

        with patch(
            "modules.commands.airplanes_command.geocode_city_sync",
            return_value=(-43.5321, 172.6362, None),
        ):
            await cmd.execute(
                mock_message(content="airplanes christchurch 50", is_dm=True)
            )

        cmd._fetch_aircraft_data.assert_called_once_with(-43.5321, 172.6362, 50.0)
