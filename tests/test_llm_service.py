import asyncio
import configparser
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

from modules.llm_service import LLMService
from modules.message_handler import MessageHandler
from modules.models import MeshMessage


def make_service(tmp_path):
    config = configparser.ConfigParser()
    config["LLM"] = {
        "enabled": "true",
        "profiles_dir": "profiles",
        "min_reply_delay_seconds": "0",
        "max_reply_delay_seconds": "0",
        "max_reply_chunks": "2",
        "chunk_bytes": "40",
        "history_retention_days": "30",
        "max_profile_chars": "1000",
        "max_queue_size": "3",
    }
    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.bot_root = Path(tmp_path)
    bot.db_manager.db_path = str(tmp_path / "bot.db")
    bot.command_manager.send_response_chunked = AsyncMock(return_value=True)
    bot.command_manager.commands = {}
    return LLMService(bot)


def test_profile_scope_defaults_to_dm(tmp_path):
    service = make_service(tmp_path)
    with service._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(llm_contacts)")}
        assert "profile_scope" in columns
        key = "aa" * 32
        conn.execute(
            "INSERT INTO llm_contacts(public_key, contact_name, enabled, radio_id) VALUES (?, 'Alice', 1, 'bot')",
            (key,),
        )
        row = conn.execute(
            "SELECT profile_scope FROM llm_contacts WHERE public_key = ?", (key,)
        ).fetchone()
    assert row["profile_scope"] == "dm"


@pytest.mark.asyncio
async def test_all_scope_profile_is_used_for_channel_llm_message(tmp_path):
    service = make_service(tmp_path)
    key = "bb" * 32
    profile_path = service.profile_dir / "alice.md"
    profile_path.write_text("Alice prefers concise technical replies.", encoding="utf-8")
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO llm_contacts
               (public_key, contact_name, enabled, interaction_mode, radio_id, profile_file, profile_scope)
               VALUES (?, 'Alice', 1, 'automatic', 'bot', 'alice.md', 'all')""",
            (key,),
        )
    captured = []

    async def generate(policy, *_args):
        captured.append(policy)
        return "Channel reply"

    service._generate = generate
    message = MeshMessage(
        content="@MeshCoreBot hello",
        sender_id="Alice",
        sender_pubkey=key,
        channel="#nelson",
        is_dm=False,
    )
    assert await service.handle_message(message) is True
    await service._pending["bot:channel:#nelson"]
    assert captured
    assert captured[0]["profile_file"] == "alice.md"


def test_utf8_chunking_respects_byte_limit(tmp_path):
    service = make_service(tmp_path)
    chunks = service.chunk_reply("Kia ora " + "ā" * 50 + " this is a longer reply")
    assert len(chunks) == 2
    assert all(len(chunk.encode("utf-8")) <= 40 for chunk in chunks)
    assert not chunks[0].startswith("1/2 ")
    assert not chunks[1].startswith("2/2 ")


def test_numeric_keep_alive_is_sent_as_ollama_number(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["keep_alive"] = "-1"
    assert service._keep_alive_value() == -1
    service.config["LLM"]["keep_alive"] = "5m"
    assert service._keep_alive_value() == "5m"


@pytest.mark.asyncio
async def test_bot_dm_conversation_is_automatic_for_known_and_unknown_contacts(tmp_path):
    service = make_service(tmp_path)
    key = "ab" * 32
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO llm_contacts(public_key, contact_name, enabled, radio_id)
               VALUES (?, 'Alice', 1, 'bot')""",
            (key,),
        )
    service._generate = AsyncMock(return_value="Nice to hear from you.")
    allowed = MeshMessage(
        content="Hello", sender_id="Alice", sender_pubkey=key, is_dm=True
    )
    unknown = MeshMessage(
        content="Hello", sender_id="Alice", sender_pubkey="cd" * 32, is_dm=True
    )
    assert await service.handle_message(unknown) is True
    assert await service.handle_message(allowed) is True
    await asyncio.gather(*service._pending.values())
    assert service.bot.command_manager.send_response_chunked.await_count == 2


