"""yume_db.py

SQLite helper for YumeBot.

Design goals
- Boring and predictable.
- Single file DB at config.YUME_DB_FILE.
- Safe with asyncio (opens a new connection per operation).
- Light migrations only (additive tables/columns).

Schema versions
v1: user_settings, world_state
v2: bot_config, daily_rules, rule_suggestions
v3: daily_meals
v4: stamps opt-in + rewards/events logs
v5: Abydos mini-game economy (debt/interest + exploration)
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from config import YUME_DB_FILE


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(YUME_DB_FILE), exist_ok=True)
    con = sqlite3.connect(
        YUME_DB_FILE,
        timeout=10,
        isolation_level=None,  # autocommit; we manage transactions explicitly
        check_same_thread=False,
    )
    con.row_factory = sqlite3.Row

    # Pragmas (safe defaults)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = _connect()
    try:
        yield con
    finally:
        try:
            con.close()
        except Exception:
            pass


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE transaction to avoid writer starvation."""

    with connect() as con:
        con.execute("BEGIN IMMEDIATE;")
        try:
            yield con
            con.execute("COMMIT;")
        except Exception:
            con.execute("ROLLBACK;")
            raise


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with connect() as con:
        cur = con.execute(sql, params)
        return int(cur.rowcount)


def executemany(sql: str, seq_of_params: Iterable[Sequence[Any]]) -> int:
    with connect() as con:
        cur = con.executemany(sql, seq_of_params)
        return int(cur.rowcount)


def fetchone(sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
    with connect() as con:
        cur = con.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None


def fetchall(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with connect() as con:
        cur = con.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def init_db() -> None:
    """Create tables / apply light migrations.

    We keep migrations intentionally simple:
    - Only additive changes (new tables / new columns)
    - Schema version tracked via schema_meta('schema_version')
    """

    now = int(time.time())

    SCHEMA_VERSION = 5

    with transaction() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )

        # Current schema version
        ver_row = con.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version';"
        ).fetchone()
        try:
            current_version = int(ver_row[0]) if ver_row is not None else 0
        except Exception:
            current_version = 0

        def _add_column(table: str, col_def: str) -> None:
            """Add a column if missing (idempotent)."""

            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col_def};")
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    return
                raise

        # ===== v1 =====
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
              user_id INTEGER PRIMARY KEY,
              dm_opt_in INTEGER NOT NULL DEFAULT 1,
              noise_opt_in INTEGER NOT NULL DEFAULT 1,
              stamps INTEGER NOT NULL DEFAULT 0,
              last_stamp_at INTEGER NOT NULL DEFAULT 0,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS world_state (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              weather TEXT NOT NULL,
              weather_changed_at INTEGER NOT NULL,
              weather_next_change_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )

        # Ensure singleton row
        row = con.execute("SELECT id FROM world_state WHERE id=1;").fetchone()
        if row is None:
            # Phase0 default: clear weather; Phase1 will rotate it.
            con.execute(
                """
                INSERT INTO world_state(id, weather, weather_changed_at, weather_next_change_at, updated_at)
                VALUES(1, ?, ?, ?, ?);
                """,
                ("clear", now, now + 6 * 3600, now),
            )

        # ===== v2 =====
        if current_version < 2:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_config (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_rules (
                  date TEXT PRIMARY KEY,              -- YYYY-MM-DD (KST)
                  rule_no INTEGER NOT NULL,
                  rule_text TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  posted_channel_id INTEGER,
                  posted_at INTEGER,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT
                );
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS rule_suggestions (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  guild_id INTEGER,
                  content TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                """
            )

        # ===== v3 =====
        if current_version < 3:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_meals (
                  date TEXT PRIMARY KEY,          -- YYYY-MM-DD (KST)
                  meal_text TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  last_requested_at INTEGER NOT NULL
                );
                """
            )

        # ===== v4 =====
        if current_version < 4:
            _add_column("user_settings", "stamps_opt_in INTEGER NOT NULL DEFAULT 1")
            _add_column("user_settings", "stamps_rewarded INTEGER NOT NULL DEFAULT 0")
            _add_column("user_settings", "stamp_title TEXT NOT NULL DEFAULT ''")
            _add_column("user_settings", "last_reward_at INTEGER NOT NULL DEFAULT 0")

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS stamp_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  guild_id INTEGER,
                  reason TEXT,
                  delta INTEGER NOT NULL,
                  stamps_after INTEGER NOT NULL,
                  created_at INTEGER NOT NULL
                );
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS stamp_rewards (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  guild_id INTEGER,
                  milestone INTEGER NOT NULL,
                  title TEXT NOT NULL,
                  letter_text TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                );
                """
            )



        # ===== v5 =====
        if current_version < 5:
            # Fix older v4 schema where stamp_rewards used column name `letter`.
            try:
                _add_column("stamp_rewards", "letter_text TEXT NOT NULL DEFAULT ''")
                # If the old column exists, copy it over once.
                try:
                    con.execute(
                        "UPDATE stamp_rewards SET letter_text = letter WHERE (letter_text='' OR letter_text IS NULL) AND letter IS NOT NULL;"
                    )
                except Exception:
                    pass
            except Exception:
                pass

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS aby_user_economy (
                  user_id INTEGER PRIMARY KEY,
                  credits INTEGER NOT NULL DEFAULT 0,
                  water INTEGER NOT NULL DEFAULT 0,
                  last_explore_ymd TEXT NOT NULL DEFAULT '',
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS aby_guild_debt (
                  guild_id INTEGER PRIMARY KEY,
                  debt INTEGER NOT NULL,
                  interest_rate REAL NOT NULL,
                  last_interest_ymd TEXT NOT NULL,
                  created_at INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                );
                """
            )

            con.execute(
                """
                CREATE TABLE IF NOT EXISTS aby_economy_log (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  guild_id INTEGER,
                  user_id INTEGER,
                  kind TEXT NOT NULL,
                  delta_credits INTEGER NOT NULL DEFAULT 0,
                  delta_water INTEGER NOT NULL DEFAULT 0,
                  delta_debt INTEGER NOT NULL DEFAULT 0,
                  memo TEXT,
                  created_at INTEGER NOT NULL
                );
                """
            )

            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_aby_econ_log_guild_time ON aby_economy_log(guild_id, created_at);"
            )
        con.execute(
            """
            INSERT INTO schema_meta(key, value, updated_at)
            VALUES('schema_version', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at;
            """,
            (str(SCHEMA_VERSION), now),
        )
