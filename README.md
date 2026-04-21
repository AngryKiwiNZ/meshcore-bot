# MeshCore Bot

A Python bot for MeshCore networks that connects over serial, BLE, or TCP/IP and responds to mesh messages with useful commands, safety check-ins, weather, alerts, and background services.

This project builds on earlier work from:
- https://github.com/agessaman/meshcore-bot
- https://github.com/SpudGunMan/meshing-around

## Highlights

- Connect over `serial`, `ble`, or `tcp`
- Plugin-based command system with built-in commands for weather, safety, routing, solar, sports, feeds, and more
- `checkin` / `rollcall` command for net roll calls, emergency welfare checks, and recent status lookups
- `wx` / `gwx` weather commands using Open-Meteo, with support for default locations, companion location fallback, and forecast options like `tomorrow` and `7d`
- `alert` command with New Zealand MetService CAP RSS support enabled by default
- Background services for scheduled weather, Discord bridging, packet capture, map uploads, earthquakes, and more
- Rate limiting, DM support, monitored channels, logging, and persistent SQLite storage

## Quick Start

### Requirements

- Python 3.7+
- A MeshCore-compatible device
- One of:
  - USB serial access
  - BLE capability
  - TCP/IP access to a MeshCore node

### Install

```bash
git clone <repository-url>
cd meshcore-bot
pip install -r requirements.txt
cp config.ini.example config.ini
```

Edit `config.ini`, then run:

```bash
python3 meshcore_bot.py
```

If you want a smaller starter config for testing core commands only:

```bash
cp config.ini.minimal-example config.ini
```

## What’s New

### Check-ins and Roll Calls

The bot now includes a safety-focused `checkin` command for status reporting and roll calls.

Supported flows:
- `checkin`
- `checkin <status>`
- `checkin list`
- `checkin last <node>`
- `checkin remove`
- `rollcall`

Examples:

```text
checkin
checkin safe at home
checkin need supplies but OK
checkin list
checkin last Jay
rollcall
```

Behavior:
- Saves a timestamped status for the sender
- Shows recent unique check-ins
- Looks up the last known check-in for a user or node
- Automatically expires old check-ins after the configured retention window

Related docs:
- [Command reference](docs/command-reference.md)
- [Check-in API](docs/checkin-api.md)
- [Local plugins](docs/local-plugins.md)

### Weather: `wx` and `gwx`

`wx` now uses the Open-Meteo-backed international weather implementation, so it works well for New Zealand and other non-US locations.

Common usage:

```text
wx Auckland
wx Nelson tomorrow
wx Christchurch 7d
gwx Tokyo
gwx Paris, France
gwx 35.6762,139.6503
```

Highlights:
- Global location support via geocoding
- `tomorrow` forecast option
- `7d` multi-day forecast option
- Default location fallback from `Weather.default_weather_location`
- Companion location fallback when the sender has known coordinates
- Shared unit settings for temperature, wind, and precipitation

Aliases:
- `wx`, `weather`, `wxa`, `wxalert`
- `gwx`, `globalweather`, `gwxa`

Related docs:
- [Command reference](docs/command-reference.md)
- [Weather service](docs/weather-service.md)
- [Configuration guide](docs/configuration.md)

### Alerts via MetService

The `alert` command now defaults to the New Zealand MetService CAP RSS feed:

```ini
[Alert_Command]
enabled = true
provider = metservice
```

Basic usage:

```text
alert
```

Current behavior:
- Pulls from `https://alerts.metservice.com/cap/rss`
- Returns alerts mentioning `Nelson`
- If nothing matches, responds with a message showing that there are currently no Nelson-region alerts and includes the total number of feed alerts for troubleshooting

Note:
- `metservice` is the current default
- `pulsepoint` is still available for legacy US incident workflows

Related docs:
- [Command reference](docs/command-reference.md)
- [Example config](config.ini.example)

## Core Features

### Commands

Examples of available command groups:

- Basic: `test`, `ping`, `help`, `hello`, `cmd`
- Safety and emergency: `checkin`, `rollcall`, `alert`
- Weather and environment: `wx`, `gwx`, `aqi`, `sun`, `moon`, `solar`, `solarforecast`, `hfcond`, `aurora`
- Mesh and network tools: `path`, `prefix`, `stats`, `channels`, `multitest`, `webviewer`
- Fun and utility: `dice`, `roll`, `magic8`, `joke`, `dadjoke`, `hacker`, `catfact`
- Admin or operational tools: `repeater`, `advert`, `feed`, `announcements`, `reload`, `greeter`