@pytest.mark.asyncio
async def test_unavailable_dm_radio_fails_closed(tmp_path):
    service = make_service(tmp_path)
    key = "ef" * 32
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO llm_contacts(public_key, contact_name, enabled, radio_id)
               VALUES (?, 'Bob', 1, 'dm')""",
            (key,),
        )
    message = MeshMessage(content="Hi", sender_id="Bob", sender_pubkey=key, is_dm=True)
    assert await service.handle_message(message) is True
    service.bot.command_manager.send_response_chunked.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_dm_no_longer_requires_persistent_ai_optin(tmp_path):
    service = make_service(tmp_path)
    key = "12" * 32
    plain = MeshMessage(content="How are you?", sender_id="Sam", sender_pubkey=key, is_dm=True)
    service._generate = AsyncMock(return_value="Doing well.")
    assert await service.handle_message(plain) is True
    await asyncio.gather(*service._pending.values())
    saved = service._history(f"bot:dm:{key}")
    assert any("How are you?" in item["content"] for item in saved)


@pytest.mark.asyncio
async def test_known_python_command_in_bot_dm_bypasses_llm(tmp_path):
    service = make_service(tmp_path)
    command = Mock()
    command.name = "wx"
    command.keywords = ["wx", "weather"]
    service.bot.command_manager.commands = {"wx": command}
    message = MeshMessage(
        content="wx Nelson",
        sender_id="Sam",
        sender_pubkey="13" * 32,
        is_dm=True,
    )

    assert await service.handle_message(message) is False
    service.bot.command_manager.send_response_chunked.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_menu_reply_uses_inbound_sender_when_bypassing_llm(tmp_path):
    service = make_service(tmp_path)

    class StatefulCommandManager:
        commands = {}

        def __init__(self):
            self.seen_message = None

        def is_command_trigger(self, content, ignored_commands=None, message=None):
            self.seen_message = message
            return content == "4" and message.sender_pubkey == "15" * 32

    manager = StatefulCommandManager()
    service.bot.command_manager = manager
    message = MeshMessage(
        content="4",
        sender_id="Builder",
        sender_pubkey="15" * 32,
        is_dm=True,
    )

    assert await service.handle_message(message) is False
    assert manager.seen_message is message


@pytest.mark.asyncio
async def test_natural_weather_dm_is_rewritten_for_existing_command_router(tmp_path):
    service = make_service(tmp_path)
    service._select_natural_command = AsyncMock(return_value="wx Nelson")
    message = MeshMessage(
        content="Could you give me the weather forecast for Nelson?",
        sender_id="Sam",
        sender_pubkey="14" * 32,
        is_dm=True,
    )

    assert await service.handle_message(message) is False
    assert message.content == "wx Nelson"
    service._select_natural_command.assert_awaited_once()
    service.bot.command_manager.send_response_chunked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmentioned_channel_never_attempts_natural_command_selection(tmp_path):
    service = make_service(tmp_path)
    service._select_natural_command = AsyncMock(return_value="wx Nelson")
    message = MeshMessage(
        content="What is the Nelson weather forecast?",
        sender_id="Sam",
        channel="Public",
    )

    assert await service.handle_message(message) is False
    service._select_natural_command.assert_not_awaited()


def test_tool_call_parser_accepts_only_approved_command_and_safe_arguments():
    response = {
        "choices": [{
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "run_meshcore_command",
                        "arguments": json.dumps({
                            "command": "wx",
                            "arguments": "Nelson tomorrow",
                        }),
                    }
                }]
            }
        }]
    }

    assert LLMService._parse_tool_command(response, {"wx"}) == "wx Nelson tomorrow"
    assert LLMService._parse_tool_command(response, {"moon"}) is None
    response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = (
        '{"command":"wx","arguments":"Nelson; reload"}'
    )
    assert LLMService._parse_tool_command(response, {"wx"}) is None


def test_natural_weather_command_defaults_to_configured_nelson_location(tmp_path):
    service = make_service(tmp_path)
    service.config["Weather"] = {
        "default_weather_location": "Nelson, New Zealand",
    }

    assert service._normalise_selected_command("wx") == "wx Nelson, New Zealand"
    assert (
        service._normalise_selected_command("wx tomorrow")
        == "wx Nelson, New Zealand tomorrow"
    )
    assert service._normalise_selected_command("wx Christchurch") == "wx Christchurch"


def test_bare_natural_weather_questions_route_deterministically_to_nelson(tmp_path):
    service = make_service(tmp_path)
    service.config["Weather"] = {
        "default_weather_location": "Nelson, New Zealand",
    }

    assert (
        service._default_weather_request("what's the weather forecast bot?")
        == "wx Nelson, New Zealand"
    )
    assert (
        service._default_weather_request("Could you give me the forcast tomorrow?")
        == "wx Nelson, New Zealand tomorrow"
    )
    assert service._default_weather_request("The weather was lovely yesterday") is None
    assert service._default_weather_request("What's the weather for Christchurch?") is None


@pytest.mark.asyncio
async def test_default_weather_selection_does_not_depend_on_deepseek_tool_choice(tmp_path):
    service = make_service(tmp_path)
    service.config["Weather"] = {
        "default_weather_location": "Nelson, New Zealand",
    }
    command = Mock()
    command.name = "wx"
    service.bot.command_manager.commands = {"wx": command}

    selected = await service._select_natural_command(
        "what's the weather forecast bot?"
    )

    assert selected == "wx Nelson, New Zealand"


@pytest.mark.asyncio
async def test_channel_requires_mention_and_known_command_falls_through(tmp_path):
    service = make_service(tmp_path)
    command = Mock()
    command.name = "wx"
    command.keywords = ["wx", "weather"]
    service.bot.command_manager.commands = {"wx": command}
    ordinary = MeshMessage(content="hello", sender_id="Sam", channel="Public")
    command_message = MeshMessage(
        content="@MeshCoreBot wx Nelson", sender_id="Sam", channel="Public"
    )
    mentioned = MeshMessage(
        content="@MeshCoreBot how are you?", sender_id="Sam", channel="Public"
    )
    service._generate = AsyncMock(return_value="Doing well.")
    assert await service.handle_message(ordinary) is False
    assert await service.handle_message(command_message) is False
    assert command_message.content == "wx Nelson"
    assert await service.handle_message(mentioned) is True
    await asyncio.gather(*service._pending.values())


@pytest.mark.asyncio
async def test_channel_mention_is_case_insensitive_and_marks_selected_tool(tmp_path):
    service = make_service(tmp_path)
    service._select_natural_command = AsyncMock(
        return_value="wx Nelson, New Zealand"
    )
    message = MeshMessage(
        content="@mEsHcOrEbOt what's the weather forecast?",
        sender_id="Sam",
        channel="#new-channel",
        routing_info={"path_length": 1},
    )

    assert await service.handle_message(message) is False
    assert message.content == "wx Nelson, New Zealand"
    assert message.routing_info["path_length"] == 1
    assert message.routing_info["llm_mentioned_tool_command"] == "wx"


@pytest.mark.asyncio
async def test_meshcore_bracketed_contact_mention_routes_to_llm(tmp_path):
    service = make_service(tmp_path)
    service._generate = AsyncMock(return_value="About 93 degrees Celsius.")
    message = MeshMessage(
        content="@[MeshCoreBot] what's 200f in c?",
        sender_id="Sam",
        channel="#new-channel",
    )

    assert await service.handle_message(message) is True
    await asyncio.gather(*service._pending.values())
    saved = service._history("bot:channel:#new-channel")
    assert any("what's 200f in c?" in item["content"] for item in saved)
    service.bot.command_manager.send_response_chunked.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    (
        "@[MeshCoreBot] Hello",
        "@[MeshCoreBot] Hey mate. Are you still there?",
    ),
)
async def test_mentioned_channel_greetings_are_conversations(tmp_path, content):
    service = make_service(tmp_path)
    hello = Mock()
    hello.name = "hello"
    hello.keywords = ["hello", "hi", "hey"]
    service.bot.command_manager.commands = {"hello": hello}
    service._generate = AsyncMock(return_value="Yep, still here!")
    message = MeshMessage(
        content=content,
        sender_id="Chris",
        channel="#nelson",
    )

    assert await service.handle_message(message) is True
    await asyncio.gather(*service._pending.values())
    service.bot.command_manager.send_response_chunked.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    (
        "Hello",
        "Hey mate. How are you?",
        "!hello",
    ),
)
async def test_dm_greetings_are_always_conversations(tmp_path, content):
    service = make_service(tmp_path)
    stale_hello = Mock()
    stale_hello.name = "hello"
    stale_hello.keywords = ["hello", "hi", "hey"]
    service.bot.command_manager.commands = {"hello": stale_hello}
    service._generate = AsyncMock(return_value="Hi! How are you doing?")
    message = MeshMessage(
        content=content,
        sender_id="Chris",
        sender_pubkey="39" * 32,
        is_dm=True,
    )

    assert await service.handle_message(message) is True
    await asyncio.gather(*service._pending.values())
    service.bot.command_manager.send_response_chunked.assert_awaited_once()


def test_message_handler_accepts_only_an_approved_mentioned_tool_off_channel():
    config = configparser.ConfigParser()
    config["Bot"] = {"enabled": "true"}
    config["Channels"] = {"respond_to_dms": "true"}
    command_manager = Mock()
    command_manager.is_user_banned.return_value = False
    command_manager.monitor_channels = ["Public"]
    command_manager.commands = {"wx": object()}
    bot = Mock()
    bot.config = config
    bot.command_manager = command_manager
    bot.llm_service.tool_commands = {"wx"}
    handler = object.__new__(MessageHandler)
    handler.bot = bot
    handler.logger = Mock()
    message = MeshMessage(
        content="wx Nelson, New Zealand",
        sender_id="Sam",
        channel="#new-channel",
        routing_info={"llm_mentioned_tool_command": "wx"},
    )

    assert handler.should_process_message(message) is True
    message.routing_info["llm_mentioned_tool_command"] = "alert"
    assert handler.should_process_message(message) is False


@pytest.mark.asyncio
async def test_channel_mention_requires_a_name_boundary(tmp_path):
    service = make_service(tmp_path)
    misleading = MeshMessage(
        content="@MeshCoreBotFake hello", sender_id="Sam", channel="Public"
    )
    assert await service.handle_message(misleading) is False


@pytest.mark.asyncio
async def test_ollama_generations_are_serialized(tmp_path):
    service = make_service(tmp_path)
    active = 0
    peak = 0

    async def generate(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "reply"

    service._generate = generate
    policy = {"public_key": "key", "contact_name": "Test", "profile_file": None}
    replies = await asyncio.gather(
        service._queued_generate(policy, "bot:dm:first", "bot"),
        service._queued_generate(policy, "bot:dm:second", "bot"),
    )
    assert replies == ["reply", "reply"]
    assert peak == 1
    assert service._queued_requests == 0


@pytest.mark.asyncio
async def test_messages_during_initial_delay_are_bundled_into_one_reply(tmp_path):
    service = make_service(tmp_path)
    key = "a1" * 32
    conversation_key = f"bot:dm:{key}"
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO llm_contacts
               (public_key, contact_name, enabled, interaction_mode, radio_id)
               VALUES (?, 'Alice', 1, 'automatic', 'bot')""",
            (key,),
        )
    service._setting_float = Mock(return_value=0.05)
    delay_patch = patch("modules.llm_service.random.uniform", return_value=0.05)
    delay_patch.start()
    generated = []

    async def generate(*_args):
        generated.append(service._model_messages(
            service._policy(key), "bot", service._history(conversation_key)
        ))
        return "I heard both messages."

    service._generate = generate
    first = MeshMessage(content="First thought", sender_id="Alice", sender_pubkey=key, is_dm=True)
    second = MeshMessage(content="And another thought", sender_id="Alice", sender_pubkey=key, is_dm=True)
    assert await service.handle_message(first) is True
    await asyncio.sleep(0.005)
    assert await service.handle_message(second) is True
    await service._pending[conversation_key]

    assert len(generated) == 1
    assert "First thought" in generated[0][-1]["content"]
    assert "They then added: And another thought" in generated[0][-1]["content"]
    service.bot.command_manager.send_response_chunked.assert_awaited_once()
    delay_patch.stop()


