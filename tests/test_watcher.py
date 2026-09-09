import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from ani365_bot.api import APIError
from ani365_bot.store import Store
from ani365_bot.watcher import (NOTIFICATION_SUBTITLES, NOTIFICATION_VOICE, Watcher,
                                available_notification_modes, retry_delay)


class FakeStore:
    def __init__(self, candidates=()):
        self.series = [7]
        self.candidates = list(candidates)
        self.outbox = []
        self.observed = []
        self.deferred = []
        self.sent = []
        self.retried = []
        self.discarded = []

    def unique_notification_series(self):
        return self.series

    def observe_episodes(self, series_id, episodes, now=None):
        self.observed.append((series_id, episodes, now))

    def notification_candidates(self, series_id, now=None):
        return list(self.candidates)

    def queue_notification(self, user_id, series_id, episode_id, mode, now=None):
        row = {"user_id": user_id, "series_id": series_id, "episode_id": episode_id,
               "mode": mode}
        if row not in self.outbox:
            self.outbox.append(row)
        self.candidates = [candidate for candidate in self.candidates
                           if (candidate["user_id"], candidate["series_id"],
                               candidate["episode_id"], candidate["mode"]) !=
                           (user_id, series_id, episode_id, mode)]

    def defer_notification_candidate(self, user_id, series_id, episode_id, mode, delay, now=None):
        self.deferred.append((user_id, series_id, episode_id, mode, delay, now))
        return True

    def pending_notifications(self, now=None):
        return list(self.outbox)

    def mark_notification_sent(self, user_id, series_id, episode_id, mode, now=None):
        self.sent.append((user_id, series_id, episode_id, mode))
        self.outbox = [row for row in self.outbox
                       if (row["user_id"], row["series_id"], row["episode_id"], row["mode"]) !=
                       (user_id, series_id, episode_id, mode)]

    def retry_notification(self, user_id, series_id, episode_id, mode, delay, now=None):
        self.retried.append((user_id, series_id, episode_id, mode, delay, now))

    def discard_notification_candidate(self, user_id, series_id, episode_id, mode):
        self.discarded.append((user_id, series_id, episode_id, mode))

    def discard_notification(self, user_id, series_id, episode_id, mode):
        self.discarded.append((user_id, series_id, episode_id, mode))
        self.outbox = [row for row in self.outbox
                       if (row["user_id"], row["series_id"], row["episode_id"], row["mode"]) !=
                       (user_id, series_id, episode_id, mode)]


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_store_baseline_skips_existing_episode_then_sends_new_one(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                store.add_watchlist(1, 7, "Title", added_at=1)
                store.configure_notifications(1, 7, True, "subtitles",
                                              [{"id": 70, "episodeFull": "13"}], now=2)
                anime = AsyncMock()
                anime.episodes.return_value = [
                    {"id": 70, "episodeFull": "13"},
                    {"id": 71, "episodeFull": "14"},
                ]
                anime.translations.return_value = [{"id": 5, "type": "subRu"}]
                delivered = AsyncMock()

                await Watcher(store, anime, delivered, clock=lambda: 100).check_once()

                anime.translations.assert_awaited_once_with(71)
                delivered.assert_awaited_once()
                self.assertEqual(store.pending_notifications(now=101), [])
                with store.db:
                    persisted = store.db.execute(
                        "SELECT sent_at FROM notification_outbox WHERE user_id=1 AND series_id=7 "
                        "AND episode_id=71").fetchone()
                self.assertEqual(persisted, (100.0,))
            finally:
                store.close()

    async def test_one_series_is_observed_once_and_fanned_out_by_translation_mode(self):
        store = FakeStore([
            {"user_id": 1, "series_id": 7, "episode_id": 70, "mode": "subtitles"},
            {"user_id": 2, "series_id": 7, "episode_id": 70, "mode": "voice"},
        ])
        anime = AsyncMock()
        anime.episodes.return_value = [{"id": 70, "episodeFull": "14"}]
        anime.translations.return_value = [
            {"id": 1, "type": "subRu", "typeKind": "sub", "typeLang": "ru"},
            {"id": 2, "type": "voiceRu", "typeKind": "voice", "typeLang": "ru"},
        ]
        delivered = AsyncMock()
        watcher = Watcher(store, anime, delivered, clock=lambda: 100)

        await watcher.check_once()

        anime.episodes.assert_awaited_once_with(7)
        anime.translations.assert_awaited_once_with(70)
        self.assertEqual(len(delivered.await_args_list), 2)
        self.assertEqual(set(store.sent), {(1, 7, 70, "subtitles"), (2, 7, 70, "voice")})

    async def test_missing_russian_translation_is_deferred_without_sending(self):
        store = FakeStore([
            {"user_id": 1, "series_id": 7, "episode_id": 70, "mode": "subtitles", "attempts": 3},
        ])
        anime = AsyncMock()
        anime.episodes.return_value = [{"id": 70}]
        anime.translations.return_value = [{"id": 1, "type": "voiceEn", "typeLang": "en"}]
        delivered = AsyncMock()
        watcher = Watcher(store, anime, delivered, clock=lambda: 100)

        await watcher.check_once()

        delivered.assert_not_awaited()
        self.assertEqual(store.sent, [])
        self.assertEqual(store.deferred[0][:4], (1, 7, 70, "subtitles"))
        self.assertEqual(store.deferred[0][4], 40)

    async def test_revoked_user_is_filtered_before_queue_or_send(self):
        store = FakeStore([
            {"user_id": 9, "series_id": 7, "episode_id": 70, "mode": "any"},
        ])
        anime = AsyncMock()
        anime.episodes.return_value = [{"id": 70}]
        delivered = AsyncMock()
        watcher = Watcher(store, anime, delivered, is_allowed=lambda user_id: user_id != 9,
                          clock=lambda: 100)

        await watcher.check_once()

        delivered.assert_not_awaited()
        self.assertEqual(store.outbox, [])
        self.assertEqual(store.discarded, [(9, 7, 70, "any")])

    async def test_delivery_failure_is_durable_and_retried(self):
        store = FakeStore([
            {"user_id": 1, "series_id": 7, "episode_id": 70, "mode": "any"},
        ])
        anime = AsyncMock()
        anime.episodes.return_value = [{"id": 70}]
        delivered = AsyncMock(side_effect=APIError("temporarily unavailable", 429, retry_after=17))
        watcher = Watcher(store, anime, delivered, clock=lambda: 100)

        await watcher.check_once()

        self.assertEqual(store.sent, [])
        self.assertEqual(store.retried, [(1, 7, 70, "any", 17, 100)])


class NotificationMetadataTests(unittest.TestCase):
    def test_translation_modes_use_existing_type_parser(self):
        rows = [
            {"type": "subRu"},
            {"typeKind": "voice", "typeLang": "RU"},
            {"type": "subEn"},
            {"type": "raw", "typeLang": "ja"},
        ]
        self.assertEqual(available_notification_modes(rows),
                         {NOTIFICATION_SUBTITLES, NOTIFICATION_VOICE})

    def test_retry_delay_is_bounded_and_honours_rate_limit(self):
        self.assertEqual(retry_delay(1), 5)
        self.assertEqual(retry_delay(3), 20)
        self.assertEqual(retry_delay(100), 300)
        self.assertEqual(retry_delay(1, 17), 17)


if __name__ == "__main__":
    unittest.main()
