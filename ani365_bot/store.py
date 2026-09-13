import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


SCHEMA_VERSION = 7


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
        # Each production process owns its connection. ``check_same_thread`` is
        # disabled so an ASGI test server may use this injected Store from its
        # event-loop thread; SQLite still serializes cross-process writes.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        os.chmod(db_path, 0o600)
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.execute("PRAGMA foreign_keys = ON")
        # The bot and the read-heavy Mini App can run in separate containers.
        # WAL lets their short transactions coexist without making credentials or
        # Anime365 responses visible outside this database.
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA busy_timeout = 5000")
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
            version = 2
        if version < 3:
            with self.db:
                self._create_v3()
                self.db.execute("PRAGMA user_version = 3")
            version = 3
        if version < 4:
            with self.db:
                self._create_v4()
                self.db.execute("PRAGMA user_version = 4")
            version = 4
        if version < 5:
            with self.db:
                self._create_v5()
                self.db.execute("PRAGMA user_version = 5")
            version = 5
        if version < 6:
            with self.db:
                self._create_v6()
                self.db.execute("PRAGMA user_version = 6")
            version = 6
        if version < 7:
            with self.db:
                self._create_v7()
                self.db.execute("PRAGMA user_version = 7")

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

    def _create_v3(self):
        """Add resumable Mini App state without changing v1/v2 records.

        A position is deliberately keyed by the local user and watch-list title.
        It contains numeric playback state only; signed CDN URLs and Anime365
        credentials never enter this table.
        """
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS playback_progress (
                user_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                episode_id INTEGER NOT NULL,
                position_seconds REAL NOT NULL DEFAULT 0 CHECK(position_seconds >= 0),
                duration_seconds REAL NOT NULL DEFAULT 0 CHECK(duration_seconds >= 0),
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, series_id),
                FOREIGN KEY(user_id, series_id)
                    REFERENCES watchlist(user_id, series_id) ON DELETE CASCADE
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS playback_progress_recent_idx
            ON playback_progress(user_id, updated_at DESC)
        """)

    def _create_v4(self):
        """Durable metadata for bounded offline jobs; media files stay on disk."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS download_jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                series_id INTEGER NOT NULL,
                episode_id INTEGER NOT NULL,
                episode_number TEXT NOT NULL,
                translation_id INTEGER NOT NULL,
                quality INTEGER NOT NULL,
                delivery TEXT NOT NULL CHECK(delivery IN ('browser', 'telegram')),
                status TEXT NOT NULL CHECK(status IN (
                    'queued', 'preparing', 'ready', 'sent', 'failed', 'cancelled', 'expired'
                )),
                filename TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                expires_at REAL,
                error_code TEXT,
                FOREIGN KEY(user_id, series_id)
                    REFERENCES watchlist(user_id, series_id) ON DELETE CASCADE
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS download_jobs_user_idx
            ON download_jobs(user_id, created_at DESC)
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS download_jobs_queue_idx
            ON download_jobs(status, created_at)
        """)

    def _create_v5(self):
        """Encrypted third-party accounts and one-time OAuth callback state."""
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS external_accounts (
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                access_token BLOB NOT NULL,
                refresh_token BLOB NOT NULL,
                expires_at REAL NOT NULL,
                external_user_id TEXT,
                sync_enabled INTEGER NOT NULL DEFAULT 1 CHECK(sync_enabled IN (0, 1)),
                PRIMARY KEY(user_id, provider)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS oauth_states_expiry_idx ON oauth_states(expires_at)")

    def _create_v6(self):
        """Persist safe Shikimori library metadata and confirmed ID mappings.

        These tables deliberately contain only provider IDs, display labels and
        progress. OAuth credentials remain solely in ``external_accounts``;
        Anime365 signed URLs never belong in an import record.
        """
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS anime_external_ids (
                anime365_series_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                external_id TEXT NOT NULL,
                PRIMARY KEY(anime365_series_id, provider),
                UNIQUE(provider, external_id)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS external_user_rates (
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                external_rate_id TEXT NOT NULL,
                external_anime_id TEXT NOT NULL,
                status TEXT NOT NULL,
                episodes INTEGER NOT NULL DEFAULT 0 CHECK(episodes >= 0),
                title TEXT NOT NULL,
                anime365_series_id INTEGER,
                imported_at REAL NOT NULL,
                PRIMARY KEY(user_id, provider, external_rate_id)
            )
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS external_user_rates_series_idx
            ON external_user_rates(user_id, provider, anime365_series_id)
        """)

    def _create_v7(self):
        """Keep a job title snapshot and allow non-destructive UI cleanup."""
        self.db.execute("ALTER TABLE download_jobs ADD COLUMN series_title TEXT")
        self.db.execute("ALTER TABLE download_jobs ADD COLUMN hidden_at REAL")
        # Preserve a useful title for work queued before this migration whenever
        # the source watch-list record still exists.
        self.db.execute("""
            UPDATE download_jobs AS d SET series_title=(
                SELECT w.title FROM watchlist AS w
                WHERE w.user_id=d.user_id AND w.series_id=d.series_id
            ) WHERE series_title IS NULL
        """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS download_jobs_visible_idx
            ON download_jobs(user_id, hidden_at, created_at DESC)
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

    def external_account(self, user_id, provider):
        user_id = self._positive_id(user_id, "user_id")
        row = self.db.execute("""
            SELECT access_token, refresh_token, expires_at, external_user_id, sync_enabled
            FROM external_accounts WHERE user_id=? AND provider=?
        """, (user_id, str(provider))).fetchone()
        if not row:
            return None
        try:
            return {"access_token": self.cipher.decrypt(row[0]).decode(),
                    "refresh_token": self.cipher.decrypt(row[1]).decode(), "expires_at": row[2],
                    "external_user_id": row[3], "sync_enabled": bool(row[4])}
        except InvalidToken:
            raise StateError("Cannot decrypt external account; restore matching token.key") from None

    def external_account_status(self, user_id, provider):
        account = self.external_account(user_id, provider)
        if not account:
            return {"connected": False}
        return {"connected": True, "external_user_id": account["external_user_id"],
                "expires_at": account["expires_at"], "sync_enabled": account["sync_enabled"]}

    def save_external_account(self, user_id, provider, access_token, refresh_token, expires_at,
                              external_user_id=None):
        user_id = self._positive_id(user_id, "user_id")
        if not access_token or not refresh_token:
            raise ValueError("External tokens are required")
        with self.db:
            self.db.execute("""
                INSERT INTO external_accounts(
                    user_id, provider, access_token, refresh_token, expires_at, external_user_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, provider) DO UPDATE SET access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                    external_user_id=excluded.external_user_id
            """, (user_id, str(provider), self.cipher.encrypt(str(access_token).encode()),
                  self.cipher.encrypt(str(refresh_token).encode()), float(expires_at), external_user_id))

    def forget_external_account(self, user_id, provider):
        with self.db:
            self.db.execute("DELETE FROM external_accounts WHERE user_id=? AND provider=?",
                            (self._positive_id(user_id, "user_id"), str(provider)))

    def create_oauth_state(self, user_id, provider, state, ttl=600, now=None):
        user_id = self._positive_id(user_id, "user_id")
        moment = self._now(now)
        with self.db:
            self.db.execute("DELETE FROM oauth_states WHERE expires_at <= ?", (moment,))
            self.db.execute("INSERT INTO oauth_states(state, user_id, provider, expires_at) VALUES (?, ?, ?, ?)",
                            (str(state), user_id, str(provider), moment + max(1, int(ttl))))

    def consume_oauth_state(self, provider, state, now=None):
        moment = self._now(now)
        with self.db:
            row = self.db.execute("SELECT user_id, expires_at FROM oauth_states WHERE state=? AND provider=?",
                                  (str(state), str(provider))).fetchone()
            self.db.execute("DELETE FROM oauth_states WHERE state=?", (str(state),))
        return row[0] if row and row[1] >= moment else None

    # Shikimori import state. A mapping is only created after an exact MAL
    # bridge or a user's explicit selection in the Mini App.
    def save_external_id(self, series_id, provider, external_id):
        series_id = self._positive_id(series_id, "series_id")
        provider, external_id = str(provider).strip(), str(external_id).strip()
        if not provider or not external_id:
            raise ValueError("External provider and id are required")
        with self.db:
            self.db.execute("""
                INSERT INTO anime_external_ids(anime365_series_id, provider, external_id)
                VALUES (?, ?, ?)
                ON CONFLICT(provider, external_id) DO UPDATE SET anime365_series_id=excluded.anime365_series_id
            """, (series_id, provider, external_id))

    def external_series_id(self, provider, external_id):
        row = self.db.execute("""
            SELECT anime365_series_id FROM anime_external_ids
            WHERE provider=? AND external_id=?
        """, (str(provider).strip(), str(external_id).strip())).fetchone()
        return int(row[0]) if row else None

    @staticmethod
    def _external_rate(row):
        keys = ("external_rate_id", "external_anime_id", "status", "episodes", "title",
                "anime365_series_id", "imported_at")
        return dict(zip(keys, row)) if row else None

    def import_external_rates(self, user_id, provider, rates, now=None):
        """Upsert a filtered external list without changing local progress.

        ``rates`` are normalized by the web layer.  Existing manual mappings
        survive a later import and only same-provider/global ID matches link
        automatically.
        """
        user_id = self._positive_id(user_id, "user_id")
        provider, timestamp = str(provider).strip(), self._now(now)
        if not provider:
            raise ValueError("External provider is required")
        imported = []
        with self.db:
            for rate in rates:
                rate_id = str(rate["external_rate_id"]).strip()
                anime_id = str(rate["external_anime_id"]).strip()
                status = str(rate["status"]).strip()
                title = str(rate.get("title") or "Без названия").strip()[:500] or "Без названия"
                try:
                    episodes = max(0, int(rate.get("episodes") or 0))
                except (TypeError, ValueError):
                    episodes = 0
                if not rate_id or not anime_id or not status:
                    continue
                mapped = self.external_series_id(provider, anime_id)
                previous = self.db.execute("""
                    SELECT anime365_series_id FROM external_user_rates
                    WHERE user_id=? AND provider=? AND external_rate_id=?
                """, (user_id, provider, rate_id)).fetchone()
                series_id = previous[0] if previous and previous[0] is not None else mapped
                self.db.execute("""
                    INSERT INTO external_user_rates(
                        user_id, provider, external_rate_id, external_anime_id, status, episodes,
                        title, anime365_series_id, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, provider, external_rate_id) DO UPDATE SET
                        external_anime_id=excluded.external_anime_id, status=excluded.status,
                        episodes=excluded.episodes, title=excluded.title, imported_at=excluded.imported_at,
                        anime365_series_id=COALESCE(external_user_rates.anime365_series_id,
                                                     excluded.anime365_series_id)
                """, (user_id, provider, rate_id, anime_id, status, episodes, title, series_id, timestamp))
                imported.append(rate_id)
        return imported

    def external_user_rates(self, user_id, provider, *, linked=None, limit=2000):
        user_id = self._positive_id(user_id, "user_id")
        where, params = ["user_id=?", "provider=?"], [user_id, str(provider)]
        if linked is True:
            where.append("anime365_series_id IS NOT NULL")
        elif linked is False:
            where.append("anime365_series_id IS NULL")
        limit = max(1, min(5000, int(limit)))
        rows = self.db.execute(f"""
            SELECT external_rate_id, external_anime_id, status, episodes, title,
                   anime365_series_id, imported_at
            FROM external_user_rates WHERE {' AND '.join(where)}
            ORDER BY imported_at DESC, title COLLATE NOCASE LIMIT ?
        """, [*params, limit]).fetchall()
        return [self._external_rate(row) for row in rows]

    def external_user_rate(self, user_id, provider, rate_id):
        user_id = self._positive_id(user_id, "user_id")
        row = self.db.execute("""
            SELECT external_rate_id, external_anime_id, status, episodes, title,
                   anime365_series_id, imported_at
            FROM external_user_rates
            WHERE user_id=? AND provider=? AND external_rate_id=?
        """, (user_id, str(provider), str(rate_id))).fetchone()
        return self._external_rate(row)

    def link_external_user_rate(self, user_id, provider, rate_id, series_id):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        if not self.has_watchlist(user_id, series_id):
            return None
        with self.db:
            cursor = self.db.execute("""
                UPDATE external_user_rates SET anime365_series_id=?
                WHERE user_id=? AND provider=? AND external_rate_id=?
            """, (series_id, user_id, str(provider), str(rate_id)))
        return self.external_user_rate(user_id, provider, rate_id) if cursor.rowcount else None

    def external_rate_for_series(self, user_id, provider, series_id):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        row = self.db.execute("""
            SELECT external_rate_id, external_anime_id, status, episodes, title,
                   anime365_series_id, imported_at
            FROM external_user_rates
            WHERE user_id=? AND provider=? AND anime365_series_id=?
            ORDER BY imported_at DESC LIMIT 1
        """, (user_id, str(provider), series_id)).fetchone()
        return self._external_rate(row)

    def update_external_rate_episodes(self, user_id, provider, rate_id, episodes):
        user_id = self._positive_id(user_id, "user_id")
        episodes = max(0, int(episodes))
        with self.db:
            self.db.execute("""
                UPDATE external_user_rates SET episodes=MAX(episodes, ?)
                WHERE user_id=? AND provider=? AND external_rate_id=?
            """, (episodes, user_id, str(provider), str(rate_id)))

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

    def playback_progress(self, user_id, series_id):
        """Return one user's resumable position for a saved Anime365 title."""
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        row = self.db.execute("""
            SELECT episode_id, position_seconds, duration_seconds, updated_at
            FROM playback_progress WHERE user_id=? AND series_id=?
        """, (user_id, series_id)).fetchone()
        if row is None:
            return None
        return dict(zip(("episode_id", "position_seconds", "duration_seconds", "updated_at"), row))

    def record_playback_progress(self, user_id, series_id, episode, position_seconds,
                                 duration_seconds, episode_number=None, *, ended=False,
                                 completion_threshold=0.9, now=None):
        """Persist a bounded resume point and mirror completed episodes to v2 state.

        The caller supplies the episode number from the just-observed Anime365
        episode list.  This keeps the old bot's ``last_watched_*`` fields fully
        compatible while the Mini App stores a position inside the current episode.
        """
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id, episode_number = self._episode(episode, episode_number)
        try:
            position = max(0.0, float(position_seconds or 0))
            duration = max(0.0, float(duration_seconds or 0))
            threshold = min(1.0, max(0.0, float(completion_threshold)))
        except (TypeError, ValueError):
            raise ValueError("Playback position must be numeric") from None
        if duration and position > duration:
            position = duration
        completed = bool(ended) or (duration > 0 and position / duration >= threshold)
        timestamp = self._now(now)
        with self.db:
            if not self.has_watchlist(user_id, series_id):
                return None
            self.db.execute("""
                INSERT INTO playback_progress(
                    user_id, series_id, episode_id, position_seconds, duration_seconds, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, series_id) DO UPDATE SET
                    episode_id=excluded.episode_id,
                    position_seconds=excluded.position_seconds,
                    duration_seconds=excluded.duration_seconds,
                    updated_at=excluded.updated_at
            """, (user_id, series_id, episode_id, position, duration, timestamp))
            if completed:
                self.db.execute("""
                    UPDATE watchlist
                    SET last_watched_episode_id=?, last_watched_episode_number=?
                    WHERE user_id=? AND series_id=?
                """, (episode_id, episode_number, user_id, series_id))
        result = self.playback_progress(user_id, series_id)
        result["completed"] = completed
        return result

    def recent_playback(self, user_id, limit=20):
        """Return private saved titles ordered by the last received player update."""
        user_id = self._positive_id(user_id, "user_id")
        limit = max(1, min(100, int(limit)))
        columns = ", ".join(f"w.{column}" for column in self._WATCHLIST_COLUMNS)
        rows = self.db.execute(f"""
            SELECT {columns}, p.episode_id, p.position_seconds, p.duration_seconds,
                   p.updated_at
            FROM playback_progress AS p
            JOIN watchlist AS w ON w.user_id=p.user_id AND w.series_id=p.series_id
            WHERE p.user_id=? ORDER BY p.updated_at DESC LIMIT ?
        """, (user_id, limit)).fetchall()
        result = []
        for row in rows:
            item = self._watchlist_dict(row[:len(self._WATCHLIST_COLUMNS)])
            item["playback"] = dict(zip(
                ("episode_id", "position_seconds", "duration_seconds", "updated_at"),
                row[len(self._WATCHLIST_COLUMNS):]))
            result.append(item)
        return result

    # Offline jobs persist only identifiers and lifecycle metadata.  The output
    # is an ephemeral file under MEDIA_DIR/ready-<unguessable-job-id>, never a
    # signed Anime365 URL or an access token.
    def create_download_job(self, job_id, user_id, series_id, episode_id, episode_number,
                            translation_id, quality, delivery, now=None):
        user_id = self._positive_id(user_id, "user_id")
        series_id = self._positive_id(series_id, "series_id")
        episode_id = self._positive_id(episode_id, "episode_id")
        translation_id = self._positive_id(translation_id, "translation_id")
        quality = self._positive_id(quality, "quality")
        if delivery not in ("browser", "telegram"):
            raise ValueError("Unknown download delivery")
        if not isinstance(job_id, str) or len(job_id) < 16 or len(job_id) > 128:
            raise ValueError("Invalid download job id")
        watch = self.get_watchlist(user_id, series_id)
        if not watch:
            return None
        with self.db:
            self.db.execute("""
                INSERT INTO download_jobs(
                    id, user_id, series_id, episode_id, episode_number, translation_id,
                    quality, delivery, status, created_at, series_title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """, (job_id, user_id, series_id, episode_id, str(episode_number or "?"),
                  translation_id, quality, delivery, self._now(now), watch["title"]))
        return self.download_job(user_id, job_id)

    def download_job(self, user_id, job_id):
        user_id = self._positive_id(user_id, "user_id")
        row = self.db.execute("""
            SELECT id, user_id, series_id, episode_id, episode_number, translation_id, quality,
                   delivery, status, filename, created_at, started_at, finished_at, expires_at,
                   error_code, series_title, hidden_at
            FROM download_jobs WHERE id=? AND user_id=?
        """, (str(job_id), user_id)).fetchone()
        if row is None:
            return None
        keys = ("id", "user_id", "series_id", "episode_id", "episode_number", "translation_id",
                "quality", "delivery", "status", "filename", "created_at", "started_at",
                "finished_at", "expires_at", "error_code", "series_title", "hidden_at")
        return dict(zip(keys, row))

    def list_download_jobs(self, user_id, limit=50):
        user_id = self._positive_id(user_id, "user_id")
        limit = max(1, min(100, int(limit)))
        ids = self.db.execute("""
            SELECT id FROM download_jobs
            WHERE user_id=? AND hidden_at IS NULL ORDER BY created_at DESC LIMIT ?
        """, (user_id, limit)).fetchall()
        return [self.download_job(user_id, row[0]) for row in ids]

    def hide_finished_download_jobs(self, user_id, now=None):
        """Hide completed rows from one user's UI while retaining all history."""
        user_id = self._positive_id(user_id, "user_id")
        with self.db:
            cursor = self.db.execute("""
                UPDATE download_jobs SET hidden_at=?
                WHERE user_id=? AND hidden_at IS NULL
                  AND status IN ('ready', 'sent', 'failed', 'cancelled', 'expired')
            """, (self._now(now), user_id))
        return cursor.rowcount

    def claim_download_job(self, job_id, now=None):
        """Atomically move exactly one queued job to preparing after a restart."""
        timestamp = self._now(now)
        with self.db:
            cursor = self.db.execute("""
                UPDATE download_jobs SET status='preparing', started_at=?, error_code=NULL
                WHERE id=? AND status='queued'
            """, (timestamp, str(job_id)))
            if not cursor.rowcount:
                return None
            row = self.db.execute("SELECT user_id FROM download_jobs WHERE id=?", (str(job_id),)).fetchone()
        return self.download_job(row[0], job_id) if row else None

    def queued_download_job_ids(self):
        return [row[0] for row in self.db.execute(
            "SELECT id FROM download_jobs WHERE status='queued' ORDER BY created_at"
        ).fetchall()]

    def cancel_download_job(self, user_id, job_id, now=None):
        user_id = self._positive_id(user_id, "user_id")
        with self.db:
            cursor = self.db.execute("""
                UPDATE download_jobs SET status='cancelled', finished_at=?
                WHERE id=? AND user_id=? AND status IN ('queued', 'preparing')
            """, (self._now(now), str(job_id), user_id))
        return cursor.rowcount > 0

    def finish_download_job(self, job_id, status, *, filename=None, expires_at=None,
                            error_code=None, now=None):
        if status not in ("ready", "sent", "failed", "cancelled"):
            raise ValueError("Invalid download status")
        with self.db:
            self.db.execute("""
                UPDATE download_jobs
                SET status=?, filename=?, expires_at=?, error_code=?, finished_at=?
                WHERE id=? AND status='preparing'
            """, (status, filename, expires_at, error_code, self._now(now), str(job_id)))

    def reset_interrupted_downloads(self):
        """A process restart releases workers and makes interrupted jobs retryable."""
        with self.db:
            self.db.execute("UPDATE download_jobs SET status='queued', started_at=NULL "
                            "WHERE status='preparing'")

    def expire_download_jobs(self, now=None):
        timestamp = self._now(now)
        with self.db:
            rows = self.db.execute("""
                SELECT id, filename FROM download_jobs
                WHERE status='ready' AND expires_at IS NOT NULL AND expires_at <= ?
            """, (timestamp,)).fetchall()
            self.db.execute("""
                UPDATE download_jobs SET status='expired', filename=NULL
                WHERE status='ready' AND expires_at IS NOT NULL AND expires_at <= ?
            """, (timestamp,))
        return rows

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
