import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ani365_bot.api import APIError, Anime365, Telegram, qualities, subtitle_translation
from ani365_bot.config import Config
from ani365_bot.http import HTTPClient, NetworkError, NoRedirects, Response
from ani365_bot.store import Store


def response(data, code=200):
    return Response(code, json.dumps(data).encode())


class APITests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_uses_me_and_never_exposes_response_body(self):
        client = AsyncMock()
        client.get.return_value = response({"data": {"isLogined": True}})
        api = Anime365(client, "https://example.org/api")
        await api.validate("secret")
        self.assertEqual(client.get.call_args.args[0], "https://example.org/api/me")
        self.assertEqual(client.get.call_args.kwargs["params"], {"access_token": "secret"})
        client.get.return_value = response({"error": "echo secret"}, 403)
        with self.assertRaises(APIError) as raised:
            await api.validate("secret")
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(raised.exception.code, 403)

    async def test_network_retries_are_bounded(self):
        client = AsyncMock()
        client.get.side_effect = NetworkError("secret-url")
        api = Anime365(client, "https://example.org/api")
        with patch("ani365_bot.api.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(APIError) as raised:
                await api.search("query")
        self.assertEqual(client.get.await_count, 3)
        self.assertNotIn("secret", str(raised.exception))

    async def test_pagination_and_literal_query(self):
        client = AsyncMock()
        client.get.side_effect = [response({"data": [{"id": i, "titles": {"ru": str(i)}} for i in range(100)]}),
                                  response({"data": [{"id": 101, "titles": {"ru": "[anime("}}]})]
        rows = await Anime365(client, "https://example.org/api").search("[anime(")
        self.assertEqual(rows[0]["id"], 101)
        self.assertEqual(len(rows), 101)
        self.assertEqual(client.get.call_args_list[1].kwargs["params"]["offset"], 100)

    async def test_telegram_errors_do_not_expose_bot_token_or_response(self):
        client = AsyncMock()
        client.post.return_value = response({"ok": False, "error_code": 429,
                                            "description": "secret", "parameters": {"retry_after": 15}})
        with self.assertRaises(APIError) as raised:
            await Telegram(client, "secret:token").call("getMe")
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(raised.exception.retry_after, 15)

    async def test_invalid_api_json_and_missing_data(self):
        client = AsyncMock()
        for value in (Response(200, b"<html>error</html>"), response({"error": "no data"})):
            client.get.return_value = value
            with self.assertRaises(APIError):
                await Anime365(client, "https://example.org/api").validate("secret")


class ShapeTests(unittest.TestCase):
    def test_embed_variants_deduplicate_qualities_and_discard_urls(self):
        for key in ("stream", "streams"):
            data = {key: [{"height": 1080, "urls": ["https://secret-url"]},
                          {"quality": "720p", "urlList": ["https://secret-url"]},
                          {"height": 1080, "url": "/video"}, {"height": 480}]}
            self.assertEqual(qualities(data), [1080, 720])
        self.assertEqual(qualities({"data": {"stream": [{"height": 480, "url": "/video"}]}}), [480])
        self.assertEqual(qualities({}), [])

    def test_filters_out_voice_translations(self):
        self.assertTrue(subtitle_translation({"typeKind": "sub"}))
        self.assertTrue(subtitle_translation({"type": "subRu"}))
        self.assertFalse(subtitle_translation({"type": "voiceRu"}))
        self.assertFalse(subtitle_translation({}))

    def test_config_accepts_existing_env_names_without_api_id(self):
        with patch.dict(os.environ, {"token_BotFather": "fake:token", "TELEGRAM_ID": "42"}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.owner_id, 42)
        self.assertEqual(cfg.bot_token, "fake:token")

    def test_invalid_config_rejected_without_echoing_secret(self):
        with patch.dict(os.environ, {"BOT_TOKEN": "secret", "OWNER_ID": "42"}, clear=True):
            with self.assertRaises(ValueError) as raised:
                Config.from_env()
            self.assertNotIn("secret", str(raised.exception))
        with patch.dict(os.environ, {"BOT_TOKEN": "fake:token", "OWNER_ID": "42", "ANI365_BASE_URL": "http://example.org"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_lost_key_cannot_silently_destroy_saved_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            store = Store(path)
            store.save_token(42, "secret")
            store.close()
            (path / "token.key").unlink()
            with self.assertRaises(ValueError):
                Store(path)

    def test_http_does_not_follow_redirects_with_credential(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.org"))

    def test_http_adapter_encodes_query_and_disables_redirects(self):
        class FakeResponse:
            code = 200

            def read(self, limit):
                return b'{"data": []}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        with patch("ani365_bot.http.build_opener") as opener:
            opener.return_value.open.return_value = FakeResponse()
            result = HTTPClient()._request("GET", "https://example.org/api", {"query": "a&b"}, None, 20)
            self.assertEqual(result.json(), {"data": []})
            request = opener.return_value.open.call_args.args[0]
            self.assertIn("query=a%26b", request.full_url)
            self.assertIsInstance(opener.call_args.args[0], NoRedirects)


if __name__ == "__main__":
    unittest.main()
