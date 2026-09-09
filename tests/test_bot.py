import asyncio
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from ani365_bot.api import APIError
from ani365_bot.bot import Bot, Session
from ani365_bot.config import Config
from ani365_bot.store import Store
from ani365_bot.watcher import Watcher


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.delete_error = None
        self.upload_status_error = None
        self.document_error = None

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "deleteMessage" and self.delete_error:
            raise self.delete_error
        if method == "editMessageText" and "Отправляю файл" in params.get("text", "") \
                and self.upload_status_error:
            raise self.upload_status_error
        if method == "sendDocument" and self.document_error:
            raise self.document_error
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True


class FakeMedia:
    def __init__(self, directory):
        self.directory = directory
        self.calls = []

    @staticmethod
    def filename(title, episode, quality):
        return "anime.mkv"

    @asynccontextmanager
    async def prepare(self, source, filename, require_subtitle, language):
        self.calls.append((source, filename, require_subtitle, language))
        path = self.directory / filename
        path.write_bytes(b"mkv")
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.telegram = FakeTelegram()
        self.anime = AsyncMock()
        self.media = FakeMedia(self.directory)
        self.bot = Bot(Config("fake:token", 42, self.directory), self.store, self.telegram,
                       self.anime, self.media)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text, user=42, chat=42, kind="private", mid=1):
        return {"message": {"message_id": mid, "date": int(time.time()), "from": {"id": user},
                            "chat": {"id": chat, "type": kind}, "text": text}}

    def callback(self, action="pick", index=0, nonce=None):
        return self.callback_for(42, action, index, nonce)

    def callback_for(self, user, action="pick", index=0, nonce=None, session=None, message_id=None,
                     data=None):
        session = session or self.bot.sessions.get(user)
        return {"callback_query": {"id": f"callback-{user}", "from": {"id": user},
                                   "message": {"message_id": message_id or session.message_id,
                                               "chat": {"id": user, "type": "private"}},
                                   "data": data or f"{nonce or session.nonce}:{action}:{index}"}}

    async def test_owner_and_private_chat_are_checked_before_any_work(self):
        for update in (self.message("secret", user=43), self.message("secret", chat=-1, kind="group")):
            await self.bot.handle(update)
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.store.due(True), [])
        self.anime.validate.assert_not_called()

    async def test_auth_deletes_incoming_before_validation_and_encrypts_token(self):
        await self.bot.handle(self.message("/start"))
        self.assertEqual(self.store.get("awaiting_token"), "1")

        async def validate(token):
            self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": 2}), self.telegram.calls)
            self.assertEqual(token, "test-anime-secret")

        self.anime.validate.side_effect = validate
        await self.bot.handle(self.message("test-anime-secret", mid=2))
        self.assertEqual(self.store.token(42), "test-anime-secret")
        self.assertEqual(self.store.get("awaiting_token"), "0")
        self.assertNotIn(b"test-anime-secret", (self.directory / "bot.sqlite3").read_bytes())
        self.assertNotIn("test-anime-secret", repr(self.telegram.calls))

    async def test_bad_replacement_keeps_previous_token_and_waits_for_retry(self):
        self.store.save_token(42, "previous")
        await self.bot.handle(self.message("/auth"))
        self.anime.validate.side_effect = APIError("Токен не прошёл проверку", 401)
        await self.bot.handle(self.message("invalid", mid=2))
        self.assertEqual(self.store.token(42), "previous")
        self.assertEqual(self.store.get("awaiting_token"), "1")

    async def test_restart_preserves_authorization_and_deletion_queue(self):
        self.store.save_token(42, "saved")
        self.store.track(42, 900, 500)
        self.store.close()
        self.store = Store(self.directory)
        self.bot.store = self.store
        self.assertEqual(self.store.token(42), "saved")
        await self.bot.cleanup(all_messages=True)
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": 900}), self.telegram.calls)
        self.assertEqual(self.store.due(True), [])

    async def test_full_selection_downloads_sends_and_removes_service_menu(self):
        self.store.save_token(42, "saved")
        self.anime.search.return_value = [{"id": 1, "titles": {"ru": "Аниме"}, "year": 2024}]
        self.anime.episodes.return_value = [{"id": 2, "episodeFull": "1", "episodeType": "tv"}]
        self.anime.translations.return_value = [{"id": 3, "authorsSummary": "Переводчик", "type": "subRu", "typeLang": "ru"}]
        self.anime.available_qualities.return_value = [1080, 720]
        self.anime.media_source.return_value = "source"
        await self.bot.handle(self.message("Аниме"))
        first_id = self.bot.session.message_id
        await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "series_actions")
        await self.bot.handle(self.callback("add"))
        self.assertTrue(self.store.has_watchlist(42, 1))
        await self.bot.handle(self.callback("open"))
        self.assertEqual(self.bot.session.stage, "episodes")
        await self.bot.handle(self.callback("back"))
        self.assertEqual(self.bot.session.stage, "series_actions")
        await self.bot.handle(self.callback("back"))
        self.assertEqual(self.bot.session.stage, "series")
        await self.bot.handle(self.callback())
        await self.bot.handle(self.callback("open"))
        for _ in range(3):
            await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "qualities")
        self.assertEqual(self.bot.session.message_id, first_id)
        self.anime.available_qualities.assert_awaited_once_with(3, "saved")
        self.telegram.upload_status_error = APIError("temporary")
        await self.bot.handle(self.callback(index=1))
        self.assertIsNone(self.bot.session)
        upload = [p for m, p in self.telegram.calls if m == "sendDocument"][-1]
        self.assertIn("720p", upload["caption"])
        self.assertIn("Тип просмотра: Субтитры · Русский", upload["caption"])
        self.assertTrue(upload["document"].startswith("file:///"))
        self.anime.media_source.assert_awaited_once_with(3, 720, "saved")
        self.assertEqual(self.media.calls[0][2:], (True, "ru"))
        progress = self.store.get_watchlist(42, 1)
        self.assertEqual((progress["last_watched_episode_id"], progress["last_watched_episode_number"]), (2, "1"))
        self.assertIn("reply_markup", upload)
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": first_id}), self.telegram.calls)
        self.assertFalse((self.directory / "anime.mkv").exists())
        self.assertEqual(self.store.due(True), [])

    async def test_pagination_and_stale_double_tap(self):
        self.store.save_token(42, "saved")
        self.anime.search.return_value = [{"id": i, "titles": {"ru": str(i)}} for i in range(20)]
        await self.bot.handle(self.message("query"))
        stale = self.callback()
        await self.bot.handle(self.callback("page", 1))
        self.assertEqual(self.bot.session.page, 1)
        await self.bot.handle(stale)
        self.anime.episodes.assert_not_awaited()
        self.assertIn("устарело", self.telegram.calls[-1][1]["text"])
        await self.bot.handle(self.callback(index=-1))
        self.anime.episodes.assert_not_awaited()

    async def test_empty_translations_keep_episode_menu(self):
        self.bot.session = Session("episodes", [{"id": 1}])
        self.anime.translations.return_value = []
        await self.bot.render()
        await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "episodes")

    async def test_switching_viewing_type_filters_studios_and_discards_old_choice(self):
        self.store.save_token(42, "saved")
        self.bot.session = Session("episodes", [{"id": 2, "episodeFull": "1"}],
                                   selected={"series": {"id": 1, "titles": {"ru": "Аниме"}}})
        self.anime.translations.return_value = [
            {"id": 10, "type": "voiceRu", "authorsSummary": "Русская студия"},
            {"id": 11, "type": "subEn", "authorsSummary": "English team"},
            {"id": 12, "type": "voiceRu", "authorsSummary": "Другая студия"},
            {"id": 13, "type": "raw", "authorsSummary": "Original"},
        ]
        self.anime.available_qualities.return_value = [1080]
        self.anime.media_source.return_value = "source"
        await self.bot.render()
        menu_id = self.bot.session.message_id
        await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "translation_types")
        self.assertEqual([g.label for g in self.bot.session.items],
                         ["Озвучка · Русский", "Субтитры · Английский", "Оригинал (RAW)"])
        stale_type_callback = self.callback(index=1)
        await self.bot.handle(self.callback())
        self.assertEqual([t["id"] for t in self.bot.session.items], [10, 12])
        self.assertIn("Выбери озвучку", self.bot.menu()[0])
        await self.bot.handle(stale_type_callback)
        self.assertEqual(self.bot.session.stage, "translations")
        await self.bot.handle(self.callback(index=1))
        self.anime.available_qualities.assert_awaited_once_with(12, "saved")
        await self.bot.handle(self.callback("back"))
        await self.bot.handle(self.callback("back"))
        self.assertEqual(self.bot.session.stage, "translation_types")
        self.assertNotIn("translation_types", self.bot.session.selected)
        self.assertNotIn("translations", self.bot.session.selected)
        await self.bot.handle(self.callback(index=1))
        self.assertEqual([t["id"] for t in self.bot.session.items], [11])
        await self.bot.handle(self.callback())
        self.anime.available_qualities.assert_awaited_with(11, "saved")
        self.assertEqual(self.bot.session.message_id, menu_id)
        # Fetch once per episode, then filter locally when changing types.
        self.anime.translations.assert_awaited_once_with(2)
        await self.bot.handle(self.callback())
        caption = [p["caption"] for m, p in self.telegram.calls if m == "sendDocument"][-1]
        self.assertIn("Тип просмотра: Субтитры · Английский", caption)
        self.assertIn("English team", caption)
        self.assertNotIn("Другая студия", caption)

    async def test_raw_only_episode_reaches_quality_and_has_correct_summary(self):
        self.store.save_token(42, "saved")
        self.bot.session = Session("episodes", [{"id": 2, "episodeFull": "1"}],
                                   selected={"series": {"id": 1, "titles": {"ru": "Аниме"}}})
        self.anime.translations.return_value = [{"id": 20, "type": "raw", "typeKind": "raw",
                                                "typeLang": "ja", "title": "Original"}]
        self.anime.available_qualities.return_value = [1440, 1080]
        self.anime.media_source.return_value = "source"
        await self.bot.render()
        await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.items[0].label, "Оригинал (RAW)")
        await self.bot.handle(self.callback())
        self.assertIn("Выбери версию оригинала", self.bot.menu()[0])
        await self.bot.handle(self.callback())
        await self.bot.handle(self.callback())
        caption = [p["caption"] for m, p in self.telegram.calls if m == "sendDocument"][-1]
        self.assertIn("Тип просмотра: Оригинал (RAW)", caption)
        self.assertIn("Версия: ja · Original", caption)
        self.assertIn("1440p", caption)
        self.assertNotIn("Субтитры:", caption)
        self.assertEqual(self.media.calls[0][2], False)
        self.assertIsNone(self.bot.session)

    async def test_viewing_types_paginate_and_back_returns_to_episode(self):
        self.bot.session = Session("episodes", [{"id": 2}])
        self.anime.translations.return_value = [{"id": i, "typeKind": "sub", "typeLang": f"lang{i}"}
                                               for i in range(12)]
        await self.bot.render()
        await self.bot.handle(self.callback())
        self.assertEqual(len(self.bot.session.pages()), 2)
        await self.bot.handle(self.callback("page", 1))
        chosen = self.bot.session.items[8]
        await self.bot.handle(self.callback(index=8))
        self.assertEqual(self.bot.session.items, chosen.translations)
        await self.bot.handle(self.callback("back"))
        self.assertEqual(self.bot.session.page, 1)
        await self.bot.handle(self.callback("back"))
        self.assertEqual(self.bot.session.stage, "episodes")

    async def test_failed_delete_is_retried_and_does_not_claim_success(self):
        self.store.track(42, 99)
        self.telegram.delete_error = APIError("rate limit", 429, retry_after=50)
        await self.bot.cleanup()
        self.assertEqual(len(self.store.due(True)), 1)
        self.assertEqual(self.store.due(), [])
        self.telegram.delete_error = None
        await self.bot.cleanup(True)
        self.assertEqual(self.store.due(True), [])

    async def test_missing_messages_and_48_hour_limit(self):
        self.store.track(42, 1, created=time.time() - 49 * 3600)
        self.store.track(42, 2)
        self.telegram.delete_error = APIError("missing", 400, missing=True)
        await self.bot.cleanup()
        self.assertEqual(self.store.due(True), [])
        self.assertEqual([p["message_id"] for m, p in self.telegram.calls if m == "deleteMessage"], [2])

    async def test_logout_forgets_token_and_cancels_menu(self):
        self.store.save_token(42, "saved")
        self.bot.session = Session("series", [{"id": 1}])
        await self.bot.handle(self.message("/logout"))
        self.assertIsNone(self.store.token(42))
        self.assertIsNone(self.bot.session)

    async def test_update_drain_blocks_new_search_but_allows_cancel(self):
        self.store.save_token(42, "saved")
        (self.directory / "drain").touch()
        await self.bot.handle(self.message("query"))
        self.anime.search.assert_not_awaited()
        self.bot.session = Session("series", [{"id": 1}])
        await self.bot.render()
        await self.bot.handle(self.callback("cancel"))
        self.assertIsNone(self.bot.session)

    async def test_owner_can_allow_revoke_and_keep_user_states_isolated(self):
        await self.bot.handle(self.message("/allow 43", mid=1))
        self.assertTrue(self.store.is_allowed(43, 42))
        await self.bot.handle(self.message("/users", mid=2))
        users_notice = [params["text"] for method, params in self.telegram.calls if method == "sendMessage"][-1]
        self.assertIn("42", users_notice)
        self.assertIn("43", users_notice)

        await self.bot.handle(self.message("/start", user=43, chat=43, mid=3))
        self.assertTrue(self.store.awaiting_token(43))
        self.assertFalse(self.store.awaiting_token(42))
        await self.bot.handle(self.message("user-token", user=43, chat=43, mid=4))
        self.assertEqual(self.store.token(43), "user-token")
        self.assertIsNone(self.store.token(42))

        self.store.save_token(42, "owner-token")
        self.anime.search.side_effect = [
            [{"id": 1, "titles": {"ru": "Владелец"}}],
            [{"id": 2, "titles": {"ru": "Пользователь"}}],
        ]
        await self.bot.handle(self.message("owner query", mid=5))
        await self.bot.handle(self.message("user query", user=43, chat=43, mid=6))
        self.assertEqual(set(self.bot.sessions), {42, 43})
        self.assertEqual(self.bot.sessions[42].items[0]["id"], 1)
        self.assertEqual(self.bot.sessions[43].items[0]["id"], 2)
        self.assertNotEqual(self.bot.sessions[42].nonce, self.bot.sessions[43].nonce)

        # Scoped cleanup must not erase the owner's active menu.
        self.store.track(42, 801)
        self.store.track(43, 802)
        await self.bot.reset(43)
        remaining = self.store.due(True)
        self.assertTrue(any(chat_id == 42 and message_id == 801
                            for chat_id, message_id, _ in remaining))
        self.assertFalse(any(chat_id == 43 for chat_id, _, _ in remaining))

        await self.bot.handle(self.message("/revoke 42", mid=7))
        self.assertTrue(self.store.is_allowed(42, 42))
        await self.bot.handle(self.message("/revoke 43", mid=8))
        self.assertFalse(self.store.is_allowed(43, 42))

    async def test_callback_from_another_allowed_user_cannot_use_session(self):
        self.store.add_allowed_user(43, owner_id=42)
        self.store.save_token(42, "owner-token")
        self.store.save_token(43, "user-token")
        self.anime.search.side_effect = [
            [{"id": 1, "titles": {"ru": "Владелец"}}],
            [{"id": 2, "titles": {"ru": "Пользователь"}}],
        ]
        await self.bot.handle(self.message("owner", mid=1))
        await self.bot.handle(self.message("user", user=43, chat=43, mid=2))
        foreign_data = f"{self.bot.sessions[42].nonce}:pick:0"

        await self.bot.handle(self.callback_for(43, data=foreign_data))

        self.assertEqual(self.bot.sessions[42].stage, "series")
        self.assertEqual(self.bot.sessions[43].stage, "series")
        self.anime.episodes.assert_not_awaited()
        self.assertIn("устарело", self.telegram.calls[-1][1]["text"])

    async def test_watching_uses_saved_series_and_continue_selects_next_episode(self):
        self.store.save_token(42, "saved")
        self.store.add_watchlist(42, 10, "Сохранённое аниме")
        self.store.update_progress(42, 10, 101, "12")
        self.anime.episodes.return_value = [
            {"id": 101, "episodeFull": "12", "episodeInt": 12, "episodeType": "tv"},
            {"id": 102, "episodeFull": "13", "episodeInt": 13, "episodeType": "tv"},
            {"id": 103, "episodeFull": "14", "episodeInt": 14, "episodeType": "tv"},
        ]
        self.anime.translations.return_value = [{"id": 4, "type": "subRu", "typeLang": "ru"}]

        await self.bot.handle(self.message("/watching"))
        self.assertEqual(self.bot.session.stage, "watching")
        await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "saved_actions")
        await self.bot.handle(self.callback("continue"))

        self.assertEqual(self.bot.session.stage, "translation_types")
        self.anime.translations.assert_awaited_once_with(102)
        self.anime.search.assert_not_called()

    async def test_failed_document_send_does_not_advance_watch_progress(self):
        self.store.save_token(42, "saved")
        self.store.add_watchlist(42, 1, "Аниме")
        episode = {"id": 2, "episodeFull": "1", "episodeType": "tv"}
        translation = {"id": 3, "authorsSummary": "Переводчик", "type": "subRu", "typeLang": "ru"}
        from ani365_bot.translations import group_translations
        group = group_translations([translation])[0]
        self.bot.session = Session("qualities", [720], {
            "series": {"id": 1, "titles": {"ru": "Аниме"}},
            "episodes": episode,
            "translation_types": group,
            "translations": translation,
        })
        self.anime.media_source.return_value = "source"
        await self.bot.render()
        self.telegram.document_error = APIError("temporary", 500)

        await self.bot.handle(self.callback())

        watch = self.store.get_watchlist(42, 1)
        self.assertIsNone(watch["last_watched_episode_id"])
        self.assertFalse((self.directory / "anime.mkv").exists())

    async def test_persistent_notification_callback_checks_watchlist_owner(self):
        self.store.add_allowed_user(43, owner_id=42)
        self.store.add_watchlist(42, 7, "Только владельца")
        foreign_session = Session("watching", [])
        foreign_session.message_id = 700

        await self.bot.handle(self.callback_for(43, session=foreign_session, data="w:o:7:0"))

        self.anime.episodes.assert_not_awaited()
        self.assertNotIn(43, self.bot.sessions)
        self.assertIn("больше недоступна", self.telegram.calls[-1][1]["text"])

    async def test_persistent_notification_is_not_added_to_temporary_cleanup_queue(self):
        await self.bot.send_watch_notification({
            "user_id": 42, "series_id": 7, "episode_id": 70, "episode_number": "14",
            "mode": "subtitles", "title": "Фрирен",
        })

        self.assertEqual(self.store.due(True), [])
        message = [params for method, params in self.telegram.calls if method == "sendMessage"][-1]
        self.assertEqual(message["chat_id"], 42)
        self.assertIn("русскими субтитрами", message["text"])
        data = [button["callback_data"] for row in message["reply_markup"]["inline_keyboard"]
                for button in row]
        self.assertEqual(data, ["w:e:7:70", "w:o:7:0", "w:d:7:0"])

    async def test_watcher_delivers_through_persistent_bot_notification_sender(self):
        baseline = [{"id": 70, "episodeFull": "13"}]
        self.store.add_watchlist(42, 7, "Фрирен")
        self.store.configure_notifications(42, 7, True, "any", baseline, now=1)
        self.anime.episodes.return_value = [*baseline, {"id": 71, "episodeFull": "14"}]
        watcher = Watcher(self.store, self.anime, self.bot.send_watch_notification,
                          is_allowed=lambda user_id: self.store.is_allowed(user_id, 42),
                          clock=lambda: 100)

        await watcher.check_once()

        self.anime.episodes.assert_awaited_once_with(7)
        self.assertEqual(self.store.pending_notifications(now=101), [])
        self.assertEqual(self.store.due(True), [])
        notice = [params for method, params in self.telegram.calls if method == "sendMessage"][-1]
        self.assertIn("Серия 14", notice["text"])

    async def test_polling_persists_offset_and_expired_menus_are_cleaned(self):
        self.bot.session = Session("series", [{"id": 1}])
        await self.bot.render()
        menu_id = self.bot.session.message_id
        self.bot.session.touched = time.time() - 901
        original_call = self.telegram.call
        batches = [[dict(self.message("/cancel"), update_id=123)], None]

        async def call(method, **params):
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getUpdates":
                batch = batches.pop(0)
                if batch is None:
                    self.assertEqual(params["offset"], 124)
                    raise asyncio.CancelledError
                return batch
            return await original_call(method, **params)

        self.telegram.call = call
        with self.assertRaises(asyncio.CancelledError):
            await self.bot.run()
        self.assertEqual(self.store.get("offset"), "124")
        self.assertIsNone(self.bot.session)
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": menu_id}), self.telegram.calls)

    async def test_completion_notice_is_deleted_after_its_deadline(self):
        await self.bot.notice("Выбор завершён", ttl=-1)
        mid = self.telegram.next_id
        await self.bot.cleanup()
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": mid}), self.telegram.calls)
        self.assertEqual(self.store.due(True), [])


if __name__ == "__main__":
    unittest.main()
