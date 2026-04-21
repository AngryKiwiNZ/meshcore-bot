# Telemetry Leaderboard

The Telemetry Leaderboard feature tracks extreme network metrics and maintains a "hall of fame" of notable achievements across your mesh network.

## Overview

This feature is inspired by the Telemetry Leaderboard from the [meshing-around](https://github.com/SpudGunMan/meshing-around) project and has been adapted for the MeshCore Bot architecture.

The leaderboard tracks:
- **Battery Levels**: Lowest battery percentage across all nodes
- **Speed Records**: Fastest ground speeds reported by nodes
- **Altitude Records**: Highest altitudes and tallest nodes
- **Temperature**: Coldest and hottest temperatures detected
- **Signal Quality**: Best and worst RF signal strength (dBm)
- **Message Activity**: Most messages sent and most telemetry packets
- **Special Packets**: Admin, tunnel, audio, and simulator packets
- **Device Detection**: WiFi and BLE device counts

## Usage

### Display the Leaderboard

```
telemetry
```

This displays all current extreme metric records with node names and values.

**Example output:**
```
📊 **Telemetry Leaderboard** 📊

🪫 Low Battery: 5.0% - TestNode-A
🚓 Speed: 42.5km/h - TestNode-B
⛰️ Altitude: 1245.0m - TestNode-C
🥶 Coldest: -12.3°C - TestNode-D
🔥 Hottest: 35.7°C - TestNode-E
📶 Best RF: -95.0dBm - TestNode-F
📉 Worst RF: -125.0dBm - TestNode-G
💬 Most Messages: 2msgs - TestNode-A
📊 Most Telemetry: 5packets - TestNode-C

📡 **Special Packets** 📡
  Admin: TestNode-D
  Tunnel: TestNode-E
  Audio: TestNode-F
```

### Reset the Leaderboard

```
telemetry reset
```

This clears all records and starts fresh. **Admin only**.

## Configuration

Add the following to your `config.ini`:

```ini
[Telemetry_Command]
# Enable or disable the telemetry leaderboard command
enabled = true

# Log when new records are broken
# true: Log info messages when new extreme metrics are detected
# false: Only display stats, don't log record breaks
log_records = true
```

## Data Persistence

The leaderboard automatically:
- **Loads** existing records from `data/telemetry/leaderboard.pkl` on bot startup
- **Saves** all records to the pickle file on bot shutdown and periodically during operation
- **Persists** records across bot restarts

## Architecture

### Key Files

- [modules/telemetry_leaderboard.py](../modules/telemetry_leaderboard.py) - Core leaderboard tracking and storage
- [modules/commands/telemetry_command.py](../modules/commands/telemetry_command.py) - Command handler for user interaction
- [modules/core.py](../modules/core.py) - Integration with the bot's event system

### Integration Points

The telemetry leaderboard integrates with:
1. **Core Bot**: Initialized at startup and cleaned up on shutdown
2. **Message Handler**: Tracks messages and telemetry packets from nodes
3. **Command Manager**: Provides the `telemetry` command to users

### Metrics Structure

Each metric tracks:
- `nodeID`: The ID of the node holding the record
- `nodeName`: Human-readable name of the node
- `value`: The metric value
- `timestamp`: When the record was set

## Future Enhancements

Potential improvements for future versions:
- Integration with actual device telemetry packets (battery level, GPS coordinates)
- Historical tracking and trending
- Per-channel leaderboards
- Periodic leaderboard announcements
- Web viewer dashboard display
- Export functionality (CSV, JSON)
- Custom metric definitions

## Differences from Meshtastic Version

The meshcore-bot version of this feature differs from the original Meshtastic-based implementation because MeshCore has a different packet structure:

- **Packet Types**: Uses MeshCore's packet types (REQ, RESPONSE, TXT_MSG, etc.) instead of Meshtastic's TELEMETRY_APP and POSITION_APP
- **Data Availability**: Currently tracks message counts and special packets; direct telemetry data (battery, temperature, GPS) would require custom packet parsing
- **Simplification**: Focus on message-based metrics rather than full device telemetry (can be extended in the future)

For full MeshCore telemetry integration, device telemetry would need to be sent via custom packets or the Path Command data.

## See Also

- [Stats Command](../docs/command-reference.md#stats) - For general bot statistics
- Original implementation: [SpudGunMan/meshing-around](https://github.com/SpudGunMan/meshing-around)