@pytest.mark.asyncio
async def test_messages_during_generation_get_one_followup_without_cancelling(tmp_path):
    service = make_service(tmp_path)
    key = "b2" * 32
    conversation_key = f"bot:dm:{key}"
    with service._connect() as conn:
        conn.execute(
            """INSERT INTO llm_contacts
               (public_key, contact_name, enabled, interaction_mode, radio_id)
               VALUES (?, 'Bob', 1, 'automatic', 'bot')""",
            (key,),
        )
    started = asyncio.Event()
    release = asyncio.Event()
    generated = []

    async def generate(_policy, _conversation_key, _radio_id, unanswered=None):
        generated.append(list(unanswered or []))
        if len(generated) == 1:
            started.set()
            await release.wait()
        return f"Reply {len(generated)}"

    service._generate = generate
    first = MeshMessage(content="Question one?", sender_id="Bob", sender_pubkey=key, is_dm=True)
    second = MeshMessage(content="Question two?", sender_id="Bob", sender_pubkey=key, is_dm=True)
    third = MeshMessage(content="One more detail", sender_id="Bob", sender_pubkey=key, is_dm=True)
    assert await service.handle_message(first) is True
    await started.wait()
    assert await service.handle_message(second) is True
    assert await service.handle_message(third) is True
    release.set()
    await service._pending[conversation_key]

    assert len(generated) == 2
    assert generated[0] == []
    assert generated[1] == ["Question two?", "One more detail"]
    assert service.bot.command_manager.send_response_chunked.await_count == 2


