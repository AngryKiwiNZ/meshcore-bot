# Telegram Bridge Service

The Telegram Bridge service posts MeshCore channel messages to Telegram channels or groups via the [Telegram Bot API](https://core.telegram.org/bots/api). This is a **one-way, read-only bridge** — messages only flow from MeshCore to Telegram.

**Features:**
- One-way message flow (MeshCore → Telegram only)
- Multi-channel mapping (map MeshCore channels to Telegram chat IDs)
- Bot API `sendMessage` with optional HTML formatting
- **DMs are NEVER bridged** (hardcoded for privacy)
- Per-chat rate limiting and retries with exponential backoff
- Disabled by default (opt-in)

---

## Quick Start

### 1. Create a Bot and Get Token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts (name and username)
3. Copy the **API token** BotFather returns (e.g. `123456789:ABCdefGHI...`)

### 2. Add Bot to Your Channel

1. Create a Telegram channel or use an existing one
2. Add your bot as an **Administrator** with at least **"Post Messages"** permission
3. Get the chat ID:
   - **Public channels**: Use the channel username (e.g. `@HowlTest`) as the chat ID
   - **Private channels/groups**: Use a numeric ID (e.g. `-1001234567890`). See [Getting the numeric chat ID](#getting-the-numeric-chat-id-private-channels) below.

### Getting the numeric chat ID (private channels)

For private channels or groups, the chat ID is a number (often starting with `-100`). Two ways to get it:

- **Forward a message**: Forward any message **from** the private channel to [@userinfobot](https://t.me/userinfobot) in a private chat. The bot’s reply includes the **Chat ID** (e.g. `-1003715244454`). Use that number in config as `bridge.ChannelName = -1003715244454`.
- **Bot API getUpdates**: Add your bot to the channel as admin, then send a message in the channel. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser; in the JSON, find the update for that message and use `message.chat.id` (e.g. `-1003715244454`).

### 3. Configure Bot

Edit `config.ini`:

```ini
[TelegramBridge]
enabled = true
api_token = YOUR_BOT_TOKEN_FROM_BOTFATHER

# Map MeshCore channel names to Telegram chat IDs
bridge.HowlTest = @HowlTest
# For private: bridge.SomeChannel = -1001234567890
```

You can also set the token via the `TELEGRAM_BOT_TOKEN` environment variable (takes precedence over config).

### 4. Restart Bot

```bash
sudo systemctl restart meshcore-bot
# OR if running manually: python3 meshcore_bot.py
```

### 5. Test

Send a message on the bridged MeshCore channel — it should appear in the Telegram channel.

---

## Configuration

### Config Keys

| Key | Required | Description |
|-----|----------|-------------|
| `enabled` | Yes | `true` to enable the bridge, `false` to disable (default: false) |
| `api_token` | Yes* | Bot token from @BotFather. Can use env var `TELEGRAM_BOT_TOKEN` instead |
| `bridge.<channel>` | At least one | MeshCore channel name → Telegram chat ID (`@channel` or numeric). Use the channel name **without** `#` (e.g. `bridge.HowlTest`). Matching is case-insensitive and ignores a leading `#`. |
| `parse_mode` | No | `HTML` (default), `Markdown`, or `MarkdownV2` |
| `disable_web_page_preview` | No | `true`/`false` — disable link previews (default: false) |
| `max_message_length` | No | 1–4096; truncate longer messages (default: 4096) |
| `filter_profanity` | No | Profanity handling: `drop` (default), `censor`, or `off`. Word list via `better-profanity`; hate symbols (e.g. 卐/卍) are always blocked/censored. |

\* Either `api_token` in config or `TELEGRAM_BOT_TOKEN` in the environment must be set when the bridge is enabled.

### Example

```ini
[TelegramBridge]
enabled = true
api_token = 123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
# parse_mode = HTML
# disable_web_page_preview = false
# max_message_length = 4096

bridge.HowlTest = @HowlTest
bridge.Public = @MyPublicChannel
bridge.emergency = -1001234567890
```

---

## Security & Privacy

### Token Security

- **Do not log or expose the API token.** The service masks it in logs (e.g. first/last 4 chars).
- Prefer storing the token in the `TELEGRAM_BOT_TOKEN` environment variable instead of `config.ini` in shared environments.
- **Rotate the token** in @BotFather if it was ever exposed (e.g. shared in a channel or committed).

### DMs Are Never Bridged

For privacy, **DMs are NEVER bridged to Telegram**. Only channel messages from explicitly configured MeshCore channels are posted. This is hardcoded and cannot be changed via configuration.

---

## Rate Limits

Telegram enforces roughly **1 message per second per chat** and **30 messages per second to different chats**. The service:

- Maintains a per-chat queue with minimum 1 second spacing between sends
- Uses exponential backoff on failures and respects `retry_after` on HTTP 429
- Drops messages after max retries or max queue age (configurable internally)

---

## Message Format

Bridged messages use HTML by default:

- **Sender** is bold: `<b>SenderName</b>: message`
- Optional channel tag: `<i>[ChannelName]</i> <b>Sender</b>: message`
- MeshCore mentions `@[username]` are rendered as `<code>@username</code>` (no tg:// link for mesh users)
- User text is escaped for HTML (`&`, `<`, `>`)
- Messages longer than `max_message_length` (default 4096) are truncated with "…"

---

## Troubleshooting

### Service Not Starting

- Ensure `[TelegramBridge]` exists and `enabled = true`
- Ensure `api_token` (or `TELEGRAM_BOT_TOKEN`) is set when enabled
- Check logs: `grep -i telegram meshcore_bot.log`

### Messages Not Appearing in Telegram

1. **Channel mapping**: The config key is the MeshCore channel name **without** `#` (e.g. `bridge.HowlTest` for channel `#howltest`). Matching is case-insensitive. Verify `bridge.<MeshCoreChannelName>` is set to the correct Telegram chat ID.
2. **Bot permissions**: Bot must be added to the channel/group as Administrator with "Post Messages"
3. **Chat ID**: For public channels use `@channelusername`; for private use numeric ID (e.g. `-100...`) — see [Getting the numeric chat ID](#getting-the-numeric-chat-id-private-channels).
4. **Logs**: Look for send errors or rate-limit messages in `meshcore_bot.log`

### 429 Rate Limit

If you see HTTP 429 responses, the service will re-queue and respect `retry_after`. Reduce message volume or ensure only needed channels are bridged.

---

## FAQ

**Q: Can I bridge messages from Telegram to MeshCore?**  
A: No. This is a one-way bridge (MeshCore → Telegram only).

**Q: Can I bridge DMs?**  
A: No. DMs are never bridged for privacy. This is hardcoded.

**Q: How do I get the numeric chat ID for a private group/channel?**  
A: Forward a message from the group/channel to [@userinfobot](https://t.me/userinfobot) or use the Telegram API (e.g. getUpdates after the bot is added).

**Q: Can I use topics in a supergroup?**  
A: The initial implementation does not set `message_thread_id`. It can be added in a future iteration if you have a mapping from channel/topic to thread ID.

**Q: How do I disable the bridge temporarily?**  
A: Set `enabled = false` in `[TelegramBridge]` and restart the bot.

---

## Implementation Details

- **Base**: `BaseServicePlugin` (`modules/service_plugins/base_service.py`)
- **Event**: Subscribes to `EventType.CHANNEL_MSG_RECV` only
- **HTTP**: `aiohttp` with fallback to `requests` in executor; timeout ~10s
- **Config**: `config.ini` section `[TelegramBridge]`; example in `config.ini.example`
- **Service file**: `modules/service_plugins/telegram_bridge_service.py`
- **Loader**: Auto-discovered; no changes to `service_plugin_loader.py` (loads when section exists and `enabled = true`)

**Dependencies:** Uses existing `aiohttp` and `requests` — no new pip dependencies.
