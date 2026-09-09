import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from ani365_bot.store import SCHEMA_VERSION, Store


def episode(identifier, number):
    return {"id": identifier, "episodeFull": str(number), "episodeInt": number}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.directory)

    def test_v1_migration_preserves_existing_data_and_advances_version(self):
        self.store.close()
        db_path = self.directory / "bot.sqlite3"
        key_path = self.directory / "token.key"
        db_path.unlink()
        key_path.unlink()
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        cipher = Fernet(key)
        db = sqlite3.connect(db_path)
        db.executescript("""
            CREATE TABLE users (user_id INTEGER PRIMARY KEY, token BLOB NOT NULL);
            CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                created REAL NOT NULL, delete_at REAL NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );
            PRAGMA user_version = 1;
        """)
        db.execute("INSERT INTO users VALUES (?, ?)", (42, cipher.encrypt(b"saved-token")))
        db.execute("INSERT INTO settings VALUES (?, ?)", ("offset", "123"))
        db.execute("INSERT INTO messages VALUES (?, ?, ?, ?)", (42, 9, 1.0, 2.0))
        db.commit()
        db.close()

        self.store = Store(self.directory)

        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.store.token(42), "saved-token")
        self.assertEqual(self.store.get("offset"), "123")
        self.assertEqual(self.store.due(True), [(42, 9, 1.0)])
        tables = {row[0] for row in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertTrue({"users", "settings", "messages", "allowed_users", "user_state",
                         "watchlist", "watch_episode_state", "notification_outbox"} <= tables)
        self.reopen()
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_owner_is_always_allowed_and_cannot_be_revoked(self):
        owner = 42
        self.assertTrue(self.store.is_allowed(owner, owner))
        self.assertFalse(self.store.is_allowed(43, owner))
        self.assertFalse(self.store.add_allowed_user(owner, owner, added_at=1))
        self.assertTrue(self.store.add_allowed_user(43, owner, added_at=2))
        self.assertFalse(self.store.add_allowed_user(43, owner, added_at=3))
        self.assertTrue(self.store.is_allowed(43, owner))
        self.assertEqual(self.store.list_allowed_users(owner), [
            {"user_id": 42, "added_at": None, "is_owner": True},
            {"user_id": 43, "added_at": 2.0, "is_owner": False},
        ])
        self.assertFalse(self.store.revoke_allowed_user(owner, owner))
        self.assertTrue(self.store.is_allowed(owner, owner))
        self.assertTrue(self.store.revoke_allowed_user(43, owner))
        self.assertFalse(self.store.is_allowed(43, owner))

    def test_awaiting_token_is_isolated_by_user(self):
        self.assertFalse(self.store.awaiting_token(1))
        self.store.set_awaiting_token(1, True)
        self.store.set_awaiting_token(2, False)
        self.assertTrue(self.store.awaiting_token(1))
        self.assertFalse(self.store.awaiting_token(2))
        self.store.set_awaiting_token(1, False)
        self.assertFalse(self.store.awaiting_token(1))

    def test_message_cleanup_queue_can_be_scoped_to_one_private_chat(self):
        self.store.track(1, 10, ttl=-1, created=1)
        self.store.track(2, 20, ttl=-1, created=1)
        self.assertEqual(self.store.due(all_messages=True, chat_id=1), [(1, 10, 1.0)])
        self.assertEqual(self.store.due(all_messages=True, chat_id=2), [(2, 20, 1.0)])

    def test_watchlist_add_is_idempotent_and_progress_is_preserved(self):
        first = self.store.add_watchlist(1, 10, "Первое название", 2024, "TV", added_at=10)
        self.assertTrue(first["created"])
        self.assertEqual(first["title"], "Первое название")
        self.assertEqual(first["year"], "2024")
        self.assertEqual(first["series_type"], "TV")
        self.assertIsNone(first["last_watched_episode_id"])

        updated = self.store.update_progress(1, 10, episode(101, "12.5"))
        self.assertEqual(updated["last_watched_episode_id"], 101)
        self.assertEqual(updated["last_watched_episode_number"], "12.5")
        available = self.store.update_available(1, 10, episode(102, 13))
        self.assertEqual(available["last_known_episode_number"], "13")

        repeated = self.store.add_watchlist(1, 10, "Новое название", 2025, "ONA", added_at=99)
        self.assertFalse(repeated["created"])
        self.assertEqual(repeated["title"], "Новое название")
        self.assertEqual(repeated["added_at"], 10.0)
        self.assertEqual(repeated["last_watched_episode_id"], 101)
        self.assertEqual(repeated["last_available_episode_id"], 102)
        self.assertEqual(len(self.store.list_watchlist(1)), 1)
        self.assertTrue(self.store.has_watchlist(1, 10))
        self.assertTrue(self.store.remove_watchlist(1, 10))
        self.assertFalse(self.store.has_watchlist(1, 10))
        self.assertFalse(self.store.remove_watchlist(1, 10))

    def test_first_notification_baseline_does_not_enqueue_existing_episodes(self):
        episodes = [episode(101, 1), episode(102, 2)]
        self.store.add_watchlist(1, 7, "Аниме")
        item = self.store.configure_notifications(1, 7, True, "sub_ru", episodes, now=10)
        self.assertTrue(item["notifications_enabled"])
        self.assertEqual(item["notification_mode"], "subtitles")
        self.assertTrue(item["notification_baselined"])
        self.assertEqual(self.store.notification_candidates(7, now=10), [])

        self.store.observe_episodes(7, episodes, now=11)
        self.assertEqual(self.store.notification_candidates(7, now=11), [])
        self.store.observe_episodes(7, [*episodes, episode(103, 3)], now=12)
        candidates = self.store.notification_candidates(7, now=12)
        self.assertEqual([(row["user_id"], row["episode_id"], row["mode"])
                          for row in candidates], [(1, 103, "subtitles")])

    def test_untranslated_candidate_is_deferred_then_sent_once_and_survives_restart(self):
        baseline = [episode(101, 1)]
        self.store.add_watchlist(1, 7, "Аниме")
        self.store.configure_notifications(1, 7, True, "voice", baseline, now=1)
        self.store.observe_episodes(7, [*baseline, episode(102, 2)], now=2)

        self.assertTrue(self.store.defer_notification_candidate(1, 7, 102, "voice", 10, now=2))
        self.assertEqual(self.store.notification_candidates(7, now=11), [])
        candidates = self.store.notification_candidates(7, now=12)
        self.assertEqual(candidates[0]["episode_id"], 102)
        self.assertEqual(candidates[0]["attempts"], 1)
        self.assertTrue(self.store.queue_notification(1, 7, 102, "voice", now=12))
        self.assertFalse(self.store.queue_notification(1, 7, 102, "voice", now=12))
        self.reopen()
        pending = self.store.pending_notifications(now=12)
        self.assertEqual([(row["episode_id"], row["mode"]) for row in pending], [(102, "voice")])
        self.assertTrue(self.store.mark_notification_sent(1, 7, 102, "voice", now=13))
        self.assertEqual(self.store.pending_notifications(now=13), [])
        saved = self.store.get_watchlist(1, 7)
        self.assertEqual(saved["last_notified_episode_id"], 102)
        self.assertEqual(saved["last_notified_episode_number"], "2")
        self.store.observe_episodes(7, [*baseline, episode(102, 2)], now=14)
        self.assertEqual(self.store.notification_candidates(7, now=14), [])

    def test_shared_series_keeps_per_user_candidates_and_unique_query(self):
        baseline = [episode(101, 1)]
        for user_id, mode in ((1, "subtitles"), (2, "voice")):
            self.store.add_watchlist(user_id, 7, f"Аниме {user_id}")
            self.store.configure_notifications(user_id, 7, True, mode, baseline, now=1)
        self.assertEqual(self.store.unique_notification_series(), [7])
        self.assertEqual([row["user_id"] for row in self.store.notification_subscriptions()], [1, 2])
        self.store.observe_episodes(7, [*baseline, episode(102, 2)], now=2)
        candidates = self.store.notification_candidates(7, now=2)
        self.assertEqual({(row["user_id"], row["mode"]) for row in candidates},
                         {(1, "subtitles"), (2, "voice")})

    def test_revoke_stops_pending_notifications_but_keeps_watchlist(self):
        self.store.add_allowed_user(1, owner_id=42)
        baseline = [episode(101, 1)]
        self.store.add_watchlist(1, 7, "Аниме")
        self.store.configure_notifications(1, 7, True, "any", baseline, now=1)
        self.store.observe_episodes(7, [*baseline, episode(102, 2)], now=2)
        self.assertTrue(self.store.queue_notification(1, 7, 102, "any", now=2))
        self.assertEqual(len(self.store.pending_notifications(now=2)), 1)

        self.assertTrue(self.store.revoke_allowed_user(1, owner_id=42))
        self.assertEqual(self.store.pending_notifications(now=2), [])
        self.assertTrue(self.store.has_watchlist(1, 7))
        self.assertFalse(self.store.get_watchlist(1, 7)["notifications_enabled"])
        self.assertEqual(self.store.notification_candidates(7, now=2), [])

    def test_remove_watchlist_cascades_pending_notification_state(self):
        baseline = [episode(101, 1)]
        self.store.add_watchlist(1, 7, "Аниме")
        self.store.configure_notifications(1, 7, True, "any", baseline, now=1)
        self.store.observe_episodes(7, [*baseline, episode(102, 2)], now=2)
        self.assertTrue(self.store.queue_notification(1, 7, 102, "any", now=2))
        self.assertTrue(self.store.remove_watchlist(1, 7))
        self.assertEqual(self.store.pending_notifications(now=2), [])
        self.assertEqual(self.store.notification_candidates(7, now=2), [])


if __name__ == "__main__":
    unittest.main()
