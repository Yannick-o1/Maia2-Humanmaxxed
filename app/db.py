"""SQLite storage: games, PGNs, ratings and cached reviews.

There is one rating row for the player and one for each of Maia-2's eleven
conditioning buckets. The buckets start at their nominal rating and are updated
after every rated game alongside the player's, so the bot's recorded strength
converges on what it actually plays like rather than what it is labelled.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import config
from .bot import bucket_count, bucket_label, bucket_nominal_rating
from .glicko2 import Rating

HUMAN_ID = "human"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ratings (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    rating      REAL NOT NULL,
    rd          REAL NOT NULL,
    volatility  REAL NOT NULL,
    games       INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS games (
    id                  TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    finished_at         TEXT,
    rated               INTEGER NOT NULL,
    human_color         TEXT NOT NULL,
    time_initial        INTEGER NOT NULL,
    time_increment      INTEGER NOT NULL,
    temperature         REAL NOT NULL,
    bucket_index        INTEGER NOT NULL,
    bucket_label        TEXT NOT NULL,
    conditioning_elo    INTEGER NOT NULL,
    result              TEXT,
    reason              TEXT,
    status_text         TEXT,
    ply_count           INTEGER NOT NULL DEFAULT 0,
    pgn                 TEXT,
    human_rating_before REAL,
    human_rd_before     REAL,
    human_rating_after  REAL,
    human_rd_after      REAL,
    bot_rating_before   REAL,
    bot_rating_after    REAL
);

CREATE TABLE IF NOT EXISTS reviews (
    game_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.conn = connect(path)
        self.conn.executescript(SCHEMA)
        self._seed_ratings()

    def close(self) -> None:
        self.conn.close()

    # --- ratings ----------------------------------------------------------
    def _seed_ratings(self) -> None:
        start = float(self.get_setting("start_rating", config.DEFAULT_START_RATING))
        start_rd = float(self.get_setting("start_rd", config.GLICKO_DEFAULT_RD))
        rows = [
            (
                HUMAN_ID,
                config.PLAYER_NAME,
                start,
                start_rd,
                config.GLICKO_DEFAULT_VOLATILITY,
            )
        ]
        for index in range(bucket_count()):
            rows.append(
                (
                    bucket_key(index),
                    bucket_label(index),
                    float(bucket_nominal_rating(index)),
                    config.GLICKO_DEFAULT_RD,
                    config.GLICKO_DEFAULT_VOLATILITY,
                )
            )
        self.conn.executemany(
            "INSERT OR IGNORE INTO ratings (id, label, rating, rd, volatility, games)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            rows,
        )
        self.conn.commit()

    def get_rating(self, rating_id: str) -> Rating:
        row = self.conn.execute(
            "SELECT rating, rd, volatility FROM ratings WHERE id = ?", (rating_id,)
        ).fetchone()
        if row is None:
            raise KeyError(rating_id)
        return Rating(row["rating"], row["rd"], row["volatility"])

    def get_games_played(self, rating_id: str) -> int:
        row = self.conn.execute("SELECT games FROM ratings WHERE id = ?", (rating_id,)).fetchone()
        return int(row["games"]) if row else 0

    def save_rating(self, rating_id: str, rating: Rating, played: bool = True) -> None:
        self.conn.execute(
            "UPDATE ratings SET rating = ?, rd = ?, volatility = ?,"
            " games = games + ?, updated_at = datetime('now') WHERE id = ?",
            (rating.rating, rating.rd, rating.volatility, 1 if played else 0, rating_id),
        )
        self.conn.commit()

    def all_ratings(self) -> list[dict[str, Any]]:
        """The player first, then the buckets in rating order.

        Sorting in SQL would order "bucket:10" before "bucket:2", so the
        numeric part is pulled out here instead.
        """
        rows = [dict(row) for row in self.conn.execute(
            "SELECT id, label, rating, rd, volatility, games FROM ratings"
        ).fetchall()]

        def order(row: dict[str, Any]) -> tuple[int, int]:
            if row["id"] == HUMAN_ID:
                return (0, 0)
            return (1, int(row["id"].split(":")[1]))

        return sorted(rows, key=order)

    def reset_human_rating(self, rating: float, rd: float) -> None:
        self.conn.execute(
            "UPDATE ratings SET rating = ?, rd = ?, volatility = ?, games = 0,"
            " updated_at = datetime('now') WHERE id = ?",
            (rating, rd, config.GLICKO_DEFAULT_VOLATILITY, HUMAN_ID),
        )
        self.conn.commit()

    # --- settings ---------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    def all_settings(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                out[row["key"]] = row["value"]
        return out

    # --- games ------------------------------------------------------------
    def save_game(self, record: dict[str, Any]) -> None:
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        updates = ", ".join(f"{key} = :{key}" for key in record if key != "id")
        self.conn.execute(
            f"INSERT INTO games ({columns}) VALUES ({placeholders})"
            f" ON CONFLICT(id) DO UPDATE SET {updates}",
            record,
        )
        self.conn.commit()

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        return dict(row) if row else None

    def list_games(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM games ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # --- reviews ----------------------------------------------------------
    def save_review(self, game_id: str, data: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO reviews (game_id, created_at, data)"
            " VALUES (?, datetime('now'), ?)"
            " ON CONFLICT(game_id) DO UPDATE SET data = excluded.data,"
            " created_at = excluded.created_at",
            (game_id, json.dumps(data)),
        )
        self.conn.commit()

    def get_review(self, game_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT data FROM reviews WHERE game_id = ?", (game_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else None


def bucket_key(index: int) -> str:
    return f"bucket:{index}"
