import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class StateError(ValueError):
    """Fixed, credential-free diagnostic safe for logs."""


class Store:
    """Only encrypted credentials, update offsets and message IDs go on disk."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        key_path = directory / "token.key"
        db_path = directory / "bot.sqlite3"
        if not key_path.exists():
            # Never silently replace a lost key for an existing database.
            if db_path.exists():
                raise StateError("token.key is missing; restore it together with bot.sqlite3")
            with key_path.open("xb") as f:
                os.chmod(key_path, 0o600)
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes().strip())
        self.db = sqlite3.connect(db_path)
        os.chmod(db_path, 0o600)
        self.db.executescript("""
            PRAGMA secure_delete = ON;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, token BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                created REAL NOT NULL, delete_at REAL NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );
            PRAGMA user_version = 1;
        """)

    def close(self):
        self.db.close()

    def token(self, user_id):
        row = self.db.execute("SELECT token FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        try:
            return self.cipher.decrypt(row[0]).decode()
        except InvalidToken:
            raise StateError("Cannot decrypt Anime365 token; restore matching token.key") from None

    def save_token(self, user_id, token):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO users VALUES (?, ?)",
                            (user_id, self.cipher.encrypt(token.encode())))

    def forget_token(self, user_id):
        with self.db:
            self.db.execute("DELETE FROM users WHERE user_id=?", (user_id,))

    def get(self, name, default=""):
        row = self.db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set(self, name, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, str(value)))

    def track(self, chat_id, message_id, ttl=0, created=None):
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?)",
                            (chat_id, message_id, created or now, now + ttl))

    def due(self, all_messages=False):
        return self.db.execute(
            "SELECT chat_id, message_id, created FROM messages WHERE delete_at <= ? ORDER BY delete_at LIMIT 100",
            (float("inf") if all_messages else time.time(),),
        ).fetchall()

    def untrack(self, chat_id, message_id):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message_id))

    def retry_delete(self, chat_id, message_id, delay):
        with self.db:
            self.db.execute("UPDATE messages SET delete_at=? WHERE chat_id=? AND message_id=?",
                            (time.time() + delay, chat_id, message_id))
