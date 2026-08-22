#!/usr/bin/env python3
"""Persistent, low-bandwidth DM games for the MeshCore bot."""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


GAME_DEFINITIONS = {
    "lemonade": {
        "name": "Lemonade Stand",
        "aliases": {"lemonade", "lemonstand"},
        "score_order": "DESC",
        "score_unit": "$",
    },
    "blackjack": {
        "name": "Blackjack",
        "aliases": {"blackjack", "bj"},
        "score_order": "DESC",
        "score_unit": " chips",
    },
    "mastermind": {
        "name": "Mastermind",
        "aliases": {"mastermind", "mmind"},
        "score_order": "ASC",
        "score_unit": " guesses",
    },
}


def ensure_game_schema(conn: sqlite3.Connection) -> None:
    """Create the shared game tables and default enable switches."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS game_settings (
            game_key TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS game_sessions (
            public_key TEXT PRIMARY KEY,
            player_name TEXT NOT NULL,
            game_key TEXT NOT NULL,
            state_json TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (unixepoch()),
            updated_at REAL NOT NULL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS game_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_key TEXT NOT NULL,
            player_name TEXT NOT NULL,
            game_key TEXT NOT NULL,
            score REAL NOT NULL,
            summary TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (unixepoch())
        );
        CREATE INDEX IF NOT EXISTS idx_game_scores_game_score
            ON game_scores(game_key, score);
        CREATE INDEX IF NOT EXISTS idx_game_sessions_updated
            ON game_sessions(updated_at);
        """
    )
    for game_key in GAME_DEFINITIONS:
        conn.execute(
            """INSERT OR IGNORE INTO game_settings(game_key, enabled)
               VALUES (?, 1)""",
            (game_key,),
        )


@dataclass
class GameTurn:
    messages: List[str]
    state: Optional[dict]
    score: Optional[float] = None
    summary: Optional[str] = None


class GameService:
    """Routes one persistent game session per DM contact."""

    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.logger = bot.logger
        self.db_path = str(bot.db_manager.db_path)
        self.enabled = self.config.getboolean("Games", "enabled", fallback=True)
        self.expiry_days = max(
            1, self.config.getint("Games", "session_expiry_days", fallback=7)
        )
        self.max_chunks = min(
            2, max(1, self.config.getint("Games", "max_reply_chunks", fallback=2))
        )
        self.chunk_bytes = min(
            145, max(80, self.config.getint("Games", "chunk_bytes", fallback=135))
        )
        with self._connect() as conn:
            ensure_game_schema(conn)
        self._purge_expired()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=60)
        conn.row_factory = sqlite3.Row
        return conn

    def _purge_expired(self) -> int:
        cutoff = time.time() - (self.expiry_days * 86400)
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM game_sessions WHERE updated_at < ?", (cutoff,)
            )
        if cursor.rowcount:
            self.logger.info("Expired %d inactive game session(s)", cursor.rowcount)
        return cursor.rowcount

    @staticmethod
    def _player_key(message) -> str:
        public_key = (message.sender_pubkey or "").strip().lower()
        if public_key:
            return public_key
        return f"name:{(message.sender_id or 'unknown').strip().lower()}"

    def _enabled_games(self) -> Dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT game_key, enabled FROM game_settings"
            ).fetchall()
        switches = {row["game_key"]: bool(row["enabled"]) for row in rows}
        return {
            key: definition
            for key, definition in GAME_DEFINITIONS.items()
            if switches.get(key, True)
        }

    def _get_session(self, public_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT player_name, game_key, state_json, updated_at
                   FROM game_sessions WHERE public_key = ?""",
                (public_key,),
            ).fetchone()
        if not row:
            return None
        try:
            state = json.loads(row["state_json"])
        except (TypeError, json.JSONDecodeError):
            self._delete_session(public_key)
            return None
        return {
            "player_name": row["player_name"],
            "game_key": row["game_key"],
            "state": state,
            "updated_at": row["updated_at"],
        }

    def _save_session(
        self, public_key: str, player_name: str, game_key: str, state: dict
    ) -> None:
        payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO game_sessions
                       (public_key, player_name, game_key, state_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(public_key) DO UPDATE SET
                       player_name=excluded.player_name,
                       game_key=excluded.game_key,
                       state_json=excluded.state_json,
                       updated_at=excluded.updated_at""",
                (public_key, player_name, game_key, payload, now, now),
            )

    def _delete_session(self, public_key: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM game_sessions WHERE public_key = ?", (public_key,)
            )

    def _save_score(
        self,
        public_key: str,
        player_name: str,
        game_key: str,
        score: float,
        summary: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO game_scores
                       (public_key, player_name, game_key, score, summary)
                   VALUES (?, ?, ?, ?, ?)""",
                (public_key, player_name, game_key, score, summary),
            )

    @staticmethod
    def _resolve_game(text: str, enabled: Dict[str, dict]) -> Optional[str]:
        candidate = text.strip().lower()
        for key, definition in enabled.items():
            if candidate == key or candidate in definition["aliases"]:
                return key
        return None

    def _menu(self) -> str:
        enabled = self._enabled_games()
        if not enabled:
            return "Games are currently unavailable."
        names = ", ".join(definition["name"] for definition in enabled.values())
        return f"🎮 Games: {names}. Start with game lemonade, game blackjack, or game mastermind."

    def _leaderboard(self, game_key: str) -> str:
        definition = GAME_DEFINITIONS[game_key]
        order = definition["score_order"]
        with self._connect() as conn:
            rows = conn.execute(
                f"""WITH ranked AS (
                        SELECT player_name, score, summary, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY public_key
                                   ORDER BY score {order}, created_at ASC
                               ) AS player_rank
                        FROM game_scores WHERE game_key = ?
                    )
                    SELECT player_name, score, summary FROM ranked
                    WHERE player_rank = 1
                    ORDER BY score {order}, created_at ASC LIMIT 5""",
                (game_key,),
            ).fetchall()
        if not rows:
            return f"No {definition['name']} scores yet."
        entries = []
        for index, row in enumerate(rows, 1):
            value = row["score"]
            if game_key == "lemonade":
                formatted = f"${value:.2f}"
            else:
                formatted = f"{int(value)}{definition['score_unit']}"
            entries.append(f"{index}.{row['player_name']} {formatted}")
        return f"🏆 {definition['name']}: " + " | ".join(entries)

    def _new_game(self, game_key: str) -> GameTurn:
        if game_key == "lemonade":
            return self._new_lemonade()
        if game_key == "blackjack":
            return self._new_blackjack()
        return self._new_mastermind()

    async def _send(self, message, texts: List[str]) -> bool:
        chunks = self._chunks(texts)
        return await self.bot.command_manager.send_response_chunked(
            message, chunks, skip_user_rate_limit_first=True
        )

    def _chunks(self, texts: List[str]) -> List[str]:
        words = " ".join(text.strip() for text in texts if text.strip()).split()
        if not words:
            return []
        chunks: List[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate.encode("utf-8")) <= self.chunk_bytes:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = word
            if len(chunks) >= self.max_chunks:
                break
        if current and len(chunks) < self.max_chunks:
            chunks.append(current)
        if len(chunks) == self.max_chunks:
            consumed = " ".join(chunks)
            original = " ".join(words)
            if len(consumed) < len(original):
                suffix = "…"
                while chunks[-1] and len(
                    (chunks[-1] + suffix).encode("utf-8")
                ) > self.chunk_bytes:
                    chunks[-1] = chunks[-1][:-1]
                chunks[-1] = chunks[-1].rstrip() + suffix
        return chunks

    async def handle_message(self, message) -> bool:
        """Handle game commands or an active game turn; return whether consumed."""
        if not self.enabled or not message.is_dm:
            return False
        self._purge_expired()
        content = (message.content or "").strip()
        lowered = content.lower()
        public_key = self._player_key(message)
        player_name = (message.sender_id or "Player").strip()
        enabled = self._enabled_games()
        session = self._get_session(public_key)

        if lowered in {"games", "game", "game help"}:
            await self._send(message, [self._menu()])
            return True
        if lowered.startswith("games top ") or lowered.startswith("game top "):
            requested = lowered.split(maxsplit=2)[-1]
            game_key = self._resolve_game(requested, GAME_DEFINITIONS)
            await self._send(
                message,
                [self._leaderboard(game_key) if game_key else self._menu()],
            )
            return True
        if lowered in {"game stop", "game quit", "game end"}:
            if session:
                self._delete_session(public_key)
                await self._send(message, ["Game stopped. Send games whenever you want another go."])
            else:
                await self._send(message, ["You don't have an active game. Send games to see the list."])
            return True
        if lowered in {"game status", "game resend"}:
            if not session:
                await self._send(message, ["You don't have an active game. Send games to start one."])
            else:
                await self._send(
                    message,
                    [self._status(session["game_key"], session["state"])],
                )
            return True

        start_text = lowered[5:].strip() if lowered.startswith("game ") else lowered
        requested_game = self._resolve_game(start_text, enabled)
        known_game = self._resolve_game(start_text, GAME_DEFINITIONS)
        if known_game and known_game not in enabled:
            await self._send(
                message,
                [f"{GAME_DEFINITIONS[known_game]['name']} is currently disabled."],
            )
            return True
        if requested_game:
            if session:
                await self._send(
                    message,
                    [f"You already have {GAME_DEFINITIONS[session['game_key']]['name']} open. Send game stop first."],
                )
                return True
            turn = self._new_game(requested_game)
            self._save_session(public_key, player_name, requested_game, turn.state or {})
            await self._send(message, turn.messages)
            return True

        if not session:
            return False

        manager = self.bot.command_manager
        detector = getattr(manager, "is_command_trigger", None)
        if detector and detector(content):
            return False

        game_key = session["game_key"]
        if game_key not in enabled:
            self._delete_session(public_key)
            await self._send(message, ["That game has been disabled. Your session was closed."])
            return True
        turn = self._play(game_key, session["state"], content)
        if turn.state is None:
            self._delete_session(public_key)
        else:
            self._save_session(public_key, player_name, game_key, turn.state)
        if turn.score is not None:
            self._save_score(
                public_key,
                player_name,
                game_key,
                turn.score,
                turn.summary or "",
            )
        await self._send(message, turn.messages)
        return True

    def _play(self, game_key: str, state: dict, content: str) -> GameTurn:
        if game_key == "lemonade":
            return self._play_lemonade(state, content)
        if game_key == "blackjack":
            return self._play_blackjack(state, content)
        return self._play_mastermind(state, content)

    def _status(self, game_key: str, state: dict) -> str:
        if game_key == "lemonade":
            return self._lemonade_prompt(state)
        if game_key == "blackjack":
            return self._blackjack_prompt(state)
        return self._mastermind_prompt(state)

    # Lemonade Stand -------------------------------------------------

    @staticmethod
    def _lemonade_week(state: dict) -> None:
        weather = random.choice(
            [
                ("Sunny", "☀️", random.randint(24, 31), 65),
                ("Warm", "🌤️", random.randint(20, 26), 50),
                ("Cloudy", "☁️", random.randint(16, 22), 35),
                ("Showers", "🌦️", random.randint(13, 19), 22),
            ]
        )
        state.update(
            {
                "phase": "cups",
                "weather": weather[0],
                "weather_icon": weather[1],
                "temperature": weather[2],
                "potential": weather[3] + random.randint(-5, 8),
                "cups_cost": round(random.uniform(2.25, 3.25), 2),
                "lemons_cost": round(random.uniform(3.50, 5.00), 2),
                "sugar_cost": round(random.uniform(2.50, 4.00), 2),
            }
        )

    def _new_lemonade(self) -> GameTurn:
        state = {
            "week": 1,
            "cash": 30.0,
            "start_cash": 30.0,
            "cups": 0,
            "lemon_servings": 0,
            "sugar_servings": 0,
        }
        self._lemonade_week(state)
        return GameTurn([self._lemonade_prompt(state)], state)

    @staticmethod
    def _money(value: float) -> str:
        return f"${value:.2f}"

    def _lemonade_prompt(self, state: dict) -> str:
        phase = state["phase"]
        head = (
            f"🍋 Week {state['week']}/7 {state['weather_icon']}{state['weather']} "
            f"{state['temperature']}°C. Cash {self._money(state['cash'])}; "
            f"stock cups:{state['cups']} lemons:{state['lemon_servings']} sugar:{state['sugar_servings']}."
        )
        prompts = {
            "cups": f"Cup packs cost {self._money(state['cups_cost'])} for 25. How many packs? (0+)",
            "lemons": f"Lemon baskets cost {self._money(state['lemons_cost'])} for 24 servings. How many?",
            "sugar": f"Sugar bags cost {self._money(state['sugar_cost'])} for 20 servings. How many?",
            "price": "Selling price per cup? e.g. 1.25",
            "continue": "Send c for next week or e to finish.",
        }
        return f"{head} {prompts.get(phase, '')}".strip()

    @staticmethod
    def _whole_number(content: str) -> Optional[int]:
        if not re.fullmatch(r"\d{1,3}", content.strip()):
            return None
        return int(content)

    def _buy_supply(
        self, state: dict, content: str, cost_key: str, stock_key: str, units: int
    ) -> Tuple[bool, str]:
        quantity = self._whole_number(content)
        if quantity is None:
            return False, "Please send a whole number, including 0."
        total = round(quantity * state[cost_key], 2)
        if total > state["cash"] + 0.001:
            return False, f"That costs {self._money(total)} but you have {self._money(state['cash'])}."
        state["cash"] = round(state["cash"] - total, 2)
        state[stock_key] += quantity * units
        return True, f"Bought {quantity}; cash {self._money(state['cash'])}."

    def _play_lemonade(self, state: dict, content: str) -> GameTurn:
        phase = state["phase"]
        if phase == "cups":
            ok, note = self._buy_supply(state, content, "cups_cost", "cups", 25)
            if ok:
                state["phase"] = "lemons"
            return GameTurn([note, self._lemonade_prompt(state)], state)
        if phase == "lemons":
            ok, note = self._buy_supply(
                state, content, "lemons_cost", "lemon_servings", 24
            )
            if ok:
                state["phase"] = "sugar"
            return GameTurn([note, self._lemonade_prompt(state)], state)
        if phase == "sugar":
            ok, note = self._buy_supply(
                state, content, "sugar_cost", "sugar_servings", 20
            )
            if ok:
                state["phase"] = "price"
            return GameTurn([note, self._lemonade_prompt(state)], state)
        if phase == "price":
            try:
                price = round(float(content.strip().replace("$", "")), 2)
            except ValueError:
                price = 0
            if price < 0.10 or price > 10:
                return GameTurn(["Send a price from $0.10 to $10.00."], state)
            supply = min(
                state["cups"], state["lemon_servings"], state["sugar_servings"]
            )
            price_factor = max(0.08, min(1.35, 1.7 - (price / 1.25)))
            demand = max(
                0,
                round(
                    state["potential"]
                    * price_factor
                    * random.uniform(0.85, 1.15)
                ),
            )
            sold = min(supply, demand)
            revenue = round(sold * price, 2)
            state["cash"] = round(state["cash"] + revenue, 2)
            state["cups"] -= sold
            state["lemon_servings"] -= sold
            state["sugar_servings"] -= sold
            profit = round(state["cash"] - state["start_cash"], 2)
            state["phase"] = "continue"
            result = (
                f"📊 Sold {sold}/{demand} cups at {self._money(price)}; "
                f"revenue {self._money(revenue)}. Cash {self._money(state['cash'])}, "
                f"P&L {self._money(profit)}."
            )
            return GameTurn([result, self._lemonade_prompt(state)], state)
        if phase == "continue":
            if content.strip().lower() in {"e", "end", "q", "quit"}:
                profit = round(state["cash"] - state["start_cash"], 2)
                return GameTurn(
                    [f"🍋 Stand closed with {self._money(state['cash'])}; profit {self._money(profit)}."],
                    None,
                    state["cash"],
                    f"Week {state['week']}, profit {self._money(profit)}",
                )
            if content.strip().lower() not in {"c", "continue", "n", "next"}:
                return GameTurn(["Send c for the next week or e to finish."], state)
            if state["week"] >= 7:
                profit = round(state["cash"] - state["start_cash"], 2)
                return GameTurn(
                    [f"🏁 Summer finished! Cash {self._money(state['cash'])}; profit {self._money(profit)}."],
                    None,
                    state["cash"],
                    f"7 weeks, profit {self._money(profit)}",
                )
            state["week"] += 1
            self._lemonade_week(state)
            return GameTurn([self._lemonade_prompt(state)], state)
        return GameTurn(["Game state reset. Send game stop, then lemonade."], state)

    # Blackjack ------------------------------------------------------

    @staticmethod
    def _new_deck() -> List[str]:
        deck = [
            f"{rank}{suit}"
            for suit in "♠♥♦♣"
            for rank in ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        ]
        random.shuffle(deck)
        return deck

    def _new_blackjack(self) -> GameTurn:
        state = {
            "phase": "bet",
            "bankroll": 100,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "deck": [],
            "player": [],
            "dealer": [],
            "bet": 0,
        }
        return GameTurn([self._blackjack_prompt(state)], state)

    @staticmethod
    def _hand_value(hand: List[str]) -> int:
        total = 0
        aces = 0
        for card in hand:
            rank = card[:-1]
            if rank == "A":
                aces += 1
                total += 11
            elif rank in {"J", "Q", "K"}:
                total += 10
            else:
                total += int(rank)
        while total > 21 and aces:
            total -= 10
            aces -= 1
        return total

    def _blackjack_prompt(self, state: dict, reveal: bool = False) -> str:
        if state["phase"] == "bet":
            return f"🃏 Blackjack. Bankroll {state['bankroll']} chips. Send bet 1-{state['bankroll']}, top, or leave."
        player = " ".join(state["player"])
        if reveal:
            dealer = " ".join(state["dealer"])
            dealer_value = self._hand_value(state["dealer"])
        else:
            dealer = f"{state['dealer'][0]} ??"
            dealer_value = self._hand_value([state["dealer"][0]])
        return (
            f"You {player} [{self._hand_value(state['player'])}] | "
            f"Dealer {dealer} [{dealer_value}]. Hit, stand, double or forfeit?"
        )

    def _settle_blackjack(self, state: dict, outcome: str) -> GameTurn:
        bet = state["bet"]
        if outcome == "win":
            state["bankroll"] += bet
            state["wins"] += 1
            label = "You win"
        elif outcome == "blackjack":
            winnings = max(1, round(bet * 1.5))
            state["bankroll"] += winnings
            state["wins"] += 1
            label = f"Blackjack! +{winnings}"
        elif outcome == "tie":
            state["ties"] += 1
            label = "Push"
        else:
            state["bankroll"] -= bet
            state["losses"] += 1
            label = "Dealer wins"
        cards = self._blackjack_prompt(state, reveal=True)
        if state["bankroll"] <= 0:
            summary = f"W{state['wins']}/L{state['losses']}/T{state['ties']}"
            return GameTurn(
                [f"{cards} {label}. You're out of chips. {summary}"],
                None,
                0,
                summary,
            )
        state.update({"phase": "bet", "player": [], "dealer": [], "bet": 0})
        return GameTurn(
            [f"{cards} {label}. Bankroll {state['bankroll']}.", self._blackjack_prompt(state)],
            state,
        )

    def _dealer_finish(self, state: dict) -> GameTurn:
        while self._hand_value(state["dealer"]) < 17:
            state["dealer"].append(state["deck"].pop())
        player_value = self._hand_value(state["player"])
        dealer_value = self._hand_value(state["dealer"])
        if dealer_value > 21 or player_value > dealer_value:
            return self._settle_blackjack(state, "win")
        if player_value == dealer_value:
            return self._settle_blackjack(state, "tie")
        return self._settle_blackjack(state, "loss")

    def _play_blackjack(self, state: dict, content: str) -> GameTurn:
        action = content.strip().lower()
        if action in {"leave", "l", "quit", "end"}:
            summary = f"W{state['wins']}/L{state['losses']}/T{state['ties']}"
            return GameTurn(
                [f"Left the table with {state['bankroll']} chips. {summary}"],
                None,
                state["bankroll"],
                summary,
            )
        if action == "top":
            return GameTurn([self._leaderboard("blackjack")], state)
        if state["phase"] == "bet":
            bet = self._whole_number(action)
            if bet is None or bet < 1 or bet > state["bankroll"]:
                return GameTurn([f"Send a whole-number bet from 1 to {state['bankroll']}."], state)
            state["deck"] = self._new_deck()
            state["bet"] = bet
            state["player"] = [state["deck"].pop(), state["deck"].pop()]
            state["dealer"] = [state["deck"].pop(), state["deck"].pop()]
            state["phase"] = "play"
            player_value = self._hand_value(state["player"])
            dealer_value = self._hand_value(state["dealer"])
            if player_value == 21:
                return self._settle_blackjack(
                    state, "tie" if dealer_value == 21 else "blackjack"
                )
            return GameTurn([self._blackjack_prompt(state)], state)
        if action in {"h", "hit"}:
            state["player"].append(state["deck"].pop())
            if self._hand_value(state["player"]) > 21:
                return self._settle_blackjack(state, "loss")
            return GameTurn([self._blackjack_prompt(state)], state)
        if action in {"s", "stand"}:
            return self._dealer_finish(state)
        if action in {"d", "double"}:
            if state["bankroll"] < state["bet"] * 2 or len(state["player"]) != 2:
                return GameTurn(["You can't double this hand. Hit or stand."], state)
            state["bet"] *= 2
            state["player"].append(state["deck"].pop())
            if self._hand_value(state["player"]) > 21:
                return self._settle_blackjack(state, "loss")
            return self._dealer_finish(state)
        if action in {"f", "forfeit", "surrender"}:
            state["bet"] = max(1, (state["bet"] + 1) // 2)
            return self._settle_blackjack(state, "loss")
        return GameTurn(["Send hit, stand, double, forfeit, or game stop."], state)

    # Mastermind -----------------------------------------------------

    def _new_mastermind(self) -> GameTurn:
        state = {"phase": "difficulty", "turn": 0}
        return GameTurn([self._mastermind_prompt(state)], state)

    def _mastermind_prompt(self, state: dict) -> str:
        if state["phase"] == "difficulty":
            return "🧠 Mastermind: choose normal (N: RYGB), hard (H: +OP), or expert (X: +WK)."
        return (
            f"Guess 4 letters from {state['palette']} "
            f"({state['turn']}/10 used). Example RYGB; or leave."
        )

    @staticmethod
    def _mastermind_feedback(secret: str, guess: str) -> Tuple[int, int]:
        exact = sum(left == right for left, right in zip(secret, guess))
        remaining_secret = [
            secret[index] for index in range(4) if secret[index] != guess[index]
        ]
        remaining_guess = [
            guess[index] for index in range(4) if secret[index] != guess[index]
        ]
        colour = 0
        for letter in remaining_guess:
            if letter in remaining_secret:
                colour += 1
                remaining_secret.remove(letter)
        return exact, colour

    def _play_mastermind(self, state: dict, content: str) -> GameTurn:
        action = content.strip().upper()
        if action.lower() in {"leave", "quit", "end"}:
            return GameTurn(["Mastermind ended."], None)
        if state["phase"] == "difficulty":
            palettes = {"N": "RYGB", "H": "RYGBOP", "X": "RYGBOPWK"}
            aliases = {"NORMAL": "N", "HARD": "H", "EXPERT": "X"}
            action = aliases.get(action, action)
            if action not in palettes:
                return GameTurn([self._mastermind_prompt(state)], state)
            palette = palettes[action]
            state.update(
                {
                    "phase": "playing",
                    "difficulty": action,
                    "palette": palette,
                    "secret": "".join(random.choice(palette) for _ in range(4)),
                    "turn": 0,
                }
            )
            return GameTurn([self._mastermind_prompt(state)], state)
        if not re.fullmatch(r"[A-Z]{4}", action) or any(
            letter not in state["palette"] for letter in action
        ):
            return GameTurn([self._mastermind_prompt(state)], state)
        state["turn"] += 1
        exact, colour = self._mastermind_feedback(state["secret"], action)
        if exact == 4:
            return GameTurn(
                [f"🏆 Correct: {state['secret']} in {state['turn']} guesses!"],
                None,
                state["turn"],
                f"Difficulty {state['difficulty']}",
            )
        if state["turn"] >= 10:
            return GameTurn(
                [f"Out of guesses—the code was {state['secret']}. Better luck next time!"],
                None,
            )
        return GameTurn(
            [f"{action}: {exact} exact, {colour} right colour/wrong place.", self._mastermind_prompt(state)],
            state,
        )