For the full command list with examples, see [docs/command-reference.md](docs/command-reference.md).

### Service Plugins

Built-in service plugins include:

- Discord bridge: [docs/discord-bridge.md](docs/discord-bridge.md)
- Packet capture: [docs/packet-capture.md](docs/packet-capture.md)
- Map uploader: [docs/map-uploader.md](docs/map-uploader.md)
- Weather service: [docs/weather-service.md](docs/weather-service.md)
- Earthquake service: [docs/earthquake-service.md](docs/earthquake-service.md)

## Configuration

The bot is configured through `config.ini`.

### Connection

```ini
[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
# ble_device_name = MeshCore
# hostname = 192.168.1.60
# tcp_port = 5000
timeout = 30
```

### Bot

```ini
[Bot]
bot_name = MeshCoreBot
enabled = true
rate_limit_seconds = 10
bot_tx_rate_limit_seconds = 1.0
per_user_rate_limit_seconds = 5
per_user_rate_limit_enabled = true
startup_advert = false
db_path = meshcore_bot.db
```

### Channels

```ini
[Channels]
monitor_channels = general,test,emergency
respond_to_dms = true
# channel_keywords = help,ping,test,hello
```

### Weather

```ini
[Weather]
default_weather_location = Nelson, New Zealand
temperature_unit = celsius
wind_speed_unit = kmh
precipitation_unit = mm
```

### Check-in Command

```ini
[Checkin_Command]
enabled = true
default_status = safe
max_list_entries = 6
retention_hours = 72
recent_window_days = 3
```

### Alert Command

```ini
[Alert_Command]
enabled = true
provider = metservice
max_incident_age_hours = 24
max_distance_km = 20.0
```

### Airplanes Command

```ini
[Airplanes_Command]
enabled = true
api_url = http://api.airplanes.live/v2/
default_location = Nelson, New Zealand
default_radius = 25
max_results = 10
url_timeout = 10
```

If no explicit coordinates, companion location, or bot location are available, the `airplanes` command falls back to `default_location`, then `Weather.default_weather_location`, then `Nelson, New Zealand`. Aircraft responses are displayed in metric units.

For full configuration coverage, see:
- [docs/configuration.md](docs/configuration.md)
- [config.ini.example](config.ini.example)

## Usage

### Run the Bot

```bash
python3 meshcore_bot.py
```

### Production Service

Install as a system service:

```bash
sudo ./install-service.sh
sudo systemctl start meshcore-bot
sudo systemctl status meshcore-bot
```

More detail: [docs/service-installation.md](docs/service-installation.md)

### Docker

```bash
mkdir -p data/{config,databases,logs,backups}
cp config.ini.example data/config/config.ini
docker compose up -d --build
```

More detail: [docs/docker.md](docs/docker.md)

## Hardware Setup

### Serial

```ini
[Connection]
connection_type = serial
serial_port = /dev/ttyUSB0
```

### BLE

```ini
[Connection]
connection_type = ble
ble_device_name = MeshCore
```

### TCP

```ini
[Connection]
connection_type = tcp
hostname = 192.168.1.60
tcp_port = 5000
```

## Troubleshooting

### Common Issues

1. Serial port not found:
   Check the device path and confirm the node is connected.
2. BLE connection problems:
   Verify the device is discoverable and the configured BLE name matches.
3. TCP connection failures:
   Confirm the host, port, and network path to the MeshCore node.
4. No command responses:
   Check `enabled`, monitored channels, DM settings, and rate limits.
5. Weather or alert failures:
   Confirm the bot has internet access and the relevant command is enabled.

### Debug Logging

```ini
[Logging]
log_level = DEBUG
```

## Architecture

The project is organized around a plugin-style structure:

- `modules/`: shared logic, database access, scheduling, utilities, and message handling
- `modules/commands/`: user-facing command plugins
- `modules/service_plugins/`: background services
- `docs/`: feature, configuration, and deployment documentation
- `tests/`: unit, command, and integration tests

## License

This project is licensed under the MIT License.

## Acknowledgments

- [MeshCore Project](https://github.com/meshcore-dev/MeshCore)
- MeshingAround bot work by K7MHI Kelly Keeton
- [meshcore-packet-capture](https://github.com/agessaman/meshcore-packet-capture)
- [meshcore-decoder](https://github.com/michaelhart/meshcore-decoder)
