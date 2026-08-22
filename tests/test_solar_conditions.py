"""Tests for modules.solar_conditions."""

import configparser
from datetime import datetime, timezone

from modules import solar_conditions


def test_get_satpass_display_times_uses_configured_timezone():
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "timezone", "Pacific/Auckland")

    original_config = solar_conditions._config
    try:
        solar_conditions.set_config(config)

        rise_local, set_local = solar_conditions._get_satpass_display_times(
            int(datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc).timestamp()),
            600,
        )

        assert rise_local.tzname() == "NZST"
        assert rise_local.hour == 17
        assert set_local.hour == 17
        assert set_local.minute == 10
    finally:
        solar_conditions._config = original_config
