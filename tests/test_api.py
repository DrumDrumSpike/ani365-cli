import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ani365_bot.api import APIError, Anime365, Telegram, media_source, qualities
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

    async def test_telegram_file_error_is_safely_classified(self):
        client = AsyncMock()
        client.post.return_value = response({
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: realpath failed for /jobs/private-title.mkv",
        })
        with self.assertLogs("ani365_bot.api", level="WARNING") as logs:
            with self.assertRaises(APIError) as raised:
                await Telegram(client, "secret:token").call("sendDocument")
        self.assertIn("не смог прочитать", str(raised.exception))
        self.assertIn("reason=filesystem", " ".join(logs.output))
        self.assertNotIn("private-title", " ".join(logs.output))

    async def test_local_document_is_uploaded_as_streaming_multipart(self):
        client = AsyncMock()
        client.post_file.return_value = response({"ok": True, "result": {"message_id": 7}})
        result = await Telegram(client, "secret:token").call(
            "sendDocument", chat_id=42, document="file:///jobs/anime-8-1080p.mkv", caption="Anime")
        self.assertEqual(result["message_id"], 7)
        args = client.post_file.await_args.args
        self.assertEqual(args[1], {"chat_id": 42, "caption": "Anime"})
        self.assertEqual(args[2], "document")
        self.assertEqual(args[3], Path("/jobs/anime-8-1080p.mkv"))
        client.post.assert_not_awaited()

    async def test_telegram_network_failure_logs_method_and_stage(self):
        client = AsyncMock()
        client.post.side_effect = NetworkError("secret", stage="response")
        with self.assertLogs("ani365_bot.api", level="WARNING") as logs:
            with self.assertRaises(APIError):
                await Telegram(client, "secret:token").call("editMessageText")
        message = " ".join(logs.output)
        self.assertIn("editMessageText failed (network stage=response)", message)
        self.assertNotIn("secret:token", message)

    async def test_multipart_retries_only_before_request_can_be_accepted(self):
        client = HTTPClient()
        success = response({"ok": True})
        with patch("ani365_bot.http.asyncio.to_thread", new_callable=AsyncMock,
                   side_effect=[NetworkError("connect", "connect", True), success]) as thread, \
                patch("ani365_bot.http.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await client.post_file("http://telegram/send", {}, "document", Path("x"))
        self.assertIs(result, success)
        self.assertEqual(thread.await_count, 2)
        sleep.assert_awaited_once_with(1)

        with patch("ani365_bot.http.asyncio.to_thread", new_callable=AsyncMock,
                   side_effect=NetworkError("response", "response", False)) as thread:
            with self.assertRaises(NetworkError):
                await client.post_file("http://telegram/send", {}, "document", Path("x"))
        self.assertEqual(thread.await_count, 1)

    async def test_invalid_api_json_and_missing_data(self):
        client = AsyncMock()
        for value in (Response(200, b"<html>error</html>"), response({"error": "no data"})):
            client.get.return_value = value
            with self.assertRaises(APIError):
                await Anime365(client, "https://example.org/api").validate("secret")

    async def test_translations_include_voice_raw_and_other_languages(self):
        rows = [{"id": i, "type": kind, "priority": i} for i, kind in
                enumerate(("subRu", "voiceRu", "raw", "subEn", "voiceEn", "subDe"), 1)]
        client = AsyncMock()
        client.get.return_value = response({"data": rows})
        result = await Anime365(client, "https://example.org/api").translations(42)
        self.assertEqual({x["id"] for x in result}, {1, 2, 3, 4, 5, 6})
        params = client.get.call_args.kwargs["params"]
        self.assertEqual(params["episodeId"], 42)
        self.assertEqual(params["isActive"], 1)
        self.assertNotIn("type", params)


class ShapeTests(unittest.TestCase):
    def test_embed_variants_deduplicate_qualities_and_discard_urls(self):
        for key in ("stream", "streams"):
            data = {key: [{"height": 1080, "urls": ["https://secret-url"]},
                          {"quality": "720p", "urlList": ["https://secret-url"]},
                          {"height": 1080, "url": "/video"}, {"height": 480}]}
            self.assertEqual(qualities(data), [1080, 720])
        self.assertEqual(qualities({"data": {"stream": [{"height": 480, "url": "/video"}]}}), [480])
        self.assertEqual(qualities({}), [])

    def test_selected_media_urls_and_subtitles_are_resolved_only_on_completion(self):
        data = {"streams": [{"height": 1080, "urls": ["//cdn.example/video.m3u8"]},
                            {"height": 720, "url": "/720.mp4"}],
                "subtitles": {"url": "/subtitles.ass"}}
        source = media_source(data, 1080, "https://smotret-anime.app")
        self.assertEqual(source.urls, ("https://cdn.example/video.m3u8",))
        self.assertEqual(source.subtitle_url, "https://smotret-anime.app/subtitles.ass")

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

    def test_multipart_upload_streams_file_with_exact_content_length(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "anime-8-1080p.mkv"
            path.write_bytes(b"mkv-data")
            connection = MagicMock()
            connection.getresponse.return_value.status = 200
            connection.getresponse.return_value.read.return_value = b'{"ok":true}'
            with patch("ani365_bot.http.http.client.HTTPConnection", return_value=connection):
                result = HTTPClient()._post_file(
                    "http://telegram:8081/botsecret/sendDocument",
                    {"chat_id": 42, "caption": "Аниме"}, "document", path, 60)
            sent = b"".join(call.args[0] for call in connection.send.call_args_list)
            length = next(call.args[1] for call in connection.putheader.call_args_list
                          if call.args[0] == "Content-Length")
            self.assertEqual(int(length), len(sent))
            self.assertIn(b"mkv-data", sent)
            self.assertIn("Аниме".encode(), sent)
            self.assertEqual(result.json(), {"ok": True})

if __name__ == "__main__":
    unittest.main()
