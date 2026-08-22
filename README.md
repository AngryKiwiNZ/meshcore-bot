# MeshCore Bot

MeshCore Bot is a privacy-conscious Python bot for MeshCore networks. It connects by
serial, BLE, or TCP and provides practical mesh commands, protected web controls,
and optional local or remote conversational assistance.

## Highlights

- Command and service-plugin architecture for weather, alerts, routing, check-ins,
  feeds, repeaters, solar conditions, sports, and network tools.
- Channel controls: select monitored channels, enable or disable DM replies, and
  limit which commands may answer in public channels.
- Emergency-oriented tools: check-ins and roll calls, alert feeds, scheduled
  messages, announcements, and dedicated emergency channels.
- Persistent DM games including Lemonade Stand, Blackjack, and Mastermind.
- Optional LLM conversations with a remote provider and automatic local Ollama
  fallback. Contact profiles, conversation history, and provider credentials stay
  in ignored local data paths and are managed only in the protected console.
- Web Console pages for operational status, contacts, repeaters, games,
  conversations, archives, and access control.
- Privacy-preserving defaults: runtime databases, logs, local configuration,
  LLM profiles, credentials, and backups are excluded from Git.

## Quick start

```bash
git clone <repository-url>
cd meshcore-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp config.ini.example config.ini
python3 meshcore_bot.py
```

Edit `config.ini` before starting. Keep this file local: it is intentionally ignored
because it can contain device addresses, channel keys, access passwords, and API keys.

For a smaller starting configuration, copy `config.ini.minimal-example` instead.

## Conversations and games

LLM support is optional and disabled by default. A remote model can be used for
short conversations, with a local Ollama model as a fallback when enabled. The Web
Console lets operators choose who may converse, whether a profile applies to DMs
only or all eligible messages, and whether channel conversations require a bot
mention. LLM tools are limited to an allowlist of read-only bot commands.

Games are persistent DM sessions. Enable individual games from the protected console
and use `games` to see the available menu.

See [LLM conversations](docs/llm-conversations.md) and the
[command reference](docs/command-reference.md) for details.

## Channel and emergency operations

Configure `monitor_channels` and `respond_to_dms` in `[Channels]`. The optional
`channel_keywords` allowlist keeps high-volume commands DM-only while retaining
essential public commands. Emergency channels can use `checkin`, `rollcall`,
announcements, scheduled messages, and enabled alert feeds.

```ini
[Channels]
monitor_channels = general,emergency
respond_to_dms = true
# channel_keywords = help,ping,checkin,rollcall
```

## Web Console security

Bind the console to `127.0.0.1` unless a reverse proxy and suitable network access
controls are in place. Set unique access passwords and a random session secret in
the local `config.ini`; never put them in example files, commits, issues, or logs.

## Documentation

- [Configuration guide](docs/configuration.md)
- [Command reference](docs/command-reference.md)
- [Web Console](docs/web-viewer.md)
- [Service plugins](docs/service-plugins.md)
- [Docker deployment](docs/docker.md)

## Privacy and publishing

Before publishing, run a secret scan and verify `git status --ignored`. Do not add
`config.ini`, `data/`, database files, logs, backups, or `local/` plugin/config
content. The repository's `.gitignore` protects these paths, but it cannot protect
files that have already been committed in Git history.
