"""Local conversational LLM support for authorised MeshCore direct messages."""

import asyncio
import hashlib
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from .db_manager import ensure_llm_schema


class LLMService:
    """Queue conversations for a remote provider with local Ollama fallback."""

    def __init__(self, bot):
        self.bot = bot
        self.logger = bot.logger
        self.config = bot.config
        self.enabled = self.config.getboolean("LLM", "enabled", fallback=False)
        self.base_url = self.config.get(
            "LLM", "ollama_url", fallback="http://127.0.0.1:11434"
        ).rstrip("/")
        self.model = self.config.get("LLM", "model", fallback="gemma3:1b")
        self.deepseek_base_url = self.config.get(
            "LLM", "deepseek_base_url", fallback="https://api.deepseek.com"
        ).rstrip("/")
        self.deepseek_model = self.config.get(
            "LLM", "deepseek_model", fallback="deepseek-v4-flash"
        )
        self.deepseek_timeout = self.config.getfloat(
            "LLM", "deepseek_timeout_seconds", fallback=30.0
        )
        self.max_delay = self.config.getfloat("LLM", "max_reply_delay_seconds", fallback=18.0)
        self.session_timeout = self.config.getint(
            "LLM", "conversation_session_timeout_seconds", fallback=1800
        )
        configured_bot_name = self.config.get(
            "Bot", "bot_name", fallback="MeshCoreBot"
        ).strip() or "MeshCoreBot"
        aliases = self.config.get(
            "LLM", "mention_aliases", fallback=f"@{configured_bot_name}"
        )
        self.mention_aliases = [alias.strip() for alias in aliases.split(",") if alias.strip()]
        self.operator_name = self.config.get(
            "LLM", "operator_name", fallback="the radio owner"
        ).strip() or "the radio owner"
        tool_commands = self.config.get(
            "LLM",
            "tool_commands",
            fallback=(
                "wx,aqi,airplanes,hfcond,moon,satpass,solar,aurora,"
                "sun,solarforecast,sports"
            ),
        )
        self.tool_commands = {
            name.strip().lower() for name in tool_commands.split(",") if name.strip()
        }
        self.timeout = self.config.getfloat("LLM", "request_timeout_seconds", fallback=120.0)
        self.history_messages = self.config.getint("LLM", "history_messages", fallback=12)
        self.max_output_tokens = self.config.getint("LLM", "max_output_tokens", fallback=90)
        self.max_chunks = self.config.getint("LLM", "max_reply_chunks", fallback=2)
        self.chunk_bytes = self.config.getint("LLM", "chunk_bytes", fallback=125)
        self.max_queue = max(1, self.config.getint("LLM", "max_queue_size", fallback=3))
        self.retention_days = max(
            1, self.config.getint("LLM", "history_retention_days", fallback=30)
        )
        self.max_profile_chars = max(
            500, self.config.getint("LLM", "max_profile_chars", fallback=6000)
        )
        profile_dir = self.config.get("LLM", "profiles_dir", fallback="data/llm_profiles")
        self.profile_dir = (bot.bot_root / profile_dir).resolve()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        runtime_file = self.config.get(
            "LLM", "runtime_status_file", fallback="data/llm_runtime_status.json"
        )
        self.runtime_status_path = (bot.bot_root / runtime_file).resolve()
        self.runtime_status_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(bot.db_manager.db_path)
        self._pending: Dict[str, asyncio.Task] = {}
        self._pending_initial_delay: Dict[str, bool] = {}
        self._waiting_snapshots: Dict[str, List[dict]] = {}
        self._inference_lock = asyncio.Lock()
        self._queued_requests = 0
        self._active_conversation: Optional[str] = None
        self._last_duration_seconds: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_provider: Optional[str] = None
        self._fallback_count = 0
        self._last_completed_at: Optional[float] = None
        self._completed_requests = 0
        self._rejected_requests = 0
        self._last_purge_at = 0.0
        self._init_schema()
        self._purge_old_history(force=True)
        self._write_runtime_status()

    async def preload_model(self) -> bool:
        """Load the configured model without generating text."""
        if not self.enabled or not self.config.getboolean(
            "LLM", "preload_model", fallback=False
        ):
            return False
        payload = {
            "model": self.model,
            "prompt": "",
            "stream": False,
            "keep_alive": self._keep_alive_value(),
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with self._inference_lock:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        f"{self.base_url}/api/generate", json=payload
                    ) as response:
                        response.raise_for_status()
                        await response.json()
            self.logger.info("LLM model preloaded and ready: %s", self.model)
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "Unable to preload LLM model (%s): %s",
                type(exc).__name__,
                str(exc) or "no detail",
            )
            return False

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            ensure_llm_schema(conn)

    def _setting_bool(self, key: str, fallback: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM llm_settings WHERE setting_key = ?", (key,)
            ).fetchone()
        if not row:
            return fallback
        return str(row["setting_value"]).strip().lower() in {"1", "true", "yes", "on"}

    def _setting_text(self, key: str, fallback: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM llm_settings WHERE setting_key = ?", (key,)
            ).fetchone()
        return str(row["setting_value"]).strip() if row else fallback

    def _bot_model_identity(self) -> str:
        provider = self._setting_text(
            "provider", self.config.get("LLM", "provider", fallback="ollama")
        ).lower()
        if provider == "deepseek":
            model = self._setting_text("deepseek_model", self.deepseek_model)
            model_label = {
                "deepseek-v4-flash": "DeepSeek V4 Flash",
                "deepseek-v4-pro": "DeepSeek V4 Pro",
            }.get(model, "DeepSeek")
            return f"{model_label}, with Gemma 3 1B as my offline fallback"
        return "the Gemma 3 1B LLM model"

    def _deepseek_api_key(self) -> str:
        environment_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if environment_key:
            return environment_key
        configured = self.config.get(
            "LLM", "deepseek_api_key_file", fallback="data/llm_deepseek_api_key"
        )
        root = self.bot.bot_root.resolve()
        path = (root / configured).resolve()
        if path != root and root not in path.parents:
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _purge_old_history(self, force: bool = False) -> int:
        now = time.time()
        if not force and now - self._last_purge_at < 21600:
            return 0
        retention_days = int(
            self._setting_float("history_retention_days", self.retention_days)
        )
        retention_days = min(365, max(1, retention_days))
        cutoff = now - (retention_days * 86400)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM llm_messages WHERE created_at < ?", (cutoff,)
            )
        self._last_purge_at = now
        if cursor.rowcount:
            self.logger.info(
                "Purged %d LLM history messages older than %d days",
                cursor.rowcount,
                retention_days,
            )
        return cursor.rowcount

    def _write_runtime_status(self) -> None:
        status = {
            "pid": os.getpid(),
            "updated_at": time.time(),
            "enabled": self.enabled and self._setting_bool("globally_enabled", True),
            "queue_depth": self._queued_requests,
            "queue_limit": self.max_queue,
            "active_conversation": self._active_conversation,
            "last_duration_seconds": self._last_duration_seconds,
            "last_error": self._last_error,
            "last_provider": self._last_provider,
            "fallback_count": self._fallback_count,
            "last_completed_at": self._last_completed_at,
            "completed_requests": self._completed_requests,
            "rejected_requests": self._rejected_requests,
        }
        temporary = self.runtime_status_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(status), encoding="utf-8")
            temporary.replace(self.runtime_status_path)
        except OSError as exc:
            self.logger.debug("Unable to write LLM runtime status: %s", exc)

    @staticmethod
    def profile_filename(public_key: str, contact_name: str = "contact") -> str:
        safe_name = re.sub(r"[^a-z0-9]+", "-", contact_name.lower()).strip("-") or "contact"
        key_tag = hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:10]
        return f"{safe_name}-{key_tag}.md"

    def _policy(self, public_key: str) -> Optional[sqlite3.Row]:
        if not public_key:
            return None
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM llm_contacts WHERE public_key = ? AND enabled = 1",
                (public_key,),
            ).fetchone()

    def _save_message(
        self, public_key: str, role: str, content: str, message_timestamp: Optional[float] = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO llm_messages
                       (public_key, role, content, message_timestamp, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (public_key, role, content, message_timestamp, time.time()),
            )

    def _history(self, public_key: str) -> List[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT role, content, message_timestamp, created_at FROM llm_messages
                   WHERE public_key = ? ORDER BY id DESC LIMIT ?""",
                (public_key, max(2, self.history_messages)),
            ).fetchall()
        history = []
        today = datetime.now().astimezone().date()
        for row in reversed(rows):
            timestamp = row["message_timestamp"] or row["created_at"]
            sent_at = datetime.fromtimestamp(timestamp).astimezone()
            content = row["content"]
            if row["role"] == "assistant":
                # Do not teach the model timestamp decorations that may have
                # escaped from an earlier response.
                content = self._strip_timestamp_prefix(content)
            if sent_at.date() != today:
                # Date-only metadata is enough to distinguish an older exchange.
                # Current-day messages remain completely natural.
                content = (
                    f"[Earlier conversation on {sent_at:%Y-%m-%d}; "
                    f"private timing metadata] {content}"
                )
            history.append(
                {"role": row["role"], "content": content}
            )
        return history

    def _setting_float(self, key: str, fallback: float) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM llm_settings WHERE setting_key = ?", (key,)
            ).fetchone()
        try:
            return float(row["setting_value"]) if row else fallback
        except (TypeError, ValueError):
            return fallback

    def _keep_alive_value(self):
        """Return durations as text and numeric Ollama sentinel values as numbers."""
        value = self.config.get("LLM", "keep_alive", fallback="5m").strip()
        try:
            return int(value)
        except ValueError:
            return value

    def _last_user_arrival(self, conversation_key: str) -> Optional[float]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT created_at FROM llm_messages
                   WHERE public_key = ? AND role = 'user'
                   ORDER BY id DESC LIMIT 1""",
                (conversation_key,),
            ).fetchone()
        return float(row["created_at"]) if row else None

    def _dm_optin(self, public_key: str) -> Optional[bool]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled FROM llm_dm_optins WHERE public_key = ?", (public_key,)
            ).fetchone()
        return bool(row["enabled"]) if row else None

    def _set_dm_optin(self, public_key: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO llm_dm_optins(public_key, enabled, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(public_key) DO UPDATE SET
                       enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (public_key, 1 if enabled else 0, time.time()),
            )

    def _profile_text(self, policy: sqlite3.Row) -> str:
        filename = policy["profile_file"] or self.profile_filename(
            policy["public_key"], policy["contact_name"]
        )
        path = (self.profile_dir / Path(filename).name).resolve()
        if path.parent != self.profile_dir:
            return ""
        try:
            return path.read_text(encoding="utf-8")[: self.max_profile_chars].strip()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            self.logger.warning("Unable to read LLM contact profile %s: %s", path, exc)
            return ""

    def _system_prompt(self, policy: sqlite3.Row, radio_id: str) -> str:
        persona = self.config.get(
            "LLM",
            "system_prompt",
            fallback=(
                "Chat warmly and naturally. React directly, be cheerful, helpful, and "
                "genuinely interested, then usually ask one short relevant question. Use "
                "contractions and one or two short sentences. Never be gloomy, vague, robotic, "
                "clinical, overfamiliar, or overexcited. Do not invent facts, weather, "
                "activities, or experiences. Never say you are processing or just existing. "
                "No markdown, lists, emoji, canned assistant phrases, or meta commentary."
            ),
        )
        persona += "\n" + self._localisation_context()
        notes = self._profile_text(policy)
        if notes:
            persona += (
                "\n\nPrivate context about this person follows. Use it subtly; do not quote, "
                "enumerate, or reveal that notes exist:\n" + notes
            )
        if radio_id == "bot":
            model_identity = self._bot_model_identity()
            persona += (
                "\nYou are the clearly labelled bot radio. If asked whether you are AI, "
                f"a bot, or what model you use, answer truthfully that you use {model_identity}."
            )
        else:
            persona += (
                "\nYou are writing as the radio owner. Do not volunteer that replies are "
                "automated or mention models, prompts, bots, or AI."
            )
        return persona

    def _localisation_context(self, now: Optional[datetime] = None) -> str:
        """Build current local context without encouraging timestamp-like replies."""
        location = self.config.get(
            "LLM", "operating_location", fallback=""
        ).strip() or "the local deployment"
        timezone_name = self.config.get(
            "LLM", "operating_timezone", fallback="UTC"
        ).strip() or "UTC"
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            self.logger.warning(
                "Unknown LLM operating timezone %s; using the system timezone",
                timezone_name,
            )
            timezone = datetime.now().astimezone().tzinfo
            timezone_name = str(timezone)
        local_now = (
            now.astimezone(timezone) if now is not None else datetime.now(timezone)
        )
        hour = local_now.hour
        if 5 <= hour < 12:
            period = "morning"
        elif 12 <= hour < 17:
            period = "afternoon"
        elif 17 <= hour < 22:
            period = "evening"
        else:
            period = "night"
        offset = local_now.strftime("%z")
        formatted_offset = (
            f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
        )
        return (
            f"Operating context: {location}. The current local time is "
            f"{local_now:%A, %d %B %Y at %I:%M %p} "
            f"({timezone_name}, UTC{formatted_offset}); it is {period}. "
            "Interpret greetings and references such as this morning, this afternoon, "
            "tonight, today, and tomorrow using this local context. Never contradict the "
            "stated local time of day. Use this silently: do not prefix replies with a date "
            "or time, and only state timing details when explicitly asked."
        )

    def _strip_mention(self, content: str) -> Optional[str]:
        for alias in self.mention_aliases:
            # MeshCore clients encode a selected contact mention as
            # @[ContactName], while manually typed mentions arrive as
            # @ContactName. Accept both representations case-insensitively.
            if alias.startswith("@"):
                name = alias[1:]
                mention_pattern = (
                    rf"@(?:\[{re.escape(name)}\]|{re.escape(name)})"
                )
            else:
                mention_pattern = re.escape(alias)
            match = re.search(
                rf"(?<![\w@]){mention_pattern}(?![\w])",
                content,
                flags=re.IGNORECASE,
            )
            if match:
                return (content[: match.start()] + content[match.end() :]).strip(" ,:-")
        return None

    def _looks_like_python_command(
        self,
        content: str,
        ignored_commands: Optional[set] = None,
        message=None,
    ) -> bool:
        ignored = {
            str(command_name).strip().lower()
            for command_name in (ignored_commands or set())
        }
        manager = self.bot.command_manager
        detector = (
            getattr(manager, "is_command_trigger", None)
            if hasattr(type(manager), "is_command_trigger")
            else None
        )
        if detector:
            return bool(
                detector(content, ignored_commands=ignored, message=message)
            )
        first = content.strip().split(maxsplit=1)[0].lower() if content.strip() else ""
        if not first:
            return False
        for registered_name, command in self.bot.command_manager.commands.items():
            command_names = {
                str(registered_name).strip().lower(),
                str(getattr(command, "name", "")).strip().lower(),
            }
            if ignored.intersection(command_names):
                continue
            keywords = getattr(command, "keywords", []) or []
            if first == getattr(command, "name", "").lower() or first in {
                str(keyword).lower() for keyword in keywords
            }:
                return True
        return False

    @staticmethod
    def _looks_like_tool_request(content: str) -> bool:
        """Cheap prefilter that avoids a second API call for ordinary conversation."""
        return bool(
            re.search(
                r"\b(?:weather|forecast|forcast|temperature|rain|wind|air\s+quality|"
                r"pollution|aircraft|airplane|aeroplane|planes?\s+overhead|"
                r"hf\s+(?:bands?|conditions?)|radio\s+conditions?|moon|"
                r"satellite\s+pass|solar\s+(?:conditions?|forecast|activity)|"
                r"aurora|sunrise|sunset|sports?|score|fixture)\b",
                content,
                flags=re.IGNORECASE,
            )
        )

    def _default_weather_request(self, content: str) -> Optional[str]:
        """Recognise a bare natural weather question without an API classifier."""
        text = re.sub(r"[^\w'\s-]", " ", content.lower())
        text = re.sub(r"\s+", " ", text).strip()
        if not re.search(r"\b(?:weather|forecast|forcast)\b", text):
            return None
        tokens = text.replace("what's", "what is").replace("how's", "how is").split()
        filler = {
            "what", "is", "the", "weather", "forecast", "forcast", "bot",
            "please", "can", "could", "would", "you", "tell", "give", "show",
            "check", "me", "us", "a", "get", "i", "we", "like", "today",
            "now", "looking", "look", "like", "there",
        }
        qualifiers = [token for token in tokens if token in {"tomorrow"}]
        remaining = [
            token for token in tokens
            if token not in filler and token not in {"tomorrow"}
        ]
        if remaining:
            return None
        request = "wx" + (" tomorrow" if qualifiers else "")
        return self._normalise_selected_command(request)

    def _available_tool_commands(self) -> Dict[str, object]:
        commands = getattr(self.bot.command_manager, "commands", {}) or {}
        return {
            name: command
            for name, command in commands.items()
            if str(name).lower() in self.tool_commands
        }

    @staticmethod
    def _parse_tool_command(data: dict, allowed: set) -> Optional[str]:
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        calls = message.get("tool_calls") or []
        if not calls:
            return None
        function = calls[0].get("function", {})
        if function.get("name") != "run_meshcore_command":
            return None
        try:
            arguments = json.loads(function.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError):
            return None
        command = str(arguments.get("command", "")).strip().lower()
        command_arguments = str(arguments.get("arguments", "")).strip()
        if command not in allowed or len(command_arguments) > 100:
            return None
        if command_arguments and not re.fullmatch(
            r"[\w\s,.'()/#:+-]+", command_arguments, flags=re.UNICODE
        ):
            return None
        return f"{command} {command_arguments}".strip()

    def _normalise_selected_command(self, selected: str) -> str:
        """Apply command-specific defaults after validating an LLM tool call."""
        command, _, arguments = selected.partition(" ")
        if command != "wx":
            return selected
        weather_qualifiers = {
            "tomorrow", "2", "3", "4", "5", "6", "7", "7day", "7-day"
        }
        argument_parts = arguments.strip().split()
        if not argument_parts or all(
            part.lower() in weather_qualifiers for part in argument_parts
        ):
            default_location = self.config.get(
                "Weather",
                "default_weather_location",
                fallback="",
            ).strip()
            if default_location:
                arguments = " ".join([default_location, *argument_parts])
        return f"{command} {arguments}".strip()

    async def _select_natural_command(self, content: str) -> Optional[str]:
        """Let DeepSeek map a natural request to an approved existing command."""
        if not self._looks_like_tool_request(content):
            return None
        available = self._available_tool_commands()
        deterministic_weather = self._default_weather_request(content)
        if deterministic_weather and "wx" in available:
            self.logger.info(
                "Natural weather request selected default command: %s",
                deterministic_weather,
            )
            return deterministic_weather
        provider = self._setting_text(
            "provider", self.config.get("LLM", "provider", fallback="ollama")
        ).lower()
        api_key = self._deepseek_api_key()
        if provider != "deepseek" or not api_key or not available:
            return None

        catalogue = []
        for name, command in sorted(available.items()):
            description = str(
                getattr(command, "short_description", "")
                or getattr(command, "description", "")
                or f"Run the {name} bot command"
            ).strip()
            usage = str(getattr(command, "usage", "") or "").strip()
            catalogue.append(
                f"{name}: {description[:180]}"
                + (f"; usage: {usage[:100]}" if usage else "")
            )
        allowed = set(available)
        payload = {
            "model": self._setting_text("deepseek_model", self.deepseek_model),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Select an existing MeshCore bot command only when the user clearly "
                        "asks for that command's live data or capability. Do not select a "
                        "command merely because a topic is mentioned conversationally. "
                        "Extract only arguments supplied or clearly implied by the user. "
                        "For weather with no location, ask the user for a location unless "
                        "a local default is configured. "
                        "Available commands:\n"
                        + "\n".join(catalogue)
                    ),
                },
                {"role": "user", "content": content},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "run_meshcore_command",
                        "description": (
                            "Run one approved read-only MeshCore information command"
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "command": {
                                    "type": "string",
                                    "enum": sorted(allowed),
                                },
                                "arguments": {
                                    "type": "string",
                                    "description": (
                                        "Only the command arguments, without the command name"
                                    ),
                                },
                            },
                            "required": ["command", "arguments"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            "tool_choice": "auto",
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": 100,
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.deepseek_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.deepseek_base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "Natural command selection failed (%s): %s",
                type(exc).__name__,
                str(exc) or "no detail",
            )
            return None
        selected = self._parse_tool_command(data, allowed)
        if selected:
            selected = self._normalise_selected_command(selected)
        if selected and self._looks_like_python_command(selected):
            self.logger.info("LLM selected existing bot command: %s", selected)
            return selected
        return None

    async def handle_message(self, message) -> bool:
        """Route conversational DMs and explicitly mentioned channel messages."""
        if not self.enabled:
            return False
        if not self._setting_bool("globally_enabled", True):
            return False
        self._purge_old_history()
        radio_id = "bot"
        prompt = message.content
        policy = None
        profile_public_key = None
        if message.is_dm:
            public_key = (message.sender_pubkey or "").strip()
            policy = self._policy(public_key)
            if policy:
                radio_id = policy["radio_id"]
            mode = policy["interaction_mode"] if policy else "ai_command"
            if radio_id == "dm":
                if not policy or mode != "automatic":
                    return False
                # The second companion connection is intentionally fail-closed until
                # its dedicated radio manager is present.
                if not getattr(self.bot, "dm_meshcore", None):
                    self.logger.warning(
                        "Authorised personal-radio DM received, but DM radio is unavailable"
                    )
                    return True
            else:
                # The public Bot radio is conversational by default. Contact
                # policies may add private profile context, but are not an
                # access-control gate. The cloned Personal radio remains
                # explicitly authorised and fail-closed above.
                radio_id = "bot"
            if radio_id == "bot" and self._looks_like_python_command(
                prompt, ignored_commands={"hello"}, message=message
            ):
                message.content = prompt
                return False
            if radio_id == "bot":
                selected_command = await self._select_natural_command(prompt)
                if selected_command:
                    message.content = selected_command
                    return False
            conversation_key = f"{radio_id}:dm:{public_key}"
        else:
            stripped = self._strip_mention(prompt)
            if stripped is None:
                return False
            # Greetings are conversational. Keep precise utility commands such
            # as wx/ping in the normal Python command router.
            if self._looks_like_python_command(
                stripped, ignored_commands={"hello"}, message=message
            ):
                message.content = stripped
                return False
            selected_command = await self._select_natural_command(stripped)
            if selected_command:
                message.content = selected_command
                routing_info = dict(message.routing_info or {})
                routing_info["llm_mentioned_tool_command"] = selected_command.partition(" ")[0]
                message.routing_info = routing_info
                return False
            prompt = stripped
            conversation_key = f"bot:channel:{message.channel}"
            public_key = conversation_key
            sender_public_key = (message.sender_pubkey or "").strip()
            profile_policy = self._policy(sender_public_key) if sender_public_key else None
            if profile_policy and profile_policy["profile_scope"] == "all":
                policy = profile_policy
                profile_public_key = sender_public_key
            else:
                policy = {
                    "public_key": conversation_key,
                    "contact_name": message.channel or "channel",
                    "profile_file": None,
                }
            prompt = f"{message.sender_id or 'Channel user'}: {prompt}"

        if not prompt:
            return True
        prior_arrival = self._last_user_arrival(conversation_key)
        new_session = prior_arrival is None or time.time() - prior_arrival > self.session_timeout
        try:
            sender_timestamp = float(message.timestamp) if message.timestamp else time.time()
        except (TypeError, ValueError):
            sender_timestamp = time.time()
        self._save_message(conversation_key, "user", prompt, sender_timestamp)
        old_task = self._pending.get(conversation_key)
        if old_task and not old_task.done():
            # Keep one worker per conversation. During its initial human delay,
            # subsequent messages become part of the same reply. Once generation
            # has started, they become one bundled follow-up after it completes.
            self._waiting_snapshots.setdefault(conversation_key, []).append({
                "public_key": conversation_key,
                "sender_id": message.sender_id,
                "message": replace(message, content=prompt),
                "radio_id": radio_id,
                "profile_public_key": profile_public_key,
                "new_session": False,
            })
            return True
        snapshot = {
            "public_key": conversation_key,
            "sender_id": message.sender_id,
            "message": replace(message, content=prompt),
            "radio_id": radio_id,
            "profile_public_key": profile_public_key,
            "new_session": new_session,
        }
        task = asyncio.create_task(self._conversation_worker(snapshot))
        self._pending[conversation_key] = task
        self._pending_initial_delay[conversation_key] = new_session
        task.add_done_callback(
            lambda done, key=conversation_key: self._task_finished(key, done)
        )
        return True

    def _task_finished(self, public_key: str, task: asyncio.Task) -> None:
        if self._pending.get(public_key) is task:
            self._pending.pop(public_key, None)
            self._pending_initial_delay.pop(public_key, None)
            self._waiting_snapshots.pop(public_key, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.logger.error("LLM reply task failed for %s: %s", public_key[:12], exc)

    async def _conversation_worker(self, snapshot: dict) -> None:
        """Reply serially, bundling messages according to the current phase."""
        max_delay = self._setting_float("max_initial_delay_seconds", self.max_delay)
        if snapshot["new_session"] and max_delay > 0:
            await asyncio.sleep(random.uniform(0.0, max_delay))
        conversation_key = snapshot["public_key"]
        while True:
            # Anything received during the initial delay belongs to this reply.
            waiting = self._waiting_snapshots.pop(conversation_key, [])
            if waiting:
                snapshot = waiting[-1]
            self._pending_initial_delay[conversation_key] = False
            await self._reply_once(snapshot)
            # Messages received while Ollama generated or the radio transmitted
            # get one follow-up reply. Multiple messages form a single bundle.
            waiting = self._waiting_snapshots.pop(conversation_key, [])
            if not waiting:
                return
            snapshot = dict(waiting[-1])
            snapshot["unanswered_prompts"] = [
                item["message"].content for item in waiting
            ]

    async def _reply_once(self, snapshot: dict) -> None:
        if snapshot["radio_id"] == "dm":
            raw_key = snapshot["public_key"].split(":", 2)[-1]
            policy = self._policy(raw_key)
            if not policy:
                return
        else:
            profile_key = snapshot.get("profile_public_key")
            policy = self._policy(profile_key) if profile_key else None
            if not policy or policy["profile_scope"] != "all":
                policy = {
                    "public_key": snapshot["public_key"],
                    "contact_name": snapshot["sender_id"] or "contact",
                    "profile_file": None,
                }
        reply = await self._queued_generate(
            policy,
            snapshot["public_key"],
            snapshot["radio_id"],
            snapshot.get("unanswered_prompts"),
        )
        if not reply:
            return
        chunks = self.chunk_reply(reply)
        if not chunks:
            return
        success = await self.bot.command_manager.send_response_chunked(
            snapshot["message"], chunks, skip_user_rate_limit_first=True
        )
        if success:
            self._save_message(snapshot["public_key"], "assistant", reply)

    async def _queued_generate(
        self,
        policy,
        conversation_key: str,
        radio_id: str,
        unanswered_prompts: Optional[List[str]] = None,
    ) -> str:
        """Run at most one provider request at once and bound queued work."""
        if not self._setting_bool("globally_enabled", True):
            return ""
        if self._queued_requests >= self.max_queue:
            self._rejected_requests += 1
            self._last_error = "Inference queue full"
            self._write_runtime_status()
            self.logger.warning("LLM inference queue full; dropping %s", conversation_key[:24])
            return ""
        self._queued_requests += 1
        self._write_runtime_status()
        try:
            async with self._inference_lock:
                if not self._setting_bool("globally_enabled", True):
                    return ""
                self._active_conversation = conversation_key
                self._write_runtime_status()
                started = time.monotonic()
                try:
                    self._last_error = None
                    reply = await self._generate(
                        policy,
                        conversation_key,
                        radio_id,
                        unanswered_prompts,
                    )
                    if not reply and not self._last_error:
                        self._last_error = "No LLM provider returned a reply"
                    return reply
                except asyncio.CancelledError:
                    self._last_error = "Request cancelled"
                    raise
                finally:
                    self._last_duration_seconds = round(time.monotonic() - started, 2)
                    self._last_completed_at = time.time()
                    self._completed_requests += 1
                    self._active_conversation = None
        finally:
            self._queued_requests = max(0, self._queued_requests - 1)
            self._write_runtime_status()

    async def _generate(
        self,
        policy,
        conversation_key: str,
        radio_id: str,
        unanswered_prompts: Optional[List[str]] = None,
    ) -> str:
        latest = self._history(conversation_key)
        if unanswered_prompts:
            bundle = unanswered_prompts[0]
            for prompt in unanswered_prompts[1:]:
                bundle += f"\nThey then added: {prompt}"
            latest.append({
                "role": "user",
                "content": (
                    "These messages arrived while your previous reply was being composed "
                    "and still need a response. Address all of them naturally:\n" + bundle
                ),
            })
        last_user = next(
            (item["content"] for item in reversed(latest) if item["role"] == "user"), ""
        )
        asks_identity = bool(
            re.search(
                r"\b(?:are you|is this|am i (?:talking|chatting) (?:to|with)|"
                r"who am i (?:talking|chatting) (?:to|with)|are these replies)\b"
                r".{0,40}\b(?:ai|artificial intelligence|bot|chatbot|llm|automated|human|real)\b"
                r"|\bwhat (?:ai|model|llm|bot) are you\b"
                r"|\b(?:what|who) are you\b"
                r"|\b(?:human or ai|real person|automated repl(?:y|ies))\b",
                last_user,
                flags=re.IGNORECASE,
            )
        )
        if asks_identity and radio_id == "bot":
            return f"Yes. I'm the bot radio, normally using {self._bot_model_identity()}."
        if asks_identity and radio_id == "dm":
            return f"It's {self.operator_name} here."

        provider = self._setting_text(
            "provider", self.config.get("LLM", "provider", fallback="ollama")
        ).lower()
        if provider == "deepseek":
            reply = await self._generate_deepseek(policy, radio_id, latest)
            if reply:
                self._last_provider = "deepseek"
                return reply
            if self._setting_bool(
                "fallback_to_ollama",
                self.config.getboolean("LLM", "fallback_to_ollama", fallback=True),
            ):
                self._fallback_count += 1
                self._last_error = "DeepSeek unavailable; Gemma fallback used"
                self.logger.warning("Falling back to local Gemma for this LLM reply")
                reply = await self._generate_ollama(policy, radio_id, latest)
                if reply:
                    self._last_provider = "ollama-fallback"
                return reply
            return ""

        reply = await self._generate_ollama(policy, radio_id, latest)
        if reply:
            self._last_provider = "ollama"
        return reply

    async def _generate_deepseek(
        self, policy, radio_id: str, latest: List[dict]
    ) -> str:
        api_key = self._deepseek_api_key()
        if not api_key:
            self.logger.warning("DeepSeek is selected but no API key is configured")
            return ""
        messages = [
            {"role": "system", "content": self._system_prompt(policy, radio_id)},
            *self._normalised_history(latest),
        ]
        payload = {
            "model": self._setting_text("deepseek_model", self.deepseek_model),
            "messages": messages,
            "thinking": {"type": "disabled"},
            "stream": False,
            "max_tokens": self.config.getint(
                "LLM", "deepseek_max_output_tokens", fallback=80
            ),
            "temperature": self.config.getfloat(
                "LLM", "deepseek_temperature", fallback=0.8
            ),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.deepseek_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.deepseek_base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as response:
                    response.raise_for_status()
                    data = await response.json()
            choices = data.get("choices") or []
            text = choices[0].get("message", {}).get("content", "") if choices else ""
            return self._sanitize_reply(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "DeepSeek request failed (%s): %s",
                type(exc).__name__,
                str(exc) or "no detail",
            )
            return ""

    async def _generate_ollama(
        self, policy, radio_id: str, latest: List[dict]
    ) -> str:
        model_messages = self._model_messages(policy, radio_id, latest)
        payload = {
            "model": self.model,
            "stream": False,
            "messages": model_messages,
            "options": {
                "temperature": self.config.getfloat("LLM", "temperature", fallback=0.75),
                "top_p": self.config.getfloat("LLM", "top_p", fallback=0.9),
                "top_k": self.config.getint("LLM", "top_k", fallback=40),
                "repeat_penalty": self.config.getfloat(
                    "LLM", "repeat_penalty", fallback=1.08
                ),
                "num_predict": self.max_output_tokens,
                "num_ctx": self.config.getint("LLM", "context_tokens", fallback=1536),
            },
            "keep_alive": self._keep_alive_value(),
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{self.base_url}/api/chat", json=payload) as response:
                    response.raise_for_status()
                    data = await response.json()
            text = data.get("message", {}).get("content", "")
            return self._sanitize_reply(text)
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            self.logger.warning(
                "Ollama request failed (%s): %s",
                type(exc).__name__,
                str(exc) or "no detail",
            )
            return ""

    @staticmethod
    def _normalised_history(latest: List[dict]) -> List[dict]:
        messages: List[dict] = []
        for item in latest:
            current = dict(item)
            if messages and messages[-1]["role"] == current["role"]:
                separator = (
                    "\nThey then added: " if current["role"] == "user" else " "
                )
                messages[-1]["content"] += separator + current["content"]
            else:
                messages.append(current)
        return messages

    def _model_messages(self, policy, radio_id: str, latest: List[dict]) -> List[dict]:
        """Build Gemma-native user/model turns with persona in the first user turn."""
        persona = self._system_prompt(policy, radio_id)
        model_messages = self._normalised_history(latest)
        # Gemma 3 IT is trained for user/model turns rather than a distinct
        # system role. Ollama maps "system" to a separate user turn, which can
        # weaken instructions on the 1B model. Fold the persona into the first
        # user turn to match Gemma's documented prompt structure.
        first_user = next(
            (index for index, item in enumerate(model_messages)
             if item["role"] == "user"),
            None,
        )
        if first_user is None:
            model_messages.insert(0, {"role": "user", "content": persona})
        else:
            model_messages[first_user]["content"] = (
                f"{persona}\n\nConversation message:\n"
                f"{model_messages[first_user]['content']}"
            )
        return model_messages

    @staticmethod
    def _strip_timestamp_prefix(text: str) -> str:
        """Remove model-echoed timing metadata from the beginning of a reply."""
        text = text or ""
        patterns = (
            r"^\s*\[(?:sent|sent at|timestamp)\s+"
            r"\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
            r"(?:\s+[A-Z]{2,5})?\]\s*[-:–—]?\s*",
            r"^\s*(?:sent|sent at|timestamp)\s*[:\-]?\s*"
            r"\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"
            r"(?:\s+[A-Z]{2,5})?\s*[-:–—]?\s*",
            r"^\s*\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?::\d{2})?"
            r"(?:\s+[A-Z]{2,5})?\s*[-:–—]?\s*",
            r"^\s*\[Earlier conversation on \d{4}-\d{2}-\d{2};"
            r"\s*private timing metadata\]\s*",
        )
        changed = True
        while changed:
            changed = False
            for pattern in patterns:
                cleaned = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)
                if cleaned != text:
                    text, changed = cleaned, True
        return text.strip()

    @staticmethod
    def _sanitize_reply(text: str) -> str:
        """Remove formatting and pictographs that waste scarce radio bytes."""
        text = LLMService._strip_timestamp_prefix(text)
        text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
        text = re.sub(r"(?m)^\s*(?:#{1,6}|[-*+]\s+|\d+[.)]\s+)", "", text)
        text = re.sub(r"[*_~`]+", "", text)
        text = re.sub(
            "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]",
            "",
            text,
        )
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _utf8_prefix(text: str, byte_limit: int) -> str:
        encoded = text.encode("utf-8")
        if len(encoded) <= byte_limit:
            return text
        return encoded[:byte_limit].decode("utf-8", errors="ignore")

    def chunk_reply(self, text: str) -> List[str]:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []
        chunks: List[str] = []
        remaining = text
        while remaining and len(chunks) < self.max_chunks:
            candidate = self._utf8_prefix(remaining, max(16, self.chunk_bytes))
            if candidate != remaining:
                split = max(candidate.rfind(". "), candidate.rfind("? "), candidate.rfind("! "))
                if split < len(candidate) // 2:
                    split = candidate.rfind(" ")
                if split > 0:
                    candidate = candidate[: split + (1 if candidate[split] in ".?!" else 0)]
            chunks.append(candidate.strip())
            remaining = remaining[len(candidate) :].strip()
        if remaining and chunks:
            last = chunks[-1].rstrip(" .") + "…"
            chunks[-1] = self._utf8_prefix(last, self.chunk_bytes)
        return chunks
