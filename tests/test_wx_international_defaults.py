"""Tests for wx_international default location behavior."""

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.models import MeshMessage


def _make_bot(default_weather_location=None):
    bot = MagicMock()
    bot.logger = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.logger.debug = Mock()

    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "default_state", "")
    config.set("Weather", "default_country", "NZ")
    config.set("Weather", "temperature_unit", "celsius")
    config.set("Weather", "wind_speed_unit", "kmh")
    config.set("Weather", "precipitation_unit", "mm")
    if default_weather_location is not None:
        config.set("Weather", "default_weather_location", default_weather_location)

    bot.config = config
    bot.db_manager = Mock()
    bot.translator.translate = Mock(side_effect=lambda key, **kwargs: key)
    bot.translator.get_value = Mock(return_value=None)
    return bot


def test_default_weather_location_is_empty_when_not_configured():
    """A location must be configured explicitly for privacy-safe defaults."""
    bot = _make_bot()
    with patch("modules.commands.alternatives.wx_international.get_nominatim_geocoder", return_value=Mock()):
        cmd = GlobalWxCommand(bot)
    assert cmd.default_weather_location == ""


def test_default_weather_location_uses_config_value():
    """Configured default_weather_location should be respected."""
    bot = _make_bot("Example City")
    with patch("modules.commands.alternatives.wx_international.get_nominatim_geocoder", return_value=Mock()):
        cmd = GlobalWxCommand(bot)
    assert cmd.default_weather_location == "Example City"


def test_companion_location_default_can_be_disabled():
    """A bare wx can use a configured default instead of sender coordinates."""
    bot = _make_bot("Example City")
    bot.config.set("Weather", "use_companion_location_for_default", "false")
    with patch(
        "modules.commands.alternatives.wx_international.get_nominatim_geocoder",
        return_value=Mock(),
    ):
        cmd = GlobalWxCommand(bot)

    assert cmd.use_companion_location_for_default is False


def test_bare_wx_uses_configured_default_when_companion_default_is_disabled():
    bot = _make_bot("Example City")
    bot.config.set("Weather", "use_companion_location_for_default", "false")
    with patch(
        "modules.commands.alternatives.wx_international.get_nominatim_geocoder",
        return_value=Mock(),
    ):
        cmd = GlobalWxCommand(bot)
    cmd._get_companion_location = Mock(return_value=(-41.33, 173.18))
    cmd.get_weather_for_location = AsyncMock(return_value="Example forecast")
    cmd.send_response = AsyncMock(return_value=True)

    result = asyncio.run(cmd.execute(MeshMessage(
        content="wx",
        sender_id="test",
        sender_pubkey="ab" * 32,
        is_dm=True,
    )))

    assert result is True
    cmd._get_companion_location.assert_not_called()
    assert cmd.get_weather_for_location.await_args.args[0] == "Example City"


def test_unknown_location_falls_back_to_default_weather_location():
    """Unknown locations should retry using the configured default location."""
    bot = _make_bot("Example City")
    with patch("modules.commands.alternatives.wx_international.get_nominatim_geocoder", return_value=Mock()):
        cmd = GlobalWxCommand(bot)

    metservice_weather = {
        "current_conditions": {
            "observations": {
                "temperature": [{"current": 15}],
                "pressure": [{}],
                "rain": [{}],
                "wind": [{}],
            }
        },
        "daily_forecast": {
            "days": [
                {"condition": "fine", "highTemp": 18, "lowTemp": 9},
                {"condition": "cloudy", "highTemp": 17, "lowTemp": 10},
                {"condition": "rain", "highTemp": 16, "lowTemp": 11},
            ]
        },
    }

    cmd.geocode_location = Mock(
        side_effect=[
            (None, None, None, None),
            (12.34, 56.78, {"country_code": "NZ", "city": "Example City"}, Mock()),
        ]
    )
    cmd._format_location_display = Mock(return_value="Example City")
    cmd._resolve_metservice_path = Mock(return_value="/towns-cities/regions/example/locations/example-city")

    with patch(
        "modules.commands.alternatives.wx_international.fetch_metservice_public_weather",
        return_value=metservice_weather,
    ):
        response = asyncio.run(cmd.get_weather_for_location("notarealplace"))

    assert response[0] == "multi_message"
    assert response[1][0].startswith("Example City:")
    assert cmd.geocode_location.call_args_list[1].args[0] == "Example City"
