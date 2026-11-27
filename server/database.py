import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


@dataclass(frozen=True)
class Message:
    id: int
    kind: str
    sender_username: str
    sender_display_name: str
    recipient_username: Optional[str]
    recipient_display_name: Optional[str]
    channel_name: Optional[str]
    channel_display_name: Optional[str]
    text: str
    created_at: str


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
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
                CREATE TABLE IF NOT EXISTS chat_read_state (
                    username TEXT NOT NULL REFERENCES users(username)
                        ON DELETE CASCADE,
                    chat_type TEXT NOT NULL
                        CHECK (chat_type IN ('general', 'private', 'channel')),
                    target TEXT NOT NULL,
                    last_read_message_id INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (username, chat_type, target)
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK (kind IN ('general', 'private', 'channel')),
                    sender_username TEXT NOT NULL REFERENCES users(username),
                    recipient_username TEXT REFERENCES users(username),
                    channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_kind_id "
                "ON messages(kind, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_private "
                "ON messages(sender_username, recipient_username, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_channel "
                "ON messages(channel_id, id)"
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

    def save_message(
        self,
        kind: str,
        sender_username: str,
        text: str,
        created_at: str,
        recipient_username: Optional[str] = None,
        channel_name: Optional[str] = None,
    ) -> int:
        with closing(self._connect()) as connection:
            channel_id = None
            if channel_name is not None:
                channel = connection.execute(
                    "SELECT id FROM channels WHERE name = ?", (channel_name,)
                ).fetchone()
                if channel is None:
                    raise ValueError("Channel does not exist")
                channel_id = channel["id"]

            cursor = connection.execute(
                """
                INSERT INTO messages (
                    kind,
                    sender_username,
                    recipient_username,
                    channel_id,
                    text,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    sender_username,
                    recipient_username,
                    channel_id,
                    text,
                    created_at,
                ),
            )
            connection.commit()
            return cursor.lastrowid

    def get_general_history(self, limit: int) -> List[Message]:
        return self._get_history("m.kind = 'general'", (), limit)

    def get_private_history(
        self, first_username: str, second_username: str, limit: int
    ) -> List[Message]:
        return self._get_history(
            """
            m.kind = 'private'
            AND (
                (m.sender_username = ? AND m.recipient_username = ?)
                OR
                (m.sender_username = ? AND m.recipient_username = ?)
            )
            """,
            (first_username, second_username, second_username, first_username),
            limit,
        )

    def get_channel_history(self, channel_name: str, limit: int) -> List[Message]:
        return self._get_history(
            "m.kind = 'channel' AND c.name = ?", (channel_name,), limit
        )

    def list_private_contacts(self, username: str) -> List[User]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    u.id,
                    u.username,
                    u.display_name,
                    u.password_hash
                FROM users u
                JOIN messages m ON u.username = CASE
                    WHEN m.sender_username = ? THEN m.recipient_username
                    ELSE m.sender_username
                END
                WHERE m.kind = 'private'
                  AND (m.sender_username = ? OR m.recipient_username = ?)
                ORDER BY u.display_name COLLATE NOCASE
                """,
                (username, username, username),
            ).fetchall()

        return [
            User(row["id"], row["username"], row["display_name"], row["password_hash"])
            for row in rows
        ]

    def get_read_marker(self, username: str, chat_type: str, target: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT last_read_message_id
                FROM chat_read_state
                WHERE username = ? AND chat_type = ? AND target = ?
                """,
                (username, chat_type, target),
            ).fetchone()
        return row["last_read_message_id"] if row is not None else 0

    def mark_chat_read(
        self, username: str, chat_type: str, target: str, message_id: int
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO chat_read_state (
                    username, chat_type, target, last_read_message_id
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username, chat_type, target) DO UPDATE SET
                    last_read_message_id = MAX(
                        chat_read_state.last_read_message_id,
                        excluded.last_read_message_id
                    )
                """,
                (username, chat_type, target, message_id),
            )
            connection.commit()

    def get_unread_counts(self, username: str) -> Dict[Tuple[str, str], int]:
        counts: Dict[Tuple[str, str], int] = {}
        with closing(self._connect()) as connection:
            general = connection.execute(
                """
                SELECT COUNT(*) AS unread_count
                FROM messages m
                WHERE m.kind = 'general'
                  AND m.sender_username != ?
                  AND m.id > COALESCE((
                      SELECT last_read_message_id
                      FROM chat_read_state
                      WHERE username = ?
                        AND chat_type = 'general'
                        AND target = 'general'
                  ), 0)
                """,
                (username, username),
            ).fetchone()
            counts[("general", "general")] = general["unread_count"]

            private_rows = connection.execute(
                """
                SELECT m.sender_username AS target, COUNT(*) AS unread_count
                FROM messages m
                WHERE m.kind = 'private'
                  AND m.recipient_username = ?
                  AND m.id > COALESCE((
                      SELECT last_read_message_id
                      FROM chat_read_state
                      WHERE username = ?
                        AND chat_type = 'private'
                        AND target = m.sender_username
                  ), 0)
                GROUP BY m.sender_username
                """,
                (username, username),
            ).fetchall()
            for row in private_rows:
                counts[("private", row["target"])] = row["unread_count"]

            channel_rows = connection.execute(
                """
                SELECT c.name AS target, COUNT(m.id) AS unread_count
                FROM channel_members cm
                JOIN channels c ON c.id = cm.channel_id
                LEFT JOIN messages m
                    ON m.channel_id = c.id
                   AND m.kind = 'channel'
                   AND m.sender_username != ?
                   AND m.id > COALESCE((
                       SELECT last_read_message_id
                       FROM chat_read_state
                       WHERE username = ?
                         AND chat_type = 'channel'
                         AND target = c.name
                   ), 0)
                WHERE cm.username = ?
                GROUP BY c.id
                """,
                (username, username, username),
            ).fetchall()
            for row in channel_rows:
                counts[("channel", row["target"])] = row["unread_count"]

        return counts

    def _get_history(
        self, where_clause: str, parameters: tuple, limit: int
    ) -> List[Message]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT
                    m.id,
                    m.kind,
                    m.sender_username,
                    sender.display_name AS sender_display_name,
                    m.recipient_username,
                    recipient.display_name AS recipient_display_name,
                    c.name AS channel_name,
                    c.display_name AS channel_display_name,
                    m.text,
                    m.created_at
                FROM messages m
                JOIN users sender ON sender.username = m.sender_username
                LEFT JOIN users recipient
                    ON recipient.username = m.recipient_username
                LEFT JOIN channels c ON c.id = m.channel_id
                WHERE {where_clause}
                ORDER BY m.id DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()

        return [
            Message(
                row["id"],
                row["kind"],
                row["sender_username"],
                row["sender_display_name"],
                row["recipient_username"],
                row["recipient_display_name"],
                row["channel_name"],
                row["channel_display_name"],
                row["text"],
                row["created_at"],
            )
            for row in reversed(rows)
        ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
