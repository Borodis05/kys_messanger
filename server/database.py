import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class User:
    id: int
    username: str
    display_name: str
    password_hash: str


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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection
