"""Tests for FeedManager pure formatting and filtering logic."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import pytest
from configparser import ConfigParser
from unittest.mock import AsyncMock, Mock

from modules.feed_manager import (
    FeedManager, extract_cap_areas, extract_cap_metadata, split_message
)


@pytest.fixture
def fm(mock_logger):
    """FeedManager with disabled networking for pure-logic tests."""
    bot = Mock()
    bot.logger = mock_logger
    bot.config = ConfigParser()
    bot.config.add_section("Feed_Manager")
    bot.config.set("Feed_Manager", "feed_manager_enabled", "false")
    bot.config.set("Feed_Manager", "max_message_length", "200")
    bot.db_manager = Mock()
    bot.db_manager.db_path = "/dev/null"
    return FeedManager(bot)


class TestApplyShortening:
    """Tests for _apply_shortening()."""

    def test_truncate_short_text_unchanged(self, fm):
        assert fm._apply_shortening("hello", "truncate:20") == "hello"

    def test_truncate_long_text_adds_ellipsis(self, fm):
        result = fm._apply_shortening("Hello World", "truncate:5")
        assert result == "Hello..."

    def test_word_wrap_breaks_at_boundary(self, fm):
        result = fm._apply_shortening("Hello beautiful world", "word_wrap:15")
        # word_wrap truncates at a word boundary and appends "..."
        # "Hello beautiful world"[:15] = "Hello beautiful", last space at 5 (too early),
        # so result is "Hello beautiful..." (truncated at 15 chars + ellipsis)
        assert result.endswith("...")
        # The base text (without ellipsis) should be <= the wrap limit
        assert len(result.rstrip(".")) <= 15 or result == "Hello beautiful..."

    def test_first_words_limits_count(self, fm):
        result = fm._apply_shortening("one two three four", "first_words:2")
        assert result.startswith("one two")

    def test_regex_extracts_group(self, fm):
        result = fm._apply_shortening("Price: $42.99 today", r"regex:Price: \$(\d+\.\d+)")
        assert result == "42.99"

    def test_if_regex_returns_then_on_match(self, fm):
        result = fm._apply_shortening("open", "if_regex:open:YES:NO")
        assert result == "YES"

    def test_if_regex_returns_else_on_no_match(self, fm):
        result = fm._apply_shortening("closed", "if_regex:open:YES:NO")
        assert result == "NO"

    def test_empty_text_returns_empty(self, fm):
        assert fm._apply_shortening("", "truncate:10") == ""


class TestSplitMessage:
    def test_short_message_unchanged(self):
        assert split_message("short message", 20) == ["short message"]

    def test_prefers_existing_line_boundary(self):
        message = "Emergency alert title\nhttps://example.test/full-link"
        assert split_message(message, 30) == [
            "Emergency alert title",
            "https://example.test/full-link",
        ]

    def test_splits_long_text_at_word_boundary(self):
        chunks = split_message("one two three four five", 10)
        assert chunks == ["one two", "three four", "five"]
        assert all(len(chunk) <= 10 for chunk in chunks)

    def test_hard_splits_single_oversized_token(self):
        assert split_message("abcdefghijkl", 5) == ["abcde", "fghij", "kl"]

    def test_respects_utf8_transport_length(self):
        chunks = split_message("🚨 Emergency warning for the area", 20)
        assert all(len(chunk.encode("utf-8")) <= 20 for chunk in chunks)

    def test_format_message_is_not_truncated_when_chunking_enabled(self, fm):
        fm.max_message_length = 10
        item = {"title": "a deliberately long title"}
        feed = {"output_format": "{title}", "chunking_enabled": 1}
        assert fm.format_message(item, feed) == item["title"]


class TestExtractCapAreas:
    def test_extracts_area_description_from_embedded_cap(self):
        rss = '''<?xml version="1.0"?>
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
          <channel><item>
            <guid isPermaLink="false">alert-123#0</guid>
            <content:encoded>&lt;alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"&gt;
              &lt;info&gt;&lt;area&gt;&lt;areaDesc&gt;TS37: Fiordland&lt;/areaDesc&gt;&lt;/area&gt;&lt;/info&gt;
            &lt;/alert&gt;</content:encoded>
          </item></channel>
        </rss>'''
        assert extract_cap_areas(rss) == {"alert-123#0": "TS37: Fiordland"}

    def test_combines_multiple_unique_areas(self):
        rss = '''<rss xmlns:content="http://purl.org/rss/1.0/modules/content/" version="2.0">
          <channel><item><guid>alert-1</guid>
            <content:encoded>&lt;alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"&gt;&lt;info&gt;
              &lt;area&gt;&lt;areaDesc&gt;Area One&lt;/areaDesc&gt;&lt;/area&gt;
              &lt;area&gt;&lt;areaDesc&gt;Area Two&lt;/areaDesc&gt;&lt;/area&gt;
            &lt;/info&gt;&lt;/alert&gt;</content:encoded>
          </item></channel>
        </rss>'''
        assert extract_cap_areas(rss) == {"alert-1": "Area One; Area Two"}


class TestExtractCapMetadata:
    def test_extracts_weather_alert_fields(self):
        cap = '''<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><info>
          <event>Heavy Rain Warning</event><urgency>Expected</urgency>
          <severity>Severe</severity><expires>2026-07-18T12:00:00+12:00</expires>
          <headline>Orange Heavy Rain Warning</headline>
          <area><areaDesc>Nelson west of Motueka</areaDesc></area>
          <area><areaDesc>Tasman District</areaDesc></area>
        </info></alert>'''
        assert extract_cap_metadata(cap) == {
            'area': 'Nelson west of Motueka; Tasman District',
            'event': 'Heavy Rain Warning',
            'headline': 'Orange Heavy Rain Warning',
            'severity': 'Severe',
            'urgency': 'Expected',
            'expires': '2026-07-18T12:00:00+12:00',
        }

    def test_invalid_cap_returns_empty_metadata(self):
        assert extract_cap_metadata('<not-valid') == {}


class TestChunkQueue:
    @staticmethod
    def make_manager(tmp_path, send_result=True):
        db_path = tmp_path / "queue.db"
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE feed_subscriptions (
                id INTEGER PRIMARY KEY,
                message_send_interval_seconds REAL,
                feed_name TEXT
            );
            CREATE TABLE feed_message_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER,
                channel_name TEXT,
                message TEXT,
                item_id TEXT,
                item_title TEXT,
                priority INTEGER DEFAULT 0,
                chunk_index INTEGER DEFAULT 0,
                chunk_count INTEGER DEFAULT 1,
                attempt_count INTEGER DEFAULT 0,
                last_attempt_at TIMESTAMP,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
            );
            CREATE TABLE feed_activity (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER,
                item_id TEXT,
                item_title TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_sent BOOLEAN DEFAULT 1
            );
            CREATE TABLE feed_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER,
                error_type TEXT,
                error_message TEXT,
                occurred_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO feed_subscriptions VALUES (2, 0, 'Emergency alerts');
            INSERT INTO feed_message_queue
                (feed_id, channel_name, message, item_id, item_title, chunk_index, chunk_count)
            VALUES
                (2, '#emergency', 'part one', 'alert-1', 'Alert', 0, 2),
                (2, '#emergency', 'part two', 'alert-1', 'Alert', 1, 2);
        """)
        conn.commit()
        conn.close()

        class DB:
            def __init__(self, path):
                self.db_path = path

            @contextmanager
            def connection(self):
                connection = sqlite3.connect(self.db_path)
                try:
                    yield connection
                finally:
                    connection.close()

        bot = Mock()
        bot.logger = Mock()
        bot.config = ConfigParser()
        bot.config.add_section("Feed_Manager")
        bot.config.set("Feed_Manager", "feed_manager_enabled", "false")
        bot.db_manager = DB(db_path)
        bot.command_manager.send_channel_message = AsyncMock(return_value=send_result)
        return FeedManager(bot), bot, db_path

    @pytest.mark.asyncio
    async def test_records_activity_only_after_final_chunk(self, tmp_path):
        manager, bot, db_path = self.make_manager(tmp_path)
        await manager.process_message_queue()

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM feed_message_queue WHERE sent_at IS NOT NULL").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM feed_activity").fetchone()[0] == 1
        conn.close()
        assert bot.command_manager.send_channel_message.await_count == 2

    @pytest.mark.asyncio
    async def test_failure_blocks_later_chunks_for_that_item(self, tmp_path):
        manager, bot, db_path = self.make_manager(tmp_path, send_result=False)
        await manager.process_message_queue()

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM feed_message_queue WHERE sent_at IS NOT NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM feed_activity").fetchone()[0] == 0
        conn.close()
        assert bot.command_manager.send_channel_message.await_count == 1

    @pytest.mark.asyncio
    async def test_emergency_message_retries_only_once_without_repeat(self, tmp_path):
        manager, bot, db_path = self.make_manager(tmp_path)
        manager.emergency_repeat_wait_seconds = 0
        manager.emergency_retry_delay_seconds = 0
        bot.transmission_tracker.get_repeat_info.return_value = {'repeat_count': 0}

        await manager.process_message_queue()
        await manager.process_message_queue()

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT attempt_count, sent_at FROM feed_message_queue ORDER BY id"
        ).fetchall()
        conn.close()
        assert [attempt_count for attempt_count, _ in rows] == [2, 2]
        assert all(sent_at is not None for _, sent_at in rows)
        assert bot.command_manager.send_channel_message.await_count == 4

    @pytest.mark.asyncio
    async def test_emergency_repeat_confirmation_avoids_retry(self, tmp_path):
        manager, bot, db_path = self.make_manager(tmp_path)
        manager.emergency_repeat_wait_seconds = 0
        bot.transmission_tracker.get_repeat_info.return_value = {'repeat_count': 1}

        await manager.process_message_queue()

        conn = sqlite3.connect(db_path)
        attempts = conn.execute(
            "SELECT attempt_count FROM feed_message_queue ORDER BY id"
        ).fetchall()
        conn.close()
        assert attempts == [(1,), (1,)]
        assert bot.command_manager.send_channel_message.await_count == 2

    @pytest.mark.asyncio
    async def test_failed_emergency_chunks_never_exceed_two_attempts(self, tmp_path):
        manager, bot, db_path = self.make_manager(tmp_path, send_result=False)

        for _ in range(6):
            await manager.process_message_queue()

        conn = sqlite3.connect(db_path)
        attempts = conn.execute(
            "SELECT attempt_count FROM feed_message_queue ORDER BY id"
        ).fetchall()
        conn.close()
        assert attempts == [(2,), (2,)]
        assert bot.command_manager.send_channel_message.await_count == 4


