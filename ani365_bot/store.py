import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


SCHEMA_VERSION = 2


class StateError(ValueError):
    """Fixed, credential-free diagnostic safe for logs."""


class Store:
    """Encrypted credentials and durable, non-secret bot state.

    The database is intentionally the only place with SQL. It is used from the
    single asyncio event-loop thread, so sqlite's normal synchronous connection
    is sufficient and keeps short state transitions atomic.
    """

    _WATCHLIST_COLUMNS = (
        "user_id", "series_id", "title", "year", "series_type", "added_at",
        "last_watched_episode_id", "last_watched_episode_number",
        "last_available_episode_id", "last_available_episode_number",
        "notifications_enabled", "notification_mode", "notification_baselined",
        "last_notified_episode_id", "last_notified_episode_number",
    )
    _NOTIFICATION_MODES = {
        "any": "any",
        "subtitle": "subtitles",
        "subtitles": "subtitles",
        "sub": "subtitles",
        "sub_ru": "subtitles",
        "ru_subtitles": "subtitles",
        "voice": "voice",
        "voice_ru": "voice",
        "ru_voice": "voice",
    }

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
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < 0 or version > SCHEMA_VERSION:
            raise StateError("bot.sqlite3 has an unsupported schema version")
        if version < 1:
            with self.db:
                self._create_v1()
                self.db.execute("PRAGMA user_version = 1")
            version = 1
        if version < 2:
            with self.db:
                self._create_v2()
                self.db.execute("PRAGMA user_version = 2")

    def _create_v1(self):
        """Initial schema, kept idempotent for pre-versioned installations."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, token BLOB NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                created REAL NOT NULL, delete_at REAL NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            )
        """)

    def _create_v2(self):
        """Add multi-user watch state without changing or deleting v1 data."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id INTEGER PRIMARY KEY,
                added_at REAL NOT NULL
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS user_state (
                user_id INTEGER PRIMARY KEY,
                awaiting_token INTEGER NOT NULL DEFAULT 0
                    CHECK(awaiting_token IN (0, 1))
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                user_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                year TEXT,
                series_type TEXT,
                added_at REAL NOT NULL,
                last_watched_episode_id INTEGER,
                last_watched_episode_number TEXT,
                last_available_episode_id INTEGER,
                last_available_episode_number TEXT,
                notifications_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK(notifications_enabled IN (0, 1)),
                notification_mode TEXT NOT NULL DEFAULT 'any',
                notification_baselined INTEGER NOT NULL DEFAULT 0
                    CHECK(notification_baselined IN (0, 1)),
                last_notified_episode_id INTEGER,
                last_notified_episode_number TEXT,
                PRIMARY KEY(user_id, series_id)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS watch_episode_state (
                user_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                episode_id INTEGER NOT NULL,
                episode_number TEXT,
                baseline INTEGER NOT NULL DEFAULT 0 CHECK(baseline IN (0, 1)),
                discovered_at REAL NOT NULL,
                candidate_retry_at REAL NOT NULL DEFAULT 0,
                candidate_attempts INTEGER NOT NULL DEFAULT 0,
                candidate_resolved_at REAL,
                PRIMARY KEY(user_id, series_id, episode_id),
                FOREIGN KEY(user_id, series_id)
                    REFERENCES watchlist(user_id, series_id) ON DELETE CASCADE
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                user_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                episode_id INTEGER NOT NULL,
                episode_number TEXT,
                mode TEXT NOT NULL,
                created_at REAL NOT NULL,
                retry_at REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                sent_at REAL,
                PRIMARY KEY(user_id, series_id, episode_id, mode),
                FOREIGN KEY(user_id, series_id)
                    REFERENCES watchlist(user_id, series_id) ON DELETE CASCADE
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS watchlist_notifications_idx
            ON watchlist(notifications_enabled, series_id)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS watch_episode_candidates_idx
            ON watch_episode_state(series_id, candidate_resolved_at, candidate_retry_at)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS notification_outbox_pending_idx
            ON notification_outbox(sent_at, retry_at)
        """)

    @staticmethod
    def _now(value):
        return time.time() if value is None else float(value)

    @staticmethod
    def _positive_id(value, name):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a positive integer") from None
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @classmethod
    def _mode(cls, mode):
        normalized = cls._NOTIFICATION_MODES.get(str(mode or "any").strip().lower())
        if not normalized:
            raise ValueError("Unknown notification mode")
        return normalized

    @classmethod
    def _episode(cls, episode, number=None):
        if isinstance(episode, dict):
            number = episode.get("episodeFull") or episode.get("episodeInt") or number
            episode = episode.get("id")
        episode_id = cls._positive_id(episode, "episode_id")
        return episode_id, None if number is None else str(number)

    @classmethod
    def _episodes(cls, episodes):
        result = []
        seen = set()
        for episode in episodes or ():
            episode_id, number = cls._episode(episode)
            if episode_id not in seen:
                result.append((episode_id, number))
                seen.add(episode_id)
        return result

    @staticmethod
    def _dicts(cursor):
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _watchlist_dict(self, row):
        if row is None:
            return None
        result = dict(zip(self._WATCHLIST_COLUMNS, row))
        result["notifications_enabled"] = bool(result["notifications_enabled"])
        result["notification_baselined"] = bool(result["notification_baselined"])
        # Friendly aliases make cached episode fields self-explanatory to callers.
        result["last_known_episode_id"] = result["last_available_episode_id"]
        result["last_known_episode_number"] = result["last_available_episode_number"]
        result["type"] = result["series_type"]
        return result

    def close(self):
        self.db.close()

    # Existing encrypted credential API.
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

    # Existing global bot settings API. Telegram's polling offset remains global.
    def get(self, name, default=""):
        row = self.db.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set(self, name, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, str(value)))

    # Existing temporary-message queue API.
    def track(self, chat_id, message_id, ttl=0, created=None):
        now = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?)",
                            (chat_id, message_id, created or now, now + ttl))

    def due(self, all_messages=False, chat_id=None):
        query = "SELECT chat_id, message_id, created FROM messages WHERE delete_at <= ?"
        params = [float("inf") if all_messages else time.time()]
        if chat_id is not None:
            query += " AND chat_id=?"
            params.append(chat_id)
        return self.db.execute(query + " ORDER BY delete_at LIMIT 100", params).fetchall()

    def untrack(self, chat_id, message_id):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE chat_id=? AND message_id=?", (chat_id, message_id))

    def retry_delete(self, chat_id, message_id, delay):
        with self.db:
            self.db.execute("UPDATE messages SET delete_at=? WHERE chat_id=? AND message_id=?",
                            (time.time() + delay, chat_id, message_id))

    # Access control. OWNER_ID is deliberately supplied by the caller rather
    # than copied into the database, so changing configuration cannot revoke it.
    def is_allowed(self, user_id, owner_id):
        user_id = self._positive_id(user_id, "user_id")
        owner_id = self._positive_id(owner_id, "owner_id")
        if user_id == owner_id:
            return True
        return self.db.execute("SELECT 1 FROM allowed_users WHERE user_id=?", (user_id,)).fetchone() is not None

    def add_allowed_user(self, user_id, owner_id=None, added_at=None):
        user_id = self._positive_id(user_id, "user_id")
        if owner_id is not None and user_id == self._positive_id(owner_id, "owner_id"):
            return False
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO allowed_users(user_id, added_at) VALUES (?, ?)",
                                     (user_id, self._now(added_at)))
        return cursor.rowcount > 0

    def revoke_allowed_user(self, user_id, owner_id):
        user_id = self._positive_id(user_id, "user_id")
        if user_id == self._positive_id(owner_id, "owner_id"):
            return False
        with self.db:
            cursor = self.db.execute("DELETE FROM allowed_users WHERE user_id=?", (user_id,))
            # Revocation must also stop background Telegram delivery. Keep the
            # encrypted token and saved list so a later explicit /allow can
            # restore access without treating the account as a new user.
            self.db.execute("DELETE FROM notification_outbox WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM watch_episode_state WHERE user_id=?", (user_id,))
            self.db.execute("UPDATE watchlist SET notifications_enabled=0, notification_baselined=0 "
                            "WHERE user_id=?", (user_id,))
        return cursor.rowcount > 0

    def list_allowed_users(self, owner_id):
        owner_id = self._positive_id(owner_id, "owner_id")
        rows = self._dicts(self.db.execute("SELECT user_id, added_at FROM allowed_users ORDER BY user_id"))
        result = [{"user_id": owner_id, "added_at": None, "is_owner": True}]
        result.extend({"user_id": row["user_id"], "added_at": row["added_at"], "is_owner": False}
                      for row in rows if row["user_id"] != owner_id)
        return sorted(result, key=lambda row: row["user_id"])

    # Short aliases keep command handlers readable.
    allow = add_allowed_user
    revoke = revoke_allowed_user
    allowed_users = list_allowed_users

    # Per-user, non-secret UI state. The old global settings row is retained
    # for compatibility and intentionally is not treated as another user's state.
    def awaiting_token(self, user_id):
        user_id = self._positive_id(user_id, "user_id")
        row = self.db.execute("SELECT awaiting_token FROM user_state WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row[0])

    def set_awaiting_token(self, user_id, awaiting):
        user_id = self._positive_id(user_id, "user_id")
        with self.db:
            self.db.execute("""
                INSERT INTO user_state(user_id, awaiting_token) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET awaiting_token=excluded.awaiting_token
            """, (user_id, int(bool(awaiting))))

    is_awaiting_token = awaiting_token

    # Watchlist records intentionally contain only display metadata and Anime365
    # numeric IDs; neither complete API responses nor signed media URLs reach disk.
    def add_watchlist(self, user_id, series_id, title, year=None, series_type=None, added_at=None):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        title = str(title or "Без названия")
        year = None if year is None else str(year)
        series_type = None if series_type is None else str(series_type)
        existing = self.get_watchlist(user_id, series_id)
        with self.db:
            if existing:
                self.db.execute("""
                    UPDATE watchlist
                    SET title=?, year=COALESCE(?, year), series_type=COALESCE(?, series_type)
                    WHERE user_id=? AND series_id=?
                """, (title, year, series_type, user_id, series_id))
            else:
                self.db.execute("""
                    INSERT INTO watchlist(
                        user_id, series_id, title, year, series_type, added_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (user_id, series_id, title, year, series_type, self._now(added_at)))
        result = self.get_watchlist(user_id, series_id)
        result["created"] = not bool(existing)
        return result

    def get_watchlist(self, user_id, series_id):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        columns = ", ".join(self._WATCHLIST_COLUMNS)
        row = self.db.execute(f"SELECT {columns} FROM watchlist WHERE user_id=? AND series_id=?",
                              (user_id, series_id)).fetchone()
        return self._watchlist_dict(row)

    watchlist_item = get_watchlist

    def list_watchlist(self, user_id):
        user_id = self._positive_id(user_id, "user_id")
        columns = ", ".join(self._WATCHLIST_COLUMNS)
        rows = self.db.execute(f"""
            SELECT {columns} FROM watchlist WHERE user_id=?
            ORDER BY added_at DESC, title COLLATE NOCASE, series_id
        """, (user_id,)).fetchall()
        return [self._watchlist_dict(row) for row in rows]

    watching = list_watchlist

    def has_watchlist(self, user_id, series_id):
        return self.get_watchlist(user_id, series_id) is not None

    def remove_watchlist(self, user_id, series_id):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        with self.db:
            cursor = self.db.execute("DELETE FROM watchlist WHERE user_id=? AND series_id=?",
                                     (user_id, series_id))
        return cursor.rowcount > 0

    def update_progress(self, user_id, series_id, episode, episode_number=None):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id, episode_number = self._episode(episode, episode_number)
        with self.db:
            cursor = self.db.execute("""
                UPDATE watchlist
                SET last_watched_episode_id=?, last_watched_episode_number=?
                WHERE user_id=? AND series_id=?
            """, (episode_id, episode_number, user_id, series_id))
        return self.get_watchlist(user_id, series_id) if cursor.rowcount else None

    def update_available(self, user_id, series_id, episode, episode_number=None):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id, episode_number = self._episode(episode, episode_number)
        with self.db:
            cursor = self.db.execute("""
                UPDATE watchlist
                SET last_available_episode_id=?, last_available_episode_number=?
                WHERE user_id=? AND series_id=?
            """, (episode_id, episode_number, user_id, series_id))
        return self.get_watchlist(user_id, series_id) if cursor.rowcount else None

    set_available = update_available

    def configure_notifications(self, user_id, series_id, enabled, mode="any", episodes=(), now=None):
        """Change a subscription and atomically establish its no-spam baseline.

        `episodes` must be the current Anime365 episode list obtained immediately
        before enabling or changing the mode. Existing episodes become baseline
        rows, so neither a restart nor a later watcher pass sends old releases.
        """
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        mode = self._mode(mode)
        enabled = bool(enabled)
        now = self._now(now)
        episodes = self._episodes(episodes)
        if not self.has_watchlist(user_id, series_id):
            return None
        latest = episodes[-1] if episodes else None
        with self.db:
            # A changed mode/disabled subscription must not deliver a stale
            # outbox item or leave an old candidate eligible.
            self.db.execute("DELETE FROM notification_outbox WHERE user_id=? AND series_id=?",
                            (user_id, series_id))
            self.db.execute("DELETE FROM watch_episode_state WHERE user_id=? AND series_id=?",
                            (user_id, series_id))
            if enabled:
                self.db.executemany("""
                    INSERT INTO watch_episode_state(
                        user_id, series_id, episode_id, episode_number, baseline, discovered_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                """, [(user_id, series_id, episode_id, number, now) for episode_id, number in episodes])
            self.db.execute("""
                UPDATE watchlist
                SET notifications_enabled=?, notification_mode=?, notification_baselined=?,
                    last_available_episode_id=COALESCE(?, last_available_episode_id),
                    last_available_episode_number=COALESCE(?, last_available_episode_number)
                WHERE user_id=? AND series_id=?
            """, (int(enabled), mode, int(enabled),
                  latest[0] if latest else None, latest[1] if latest else None, user_id, series_id))
        return self.get_watchlist(user_id, series_id)

    def notification_subscriptions(self):
        columns = ", ".join(self._WATCHLIST_COLUMNS)
        rows = self.db.execute(f"""
            SELECT {columns} FROM watchlist WHERE notifications_enabled=1
            ORDER BY series_id, user_id
        """).fetchall()
        return [self._watchlist_dict(row) for row in rows]

    subscriptions = notification_subscriptions

    def unique_notification_series(self):
        return [row[0] for row in self.db.execute("""
            SELECT DISTINCT series_id FROM watchlist
            WHERE notifications_enabled=1 ORDER BY series_id
        """).fetchall()]

    def observe_episodes(self, series_id, episodes, now=None):
        """Persist one shared Anime365 episode check for every watching user.

        Newly found episodes become durable unresolved candidates. The watcher
        later filters them by translation availability; a process restart cannot
        erase that work because candidates are stored before any API request.
        """
        series_id = self._positive_id(series_id, "series_id")
        episodes = self._episodes(episodes)
        now = self._now(now)
        latest = episodes[-1] if episodes else None
        with self.db:
            rows = self.db.execute("""
                SELECT user_id, notifications_enabled, notification_baselined
                FROM watchlist WHERE series_id=?
            """, (series_id,)).fetchall()
            for user_id, enabled, baselined in rows:
                if latest:
                    self.db.execute("""
                        UPDATE watchlist
                        SET last_available_episode_id=?, last_available_episode_number=?
                        WHERE user_id=? AND series_id=?
                    """, (latest[0], latest[1], user_id, series_id))
                if not enabled:
                    continue
                if not baselined:
                    self.db.executemany("""
                        INSERT OR IGNORE INTO watch_episode_state(
                            user_id, series_id, episode_id, episode_number, baseline, discovered_at
                        ) VALUES (?, ?, ?, ?, 1, ?)
                    """, [(user_id, series_id, episode_id, number, now) for episode_id, number in episodes])
                    self.db.execute("""
                        UPDATE watchlist SET notification_baselined=1
                        WHERE user_id=? AND series_id=?
                    """, (user_id, series_id))
                    continue
                self.db.executemany("""
                    INSERT OR IGNORE INTO watch_episode_state(
                        user_id, series_id, episode_id, episode_number, baseline, discovered_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                """, [(user_id, series_id, episode_id, number, now) for episode_id, number in episodes])

    def notification_candidates(self, series_id, now=None):
        """Return unresolved post-baseline episodes, due for translation checks."""
        series_id = self._positive_id(series_id, "series_id")
        now = self._now(now)
        return self._dicts(self.db.execute("""
            SELECT state.user_id, state.series_id, state.episode_id, state.episode_number,
                   state.discovered_at, state.candidate_attempts AS attempts,
                   watch.title, watch.year, watch.series_type,
                   watch.notification_mode AS mode
            FROM watch_episode_state AS state
            JOIN watchlist AS watch
              ON watch.user_id=state.user_id AND watch.series_id=state.series_id
            WHERE state.series_id=?
              AND watch.notifications_enabled=1
              AND state.baseline=0
              AND state.candidate_resolved_at IS NULL
              AND state.candidate_retry_at <= ?
            ORDER BY state.episode_id, state.user_id
        """, (series_id, now)))

    def defer_notification_candidate(self, user_id, series_id, episode_id, mode, delay, now=None):
        """Keep a translation-dependent candidate durable, but rate-limit rechecks."""
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        mode = self._mode(mode)
        retry_at = self._now(now) + max(0, float(delay))
        with self.db:
            cursor = self.db.execute("""
                UPDATE watch_episode_state AS state
                SET candidate_retry_at=?, candidate_attempts=candidate_attempts+1
                WHERE state.user_id=? AND state.series_id=? AND state.episode_id=?
                  AND state.baseline=0 AND state.candidate_resolved_at IS NULL
                  AND EXISTS (
                    SELECT 1 FROM watchlist
                    WHERE user_id=state.user_id AND series_id=state.series_id
                      AND notifications_enabled=1 AND notification_mode=?
                  )
            """, (retry_at, user_id, series_id, episode_id, mode))
        return cursor.rowcount > 0

    def discard_notification_candidate(self, user_id, series_id, episode_id, mode, now=None):
        """Resolve an obsolete candidate without creating an outbox message."""
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        mode = self._mode(mode)
        with self.db:
            cursor = self.db.execute("""
                UPDATE watch_episode_state AS state
                SET candidate_resolved_at=?
                WHERE state.user_id=? AND state.series_id=? AND state.episode_id=?
                  AND state.candidate_resolved_at IS NULL
                  AND EXISTS (
                    SELECT 1 FROM watchlist
                    WHERE user_id=state.user_id AND series_id=state.series_id
                      AND notification_mode=?
                  )
            """, (self._now(now), user_id, series_id, episode_id, mode))
        return cursor.rowcount > 0

    def queue_notification(self, user_id, series_id, episode_id, mode, now=None):
        """Atomically claim an eligible candidate and put it in the durable outbox."""
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        mode = self._mode(mode)
        now = self._now(now)
        with self.db:
            row = self.db.execute("""
                SELECT state.episode_number
                FROM watch_episode_state AS state
                JOIN watchlist AS watch
                  ON watch.user_id=state.user_id AND watch.series_id=state.series_id
                WHERE state.user_id=? AND state.series_id=? AND state.episode_id=?
                  AND state.baseline=0 AND state.candidate_resolved_at IS NULL
                  AND watch.notifications_enabled=1 AND watch.notification_mode=?
            """, (user_id, series_id, episode_id, mode)).fetchone()
            if not row:
                return False
            cursor = self.db.execute("""
                INSERT OR IGNORE INTO notification_outbox(
                    user_id, series_id, episode_id, episode_number, mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, series_id, episode_id, row[0], mode, now))
            self.db.execute("""
                UPDATE watch_episode_state SET candidate_resolved_at=?
                WHERE user_id=? AND series_id=? AND episode_id=?
            """, (now, user_id, series_id, episode_id))
        return cursor.rowcount > 0

    def pending_notifications(self, now=None):
        """Outbox rows that can be sent now. They are never temporary menus."""
        now = self._now(now)
        return self._dicts(self.db.execute("""
            SELECT outbox.user_id, outbox.series_id, outbox.episode_id, outbox.episode_number,
                   outbox.mode, outbox.created_at, outbox.retry_at, outbox.attempts,
                   watch.title, watch.year, watch.series_type
            FROM notification_outbox AS outbox
            JOIN watchlist AS watch
              ON watch.user_id=outbox.user_id AND watch.series_id=outbox.series_id
            WHERE outbox.sent_at IS NULL AND outbox.retry_at <= ?
              AND watch.notifications_enabled=1 AND watch.notification_mode=outbox.mode
            ORDER BY outbox.created_at, outbox.user_id
        """, (now,)))

    def mark_notification_sent(self, user_id, series_id, episode_id, mode, now=None):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        mode = self._mode(mode)
        now = self._now(now)
        with self.db:
            row = self.db.execute("""
                SELECT episode_number FROM notification_outbox
                WHERE user_id=? AND series_id=? AND episode_id=? AND mode=? AND sent_at IS NULL
            """, (user_id, series_id, episode_id, mode)).fetchone()
            if not row:
                return False
            self.db.execute("""
                UPDATE notification_outbox SET sent_at=?
                WHERE user_id=? AND series_id=? AND episode_id=? AND mode=? AND sent_at IS NULL
            """, (now, user_id, series_id, episode_id, mode))
            self.db.execute("""
                UPDATE watchlist
                SET last_notified_episode_id=?, last_notified_episode_number=?
                WHERE user_id=? AND series_id=?
            """, (episode_id, row[0], user_id, series_id))
        return True

    def retry_notification(self, user_id, series_id, episode_id, mode, delay, now=None):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        mode = self._mode(mode)
        retry_at = self._now(now) + max(0, float(delay))
        with self.db:
            cursor = self.db.execute("""
                UPDATE notification_outbox
                SET attempts=attempts+1, retry_at=?
                WHERE user_id=? AND series_id=? AND episode_id=? AND mode=? AND sent_at IS NULL
            """, (retry_at, user_id, series_id, episode_id, mode))
        return cursor.rowcount > 0

    record_notification_failure = retry_notification

    def discard_notification(self, user_id, series_id, episode_id, mode):
        """Drop an unsent notification whose recipient no longer has access."""
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        mode = self._mode(mode)
        with self.db:
            cursor = self.db.execute("""
                DELETE FROM notification_outbox
                WHERE user_id=? AND series_id=? AND episode_id=? AND mode=? AND sent_at IS NULL
            """, (user_id, series_id, episode_id, mode))
        return cursor.rowcount > 0
