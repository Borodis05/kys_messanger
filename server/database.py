import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    password_hash: str


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    display_name: str
    created_by: str


@dataclass(frozen=True)
class ChannelSummary:
    name: str
    display_name: str
    is_member: bool
    member_count: int


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(username),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_members (
                    channel_id INTEGER NOT NULL REFERENCES channels(id)
                        ON DELETE CASCADE,
                    username TEXT NOT NULL REFERENCES users(username)
                        ON DELETE CASCADE,
                    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (channel_id, username)
                )
                """
            )
            connection.commit()

    def create_user(
        self, username: str, display_name: str, password_hash: str
    ) -> Optional[User]:
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO users (username, display_name, password_hash)
                    VALUES (?, ?, ?)
                    """,
                    (username, display_name, password_hash),
                )
                user_id = cursor.lastrowid
                connection.commit()
        except sqlite3.IntegrityError:
            return None

        return User(user_id, username, display_name, password_hash)

    def get_user(self, username: str) -> Optional[User]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, username, display_name, password_hash
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

        if row is None:
            return None
        return User(
            row["id"], row["username"], row["display_name"], row["password_hash"]
        )

    def create_channel(
        self, name: str, display_name: str, created_by: str
    ) -> Optional[Channel]:
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO channels (name, display_name, created_by)
                    VALUES (?, ?, ?)
                    """,
                    (name, display_name, created_by),
                )
                channel_id = cursor.lastrowid
                connection.execute(
                    """
                    INSERT INTO channel_members (channel_id, username)
                    VALUES (?, ?)
                    """,
                    (channel_id, created_by),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            return None

        return Channel(channel_id, name, display_name, created_by)

    def get_channel(self, name: str) -> Optional[Channel]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, name, display_name, created_by
                FROM channels
                WHERE name = ?
                """,
                (name,),
            ).fetchone()

        if row is None:
            return None
        return Channel(row["id"], row["name"], row["display_name"], row["created_by"])

    def list_channels(self, username: str) -> List[ChannelSummary]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    c.name,
                    c.display_name,
                    EXISTS (
                        SELECT 1
                        FROM channel_members own_membership
                        WHERE own_membership.channel_id = c.id
                          AND own_membership.username = ?
                    ) AS is_member,
                    COUNT(all_members.username) AS member_count
                FROM channels c
                LEFT JOIN channel_members all_members ON all_members.channel_id = c.id
                GROUP BY c.id
                ORDER BY c.display_name COLLATE NOCASE
                """,
                (username,),
            ).fetchall()

        return [
            ChannelSummary(
                row["name"],
                row["display_name"],
                bool(row["is_member"]),
                row["member_count"],
            )
            for row in rows
        ]

    def join_channel(self, name: str, username: str) -> str:
        with closing(self._connect()) as connection:
            channel = connection.execute(
                "SELECT id FROM channels WHERE name = ?", (name,)
            ).fetchone()
            if channel is None:
                return "not_found"

            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO channel_members (channel_id, username)
                VALUES (?, ?)
                """,
                (channel["id"], username),
            )
            connection.commit()
            return "joined" if cursor.rowcount else "already_member"

    def leave_channel(self, name: str, username: str) -> str:
        with closing(self._connect()) as connection:
            channel = connection.execute(
                "SELECT id FROM channels WHERE name = ?", (name,)
            ).fetchone()
            if channel is None:
                return "not_found"

            cursor = connection.execute(
                """
                DELETE FROM channel_members
                WHERE channel_id = ? AND username = ?
                """,
                (channel["id"], username),
            )
            connection.commit()
            return "left" if cursor.rowcount else "not_member"

    def is_channel_member(self, name: str, username: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM channel_members cm
                JOIN channels c ON c.id = cm.channel_id
                WHERE c.name = ? AND cm.username = ?
                """,
                (name, username),
            ).fetchone()
        return row is not None

    def list_channel_members(self, name: str) -> List[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT cm.username
                FROM channel_members cm
                JOIN channels c ON c.id = cm.channel_id
                WHERE c.name = ?
                """,
                (name,),
            ).fetchall()
        return [row["username"] for row in rows]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