def test_old_history_is_purged_and_profiles_are_bounded(tmp_path):
    service = make_service(tmp_path)
    key = "56" * 32
    service._save_message(key, "user", "old")
    with service._connect() as conn:
        conn.execute(
            "UPDATE llm_messages SET created_at = ? WHERE public_key = ?",
            (time.time() - 31 * 86400, key),
        )
        conn.execute(
            """INSERT INTO llm_contacts
               (public_key, contact_name, enabled, radio_id, profile_file)
               VALUES (?, 'Profile', 1, 'bot', 'profile.md')""",
            (key,),
        )
    (service.profile_dir / "profile.md").write_text("x" * 1500, encoding="utf-8")
    service._last_purge_at = 0
    assert service._purge_old_history(force=True) == 1
    assert len(service._profile_text(service._policy(key))) == 1000


def test_generated_output_is_radio_plain_text():
    raw = "# Heading\n- **Hello** 👋\n`radio`"
    assert LLMService._sanitize_reply(raw) == "Heading Hello radio"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[Sent 2026-07-28 22:21] Yeah, all good.", "Yeah, all good."),
        ("Sent at: 2026-07-28 22:21 - Still here.", "Still here."),
        ("2026-07-28 22:21 — Doing well.", "Doing well."),
        (
            "[Earlier conversation on 2026-07-27; private timing metadata] Hello",
            "Hello",
        ),
    ],
)
def test_generated_output_never_exposes_timing_metadata(raw, expected):
    assert LLMService._sanitize_reply(raw) == expected


