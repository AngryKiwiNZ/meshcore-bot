"""Tests for wx_international default location behavior."""

import configparser
from unittest.mock import MagicMock, Mock, patch

from modules.commands.alternatives.wx_international import GlobalWxCommand


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
    return bot


def test_default_weather_location_falls_back_to_nelson():
    """When config value is missing, Nelson, New Zealand is used."""
    bot = _make_bot()
    with patch("modules.commands.alternatives.wx_international.get_nominatim_geocoder", return_value=Mock()):
        cmd = GlobalWxCommand(bot)
    assert cmd.default_weather_location == "Nelson, New Zealand"


def test_default_weather_location_uses_config_value():
    """Configured default_weather_location should be respected."""
    bot = _make_bot("Christchurch, New Zealand")
    with patch("modules.commands.alternatives.wx_international.get_nominatim_geocoder", return_value=Mock()):
        cmd = GlobalWxCommand(bot)
    assert cmd.default_weather_location == "Christchurch, New Zealand"
