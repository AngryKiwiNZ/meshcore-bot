# Weather Service

Provides scheduled weather forecasts and lightning detection.

---

## Quick Start

1. **Configure Bot** - Edit `config.ini`:

```ini
[Weather_Service]
enabled = true

# Your location (choose one option)
my_position_lat = 47.6062
my_position_lon = -122.3321
# weather_location = Nelson, New Zealand

# Daily forecast time
weather_alarm = 6:00              # Or "sunrise" / "sunset"

# Channels
weather_channel = #weather
alerts_channel = #weather
```

2. **Restart Bot** - Daily forecasts start automatically

---

## Configuration

### Basic Settings

```ini
[Weather_Service]
enabled = true
my_position_lat = 47.6062         # Option A: latitude
my_position_lon = -122.3321       # Option A: longitude
# weather_location = Nelson, New Zealand  # Option B: place name
weather_alarm = 6:00              # Time for daily forecast (HH:MM or sunrise/sunset)
weather_channel = #weather        # Channel for forecasts
alerts_channel = #weather         # Reserved for alert workflows
```

`weather_location` is useful when you want to configure by city/place instead of coordinates.
If both are set, `weather_location` is used to resolve coordinates on service startup.

### Lightning Detection (Optional)

Requires `paho-mqtt` library.

```ini
blitz_collection_interval = 600000     # Aggregate lightning every 10 minutes

# Define detection area (optional)
blitz_area_min_lat = 47.0
blitz_area_min_lon = -123.0
blitz_area_max_lat = 48.0
blitz_area_max_lon = -121.0
```

---

## Features

### Daily Weather Forecast

Sends forecast to `weather_channel` at configured time:

**Example Output:**
```
🌤️ Daily Weather: Seattle: ☀️Clear 68°F NNE8mph | Tomorrow: 🌧️Light Rain 55-72°F
```

**Data Includes:**
- Current conditions with emoji
- Temperature
- Wind speed and direction
- Tomorrow's forecast

**Scheduling Options:**
- Fixed time: `weather_alarm = 6:00` (24-hour format)
- Sunrise: `weather_alarm = sunrise`
- Sunset: `weather_alarm = sunset`

### Weather Alerts

NOAA weather alerts are disabled in Open-Meteo-only mode.
For non-US deployments (including New Zealand), this prevents unsupported NOAA lookups.

### Lightning Detection (Optional)

Monitors real-time lightning strikes via Blitzortung MQTT:

**Example Output:**
```
🌩️ Bellevue (15km NE)
```

**How It Works:**
1. Connects to Blitzortung MQTT broker
2. Filters strikes within configured `blitz_area`
3. Aggregates strikes every `blitz_collection_interval`
4. Reports areas with 10+ strikes

---

## Weather Data Source

Uses MetService public forecast data by default for New Zealand locations, with Open-Meteo fallback for non-NZ locations or when MetService matching is unavailable.

Optional pinning for a specific NZ forecast page:
```ini
[Weather]
weather_provider = metservice
metservice_location_path = /towns-cities/regions/nelson/locations/nelson
```

**Temperature Units:**
Inherited from `[Weather]` section (see Weather command docs):
```ini
[Weather]
temperature_unit = fahrenheit     # fahrenheit or celsius
wind_speed_unit = mph             # mph, ms, kn
precipitation_unit = inch         # inch or mm
```

---

## Alerts (US Only)

Weather alerts use NOAA API which is **US-only**. For other countries:
- Daily forecasts work worldwide via Open-Meteo
- Weather alerts won't be available
- Lightning detection works worldwide via Blitzortung

---

## Troubleshooting

### Service Not Starting

Check logs:
```bash
tail -f meshcore_bot.log | grep WeatherService
```

Common issues:
- Missing `my_position_lat` or `my_position_lon`
- Invalid coordinates
- `enabled = false`

### No Daily Forecasts

1. **Check alarm time** - Service logs "Next forecast at HH:MM:SS"
2. **Check channel** - Verify `weather_channel` exists
3. **Check position** - Coordinates must be valid

### No Weather Alerts

1. **US only** - NOAA alerts only work in the United States
2. **Check polling** - Service logs "Starting weather alerts polling"
3. **New alerts only** - Only alerts issued since last check are sent

### Lightning Not Working

1. **Check dependencies**: `pip install paho-mqtt`
2. **Check area config** - All 4 coordinates required (min/max lat/lon)
3. **Check MQTT connection** - Service logs "Connected to Blitzortung MQTT"

---

## Advanced

### Sunrise/Sunset Forecasts

When using `weather_alarm = sunrise` or `sunset`:
- Calculates time based on your coordinates
- Updates daily for seasonal changes
- Uses local timezone automatically

### Alert Deduplication

Alerts are tracked by ID to prevent duplicates. The service maintains a list of seen alert IDs and only sends new alerts.

### Lightning Strike Bucketing

Strikes are grouped by:
- **Direction** (heading from your location)
- **Distance** (grouped in 10km buckets)

Example: All strikes 50-60km to the NE are counted as one area.

---

## FAQ

**Q: Do I need an API key?**
A: No. Open-Meteo is free and doesn't require an API key.

**Q: Can I get alerts for other countries?**
A: Daily forecasts work worldwide. Weather alerts are currently US-only (NOAA). If you would like added, let me know.

**Q: How accurate are the forecasts?**
A: Open-Meteo uses data from national weather services (NOAA, DWD, etc.). Accuracy varies by location.

**Q: Can I change temperature units?**
A: Yes, set `temperature_unit` in the `[Weather]` section (used by wx command too).

**Q: Does lightning detection work worldwide?**
A: Yes. Blitzortung has global coverage.

**Q: Why do I need to define a lightning detection area?**
A: To filter strikes. Without an area, you'd get alerts for the entire globe.