class TestGetNestedValue:
    """Tests for _get_nested_value()."""

    def test_simple_field_access(self, fm):
        assert fm._get_nested_value({"name": "test"}, "name") == "test"

    def test_nested_field_access(self, fm):
        data = {"raw": {"Priority": "high"}}
        assert fm._get_nested_value(data, "raw.Priority") == "high"

    def test_missing_field_returns_default(self, fm):
        assert fm._get_nested_value({}, "missing") == ""
        assert fm._get_nested_value({}, "missing", "N/A") == "N/A"


class TestShouldSendItem:
    """Tests for _should_send_item() filter evaluation."""

    def test_no_filter_sends_all(self, fm):
        feed = {"id": 1}
        item = {"raw": {"Priority": "low"}}
        assert fm._should_send_item(feed, item) is True

    def test_equals_filter_matches(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "equals", "value": "high"}
                ]
            }),
        }
        item = {"raw": {"Priority": "high"}}
        assert fm._should_send_item(feed, item) is True

    def test_equals_filter_rejects(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "equals", "value": "high"}
                ]
            }),
        }
        item = {"raw": {"Priority": "low"}}
        assert fm._should_send_item(feed, item) is False

    def test_in_filter_matches(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "in", "values": ["high", "highest"]}
                ]
            }),
        }
        item = {"raw": {"Priority": "highest"}}
        assert fm._should_send_item(feed, item) is True

    def test_and_logic_all_must_pass(self, fm):
        feed = {
            "id": 1,
            "filter_config": json.dumps({
                "conditions": [
                    {"field": "Priority", "operator": "equals", "value": "high"},
                    {"field": "Status", "operator": "equals", "value": "open"},
                ],
                "logic": "AND",
            }),
        }
        # First condition passes, second fails
        item = {"raw": {"Priority": "high", "Status": "closed"}}
        assert fm._should_send_item(feed, item) is False


class TestFormatTimestamp:
    """Tests for _format_timestamp()."""

    def test_recent_timestamp(self, fm):
        five_min_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        result = fm._format_timestamp(five_min_ago)
        assert "5m ago" in result

    def test_none_returns_empty(self, fm):
        assert fm._format_timestamp(None) == ""
