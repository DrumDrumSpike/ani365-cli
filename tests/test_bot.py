import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from ani365_bot.api import APIError
from ani365_bot.bot import Bot, Session
from ani365_bot.config import Config
from ani365_bot.store import Store


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.delete_error = None

    async def call(self, method, **params):
        self.calls.append((method, params))
        if method == "deleteMessage" and self.delete_error:
            raise self.delete_error
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        return True


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory)
        self.telegram = FakeTelegram()
        self.anime = AsyncMock()
        self.bot = Bot(Config("fake:token", 42, self.directory), self.store, self.telegram, self.anime)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text, user=42, chat=42, kind="private", mid=1):
        return {"message": {"message_id": mid, "date": int(time.time()), "from": {"id": user},
                            "chat": {"id": chat, "type": kind}, "text": text}}

    def callback(self, action="pick", index=0, nonce=None):
        session = self.bot.session
        return {"callback_query": {"id": "callback", "from": {"id": 42},
                                   "message": {"message_id": session.message_id,
                                               "chat": {"id": 42, "type": "private"}},
                                   "data": f"{nonce or session.nonce}:{action}:{index}"}}

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

    async def test_full_selection_and_back_use_one_menu_without_download(self):
        self.store.save_token(42, "saved")
        self.anime.search.return_value = [{"id": 1, "titles": {"ru": "Аниме"}, "year": 2024}]
        self.anime.episodes.return_value = [{"id": 2, "episodeFull": "1", "episodeType": "tv"}]
        self.anime.translations.return_value = [{"id": 3, "authorsSummary": "Переводчик", "typeLang": "ru"}]
        self.anime.available_qualities.return_value = [1080, 720]
        await self.bot.handle(self.message("Аниме"))
        first_id = self.bot.session.message_id
        await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "episodes")
        await self.bot.handle(self.callback("back"))
        self.assertEqual(self.bot.session.stage, "series")
        for _ in range(3):
            await self.bot.handle(self.callback())
        self.assertEqual(self.bot.session.stage, "qualities")
        self.assertEqual(self.bot.session.message_id, first_id)
        self.anime.available_qualities.assert_awaited_once_with(3, "saved")
        await self.bot.handle(self.callback(index=1))
        self.assertIsNone(self.bot.session)
        notices = [p["text"] for m, p in self.telegram.calls if m == "sendMessage"]
        self.assertIn("720p", notices[-1])
        self.assertIn("пока не подключены", notices[-1])
        self.assertIn(("deleteMessage", {"chat_id": 42, "message_id": first_id}), self.telegram.calls)
        self.assertFalse(any(method in ("sendDocument", "sendVideo") for method, _ in self.telegram.calls))
        self.assertEqual(len(self.store.due(True)), 1)

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