def test_today_history_is_natural_and_old_history_is_date_only(tmp_path):
    service = make_service(tmp_path)
    key = "90" * 32
    now = time.time()
    service._save_message(key, "user", "Today message", now)
    service._save_message(
        key,
        "assistant",
        "[Sent 2026-07-28 22:21] Earlier reply",
        now - 2 * 86400,
    )
    history = service._history(key)
    assert history[0]["content"] == "Today message"
    assert "[Sent " not in history[1]["content"]
    assert history[1]["content"].startswith("[Earlier conversation on ")
    assert history[1]["content"].endswith("Earlier reply")


def test_localisation_context_uses_configured_time_and_period(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["operating_location"] = "Example City"
    service.config["LLM"]["operating_timezone"] = "UTC"
    local_time = datetime(
        2026, 7, 29, 9, 15, tzinfo=ZoneInfo("UTC")
    )

    context = service._localisation_context(local_time)

    assert "Example City" in context
    assert "Wednesday, 29 July 2026 at 09:15 AM" in context
    assert "UTC, UTC+00:00" in context
    assert "it is morning" in context
    assert "do not prefix replies with a date or time" in context


def test_localisation_context_uses_utc_when_configured(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["operating_timezone"] = "UTC"
    summer_time = datetime(
        2026, 12, 29, 14, 30, tzinfo=ZoneInfo("UTC")
    )

    context = service._localisation_context(summer_time)

    assert "UTC+00:00" in context
    assert "it is afternoon" in context


def test_gemma_persona_is_folded_into_first_user_turn(tmp_path):
    service = make_service(tmp_path)
    policy = {"public_key": "key", "contact_name": "Test", "profile_file": None}
    messages = service._model_messages(
        policy,
        "bot",
        [
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "Good thanks."},
            {"role": "user", "content": "What's new?"},
        ],
    )
    assert all(message["role"] != "system" for message in messages)
    assert "How are you?" in messages[0]["content"]
    assert "warm" in messages[0]["content"].lower()
    assert messages[-1] == {"role": "user", "content": "What's new?"}


@pytest.mark.asyncio
async def test_bot_identity_disclosure_is_deterministic(tmp_path):
    service = make_service(tmp_path)
    key = "34" * 32
    conversation_key = f"bot:dm:{key}"
    service._save_message(conversation_key, "user", "Are you an AI?", 100)
    reply = await service._generate(
        {"public_key": key, "contact_name": "Test", "profile_file": None},
        conversation_key,
        "bot",
    )
    assert "Gemma 3 1B" in reply


@pytest.mark.asyncio
async def test_deepseek_is_primary_when_configured(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["provider"] = "deepseek"
    service._generate_deepseek = AsyncMock(return_value="Remote reply")
    service._generate_ollama = AsyncMock(return_value="Local reply")
    key = "35" * 32
    conversation_key = f"bot:dm:{key}"
    service._save_message(conversation_key, "user", "How are you?", 100)

    reply = await service._generate(
        {"public_key": key, "contact_name": "Test", "profile_file": None},
        conversation_key,
        "bot",
    )

    assert reply == "Remote reply"
    service._generate_deepseek.assert_awaited_once()
    service._generate_ollama.assert_not_awaited()
    assert service._last_provider == "deepseek"


@pytest.mark.asyncio
async def test_gemma_is_used_when_deepseek_is_unavailable(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["provider"] = "deepseek"
    service.config["LLM"]["fallback_to_ollama"] = "true"
    service._generate_deepseek = AsyncMock(return_value="")
    service._generate_ollama = AsyncMock(return_value="Offline reply")
    key = "36" * 32
    conversation_key = f"bot:dm:{key}"
    service._save_message(conversation_key, "user", "Still there?", 100)

    reply = await service._generate(
        {"public_key": key, "contact_name": "Test", "profile_file": None},
        conversation_key,
        "bot",
    )

    assert reply == "Offline reply"
    service._generate_ollama.assert_awaited_once()
    assert service._last_provider == "ollama-fallback"
    assert service._fallback_count == 1
    assert service._last_error == "DeepSeek unavailable; Gemma fallback used"


@pytest.mark.asyncio
async def test_deepseek_bot_identity_names_the_offline_fallback(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["provider"] = "deepseek"
    key = "37" * 32
    conversation_key = f"bot:dm:{key}"
    service._save_message(conversation_key, "user", "What AI are you?", 100)

    reply = await service._generate(
        {"public_key": key, "contact_name": "Test", "profile_file": None},
        conversation_key,
        "bot",
    )

    assert "DeepSeek V4 Flash" in reply
    assert "Gemma 3 1B" in reply


@pytest.mark.asyncio
async def test_deepseek_bot_identity_recognises_broad_question(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["provider"] = "deepseek"
    key = "38" * 32
    conversation_key = f"bot:dm:{key}"
    service._save_message(conversation_key, "user", "What are you?", 100)

    reply = await service._generate(
        {"public_key": key, "contact_name": "Test", "profile_file": None},
        conversation_key,
        "bot",
    )

    assert "DeepSeek V4 Flash" in reply
    assert "offline fallback" in reply


def test_deepseek_system_prompt_uses_primary_model_identity(tmp_path):
    service = make_service(tmp_path)
    service.config["LLM"]["provider"] = "deepseek"
    policy = {"public_key": "key", "contact_name": "Test", "profile_file": None}

    prompt = service._system_prompt(policy, "bot")

    assert "DeepSeek V4 Flash" in prompt
    assert "Gemma 3 1B as my offline fallback" in prompt


def test_consecutive_messages_are_combined_for_provider_history(tmp_path):
    service = make_service(tmp_path)
    messages = service._normalised_history([
        {"role": "user", "content": "First point"},
        {"role": "user", "content": "Second point"},
        {"role": "assistant", "content": "Got both."},
    ])

    assert messages == [
        {
            "role": "user",
            "content": "First point\nThey then added: Second point",
        },
        {"role": "assistant", "content": "Got both."},
    ]


@pytest.mark.asyncio
async def test_personal_identity_variation_is_deterministic(tmp_path):
    service = make_service(tmp_path)
    key = "78" * 32
    conversation_key = f"dm:dm:{key}"
    service._save_message(
        conversation_key, "user", "Am I chatting with a bot or a real person?", 100
    )
    reply = await service._generate(
        {"public_key": key, "contact_name": "Test", "profile_file": None},
        conversation_key,
        "dm",
    )
    assert reply == "It's the radio owner here."
