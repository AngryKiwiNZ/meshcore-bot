"""Tests for weather service location configuration behavior."""

import configparser
from unittest.mock import MagicMock, Mock, patch

from modules.service_plugins.weather_service import WeatherService


def _make_bot(weather_service_overrides=None, weather_overrides=None):
    bot = MagicMock()
    bot.logger = Mock()
    bot.logger.info = Mock()
    bot.logger.warning = Mock()
    bot.logger.error = Mock()
    bot.logger.debug = Mock()

    config = configparser.ConfigParser()
    config.add_section("Weather_Service")
    config.add_section("Weather")

    config.set("Weather", "temperature_unit", "fahrenheit")
    config.set("Weather", "wind_speed_unit", "mph")
    config.set("Weather", "precipitation_unit", "inch")

    if weather_service_overrides:
        for key, value in weather_service_overrides.items():
            config.set("Weather_Service", key, str(value))

    if weather_overrides:
        for key, value in weather_overrides.items():
            config.set("Weather", key, str(value))

    bot.config = config
    return bot


def test_weather_service_uses_geocoded_location_when_configured():
    """Setting weather_location geocodes position and keeps service enabled."""
    bot = _make_bot({"weather_location": "Seattle, WA"})

    mock_response = Mock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "results": [
            {
                "name": "Seattle",
                "country": "United States",
                "latitude": 47.6062,
                "longitude": -122.3321,
            }
        ]
    }

    mock_session = Mock()
    mock_session.get.return_value = mock_response

    with patch.object(WeatherService, "_create_retry_session", return_value=mock_session):
        service = WeatherService(bot)

    assert service.enabled is True
    assert service.my_position_lat == 47.6062
    assert service.my_position_lon == -122.3321
    assert service._cached_location_name == "Seattle, United States"


def test_weather_service_disables_without_location_or_coordinates():
    """Service is disabled when neither coordinates nor weather_location are configured."""
    bot = _make_bot()

    mock_session = Mock()
    with patch.object(WeatherService, "_create_retry_session", return_value=mock_session):
        service = WeatherService(bot)

    assert service.enabled is False


def test_weather_service_disables_when_geocoding_has_no_results():
    """Service is disabled when weather_location geocoding fails and no coordinates exist."""
    bot = _make_bot({"weather_location": "NotARealPlace123"})

    mock_response = Mock()
    mock_response.ok = True
    mock_response.json.return_value = {"results": []}

    mock_session = Mock()
    mock_session.get.return_value = mock_response

    with patch.object(WeatherService, "_create_retry_session", return_value=mock_session):
        service = WeatherService(bot)

    assert service.enabled is False
