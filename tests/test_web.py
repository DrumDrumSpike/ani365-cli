import hashlib
import hmac
import json
import re
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx

from ani365_bot.api import MediaSource
from ani365_bot.config import Config
from ani365_bot.store import Store
from ani365_bot.web import create_app, validate_init_data


BOT_TOKEN = "123456:mini-app-test-token"


def init_data(user_id, *, auth_date=None, token=BOT_TOKEN):
    values = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.config = Config(BOT_TOKEN, 42, data_dir=Path(self.temp.name),
                             media_dir=Path(self.temp.name) / "jobs", web_cookie_secure=False,
                             anime_token="server-anime365-token")
        self.anime = type("Anime", (), {})()
        self.anime.search = AsyncMock(return_value=[])
        self.anime.episodes = AsyncMock(return_value=[{
            "id": 700, "episodeFull": "7", "episodeInt": 7, "episodeType": "tv"
        }])
        self.anime.translations = AsyncMock(return_value=[{"id": 800, "type": "subRu"}])
        self.anime.available_qualities = AsyncMock(return_value=[1080])
        self.anime.media_source = AsyncMock(return_value=MediaSource(
            ("https://cdn.example/video.m3u8?signature=private",),
            "https://cdn.example/subtitles.ass?signature=private"))
        self.app = create_app(self.config, self.store, self.anime)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.store.close()
        self.temp.cleanup()

    @staticmethod
    def headers(user_id):
        return {"X-Telegram-Init-Data": init_data(user_id)}

    def test_init_data_validation_rejects_tampering_and_expiry(self):
        self.assertEqual(validate_init_data(init_data(42), BOT_TOKEN), 42)
        with self.assertRaises(ValueError):
            validate_init_data(init_data(42) + "tampered", BOT_TOKEN)
        with self.assertRaises(ValueError):
            validate_init_data(init_data(42, auth_date=int(time.time()) - 90000), BOT_TOKEN)

    def test_allowlist_owner_and_revocation_gate_every_private_api(self):
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        self.assertEqual(self.client.get("/api/me", headers=self.headers(99)).status_code, 403)
        self.assertEqual(self.client.get("/api/me", headers=self.headers(42)).status_code, 200)
        self.store.add_allowed_user(99, owner_id=42)
        self.assertEqual(self.client.get("/api/me", headers=self.headers(99)).status_code, 200)
        self.store.revoke_allowed_user(99, 42)
        self.assertEqual(self.client.get("/api/library", headers=self.headers(99)).status_code, 403)

    def test_shared_config_anime365_token_serves_allowed_user_without_personal_token(self):
        self.store.add_allowed_user(7, owner_id=42)
        self.store.add_watchlist(7, 55, "Shared token title")
        config = replace(self.config, anime_token="server-anime365-token")
        app = create_app(config, self.store, self.anime)
        client = TestClient(app)
        try:
            result = client.post("/api/play", headers=self.headers(7), json={
                "series_id": 55, "episode_id": 700, "translation_id": 800, "quality": 1080,
            })
        finally:
            client.close()
        self.assertEqual(result.status_code, 200)
        self.anime.media_source.assert_awaited_with(800, 1080, "server-anime365-token")
        self.assertIsNone(self.store.token(7))

    def test_hentai_catalog_is_separate_from_anime365_and_has_no_shikimori_routes(self):
        hentai = type("Hentai", (), {})()
        hentai.search = AsyncMock(return_value=[{
            "id": 55, "titles": {"ru": "Отдельный каталог"}, "year": 2026,
            "typeTitle": "TV", "posterUrlSmall": "https://h365-art.org/posters/55.jpg",
        }])
        hentai.episodes = AsyncMock(return_value=[{
            "id": 1700, "episodeFull": "1", "episodeInt": 1,
        }])
        hentai.translations = AsyncMock(return_value=[{"id": 1800, "type": "subRu"}])
        hentai.available_qualities = AsyncMock(return_value=[720])
        hentai.media_source = AsyncMock(return_value=MediaSource(
            ("https://cdn.example/hentai.m3u8?signature=private",), None))
        config = replace(self.config, hentai_url="https://hentai365.ru/api", hentai_token="hentai-secret")
        app = create_app(config, self.store, self.anime, hentai)
        client = TestClient(app)
        try:
            catalog = client.get("/api/hentai/catalog?query=test", headers=self.headers(42))
            self.assertEqual(catalog.status_code, 200)
            item = catalog.json()["items"][0]
            self.assertEqual(item["series_id"], 1_000_000_000_055)
            self.assertEqual(item["poster_url"], "https://h365-art.org/posters/55.jpg")
            self.assertNotIn("hentai-secret", catalog.text)
            added = client.post("/api/library", headers=self.headers(42), json={
                "series_id": item["series_id"], "title": item["title"], "provider": "hentai365",
            })
            self.assertEqual(added.status_code, 200)
            detail = client.get(f"/api/library/{item['series_id']}", headers=self.headers(42))
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["item"]["provider"], "hentai365")
            self.assertEqual(detail.json()["item"]["poster_url"], "https://h365-art.org/posters/55.jpg")
            library = client.get("/api/library", headers=self.headers(42))
            self.assertEqual(library.json()["items"][0]["poster_url"], "https://h365-art.org/posters/55.jpg")
            hentai.episodes.assert_awaited_with(55)
            play = client.post("/api/play", headers=self.headers(42), json={
                "series_id": item["series_id"], "episode_id": 1700, "translation_id": 1800, "quality": 720,
            })
            self.assertEqual(play.status_code, 200)
            self.assertNotIn("hentai-secret", play.text)
            hentai.media_source.assert_awaited_with(1800, 720, "hentai-secret")
            self.assertEqual(
                client.get(f"/api/library/{item['series_id']}/shikimori-rates", headers=self.headers(42)).status_code,
                404,
            )
            self.assertEqual(
                client.patch(f"/api/library/{item['series_id']}/notifications", headers=self.headers(42),
                             json={"enabled": True, "mode": "any"}).status_code,
                422,
            )
        finally:
            client.close()

    def test_cross_user_library_and_stream_ticket_are_not_visible(self):
        self.store.add_allowed_user(7, owner_id=42)
        self.store.add_allowed_user(8, owner_id=42)
        self.store.add_watchlist(7, 55, "Private title")
        self.assertEqual(self.client.get("/api/library/55", headers=self.headers(8)).status_code, 404)
        ticket = self.app.state.tickets.create(7, "https://cdn.example/signed-private")
        self.assertEqual(self.client.get(f"/api/stream/{ticket}", headers=self.headers(8)).status_code, 404)

    def test_playback_completion_updates_v2_progress_without_persisting_urls_or_token(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.save_token(42, "anime365-secret-token")
        result = self.client.post("/api/play", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 700, "translation_id": 800, "quality": 1080,
        })
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("anime365-secret-token", result.text)
        self.assertIn("media_url", result.json())
        progress = self.client.post("/api/progress", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 700, "position_seconds": 89, "duration_seconds": 100,
        })
        self.assertFalse(progress.json()["completed"])
        progress = self.client.post("/api/progress", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 700, "position_seconds": 90, "duration_seconds": 100,
        })
        self.assertTrue(progress.json()["completed"])
        self.assertEqual(self.store.get_watchlist(42, 55)["last_watched_episode_id"], 700)
        raw_database = (Path(self.temp.name) / "bot.sqlite3").read_bytes()
        self.assertNotIn(b"signed-private", raw_database)
        self.assertNotIn(b"signature=private", raw_database)
        self.assertNotIn(b"anime365-secret-token", raw_database)

    def test_play_returns_owner_bound_subtitle_ticket_and_serves_vtt(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.save_token(42, "anime365-secret-token")
        result = self.client.post("/api/play", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 700, "translation_id": 800, "quality": 1080,
        })
        self.assertEqual(result.status_code, 200)
        subtitle_url = result.json()["subtitle_url"]
        self.assertTrue(subtitle_url.startswith("/api/subtitles/"))
        self.assertNotIn("subtitles.ass?signature=private", result.text)

        def download_subtitle(_url, directory):
            path = directory / "subtitles.vtt"
            path.write_text("WEBVTT\\n\\n00:00.000 --> 00:01.000\\nТест\\n", encoding="utf-8")
            return path

        with patch.object(self.app.state.downloads.media, "_download_subtitle", download_subtitle):
            response = self.client.get(subtitle_url, headers=self.headers(42))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/vtt"))
        self.assertIn("WEBVTT", response.text)
        self.store.add_allowed_user(7, owner_id=42)
        self.assertEqual(self.client.get(subtitle_url, headers=self.headers(7)).status_code, 404)

    def test_ended_marks_short_or_unknown_duration_as_watched(self):
        self.store.add_watchlist(42, 55, "Title")
        result = self.client.post("/api/progress", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 700, "position_seconds": 0, "duration_seconds": 0,
            "ended": True,
        })
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["completed"])

    def test_manual_episode_completion_updates_only_its_owner_progress(self):
        self.store.add_watchlist(42, 55, "Title")
        self.anime.episodes = AsyncMock(return_value=[
            {"id": 700, "episodeFull": "7", "episodeInt": 7},
            {"id": 701, "episodeFull": "8", "episodeInt": 8},
        ])
        result = self.client.post("/api/library/55/episodes/701/watched", headers=self.headers(42))
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["completed"])
        self.assertEqual(self.store.get_watchlist(42, 55)["last_watched_episode_id"], 701)
        self.store.add_allowed_user(7, owner_id=42)
        self.assertEqual(
            self.client.post("/api/library/55/episodes/701/watched", headers=self.headers(7)).status_code,
            404,
        )

    def test_travel_queues_only_unwatched_episodes_with_matching_translation_profile(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.update_progress(42, 55, {"id": 700, "episodeFull": "7"})
        self.anime.episodes = AsyncMock(return_value=[
            {"id": 700, "episodeFull": "7", "episodeInt": 7, "episodeType": "tv"},
            {"id": 701, "episodeFull": "8", "episodeInt": 8, "episodeType": "tv"},
            {"id": 702, "episodeFull": "9", "episodeInt": 9, "episodeType": "tv"},
            {"id": 703, "episodeFull": "10", "episodeInt": 10, "episodeType": "tv"},
        ])
        self.anime.translations = AsyncMock(side_effect=lambda episode_id: {
            701: [{"id": 801, "type": "subRu", "authorsSummary": "Studio"}],
            702: [{"id": 802, "type": "subRu", "authorsSummary": "Studio"}],
            703: [{"id": 803, "type": "voiceRu", "authorsSummary": "Studio"}],
        }.get(episode_id, []))
        self.app.state.downloads.enqueue = MagicMock()
        result = self.client.post("/api/travel", headers=self.headers(42), json={
            "series_id": 55, "anchor_episode_id": 701, "translation_id": 801,
            "quality": 1080, "count": 3, "delivery": "browser",
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["queued"], 2)
        self.assertEqual(result.json()["skipped_episodes"], ["10"])
        self.assertEqual(self.app.state.downloads.enqueue.call_count, 2)
        queued = self.store.list_download_jobs(42)
        self.assertEqual(sorted(job["episode_id"] for job in queued), [701, 702])
        self.store.add_allowed_user(7, owner_id=42)
        denied = self.client.post("/api/travel", headers=self.headers(7), json={
            "series_id": 55, "anchor_episode_id": 701, "translation_id": 801,
            "quality": 1080, "count": 1, "delivery": "browser",
        })
        self.assertEqual(denied.status_code, 404)

    def test_clear_downloads_hides_only_owner_finished_jobs(self):
        self.store.add_watchlist(42, 55, "Visible title")
        self.store.add_allowed_user(7, owner_id=42)
        self.store.add_watchlist(7, 55, "Other title")
        own = self.store.create_download_job("a" * 16, 42, 55, 700, "7", 800, 1080, "browser")
        other = self.store.create_download_job("b" * 16, 7, 55, 700, "7", 800, 1080, "browser")
        self.store.claim_download_job(own["id"])
        self.store.claim_download_job(other["id"])
        self.store.finish_download_job(own["id"], "sent")
        self.store.finish_download_job(other["id"], "sent")
        response = self.client.post("/api/downloads/clear", headers=self.headers(42))
        self.assertEqual(response.json(), {"hidden": 1})
        self.assertEqual(self.client.get("/api/downloads", headers=self.headers(42)).json()["items"], [])
        visible = self.client.get("/api/downloads", headers=self.headers(7)).json()["items"]
        self.assertEqual(visible[0]["series_title"], "Other title")
        self.assertEqual(self.store.download_job(42, own["id"])["status"], "sent")

    def test_library_notification_settings_are_baselined_and_private(self):
        self.store.add_watchlist(42, 55, "Title")
        enabled = self.client.patch("/api/library/55/notifications", headers=self.headers(42),
                                    json={"enabled": True, "mode": "subtitles"})
        self.assertEqual(enabled.status_code, 200)
        self.assertTrue(enabled.json()["notifications_enabled"])
        self.assertEqual(enabled.json()["notification_mode"], "subtitles")
        self.assertTrue(enabled.json()["notification_baselined"])
        self.store.add_allowed_user(7, owner_id=42)
        self.assertEqual(self.client.patch("/api/library/55/notifications", headers=self.headers(7),
                                           json={"enabled": True, "mode": "any"}).status_code, 404)
        disabled = self.client.patch("/api/library/55/notifications", headers=self.headers(42),
                                     json={"enabled": False, "mode": "subtitles"})
        self.assertFalse(disabled.json()["notifications_enabled"])

    def test_range_proxy_streams_partial_content_only_for_ticket_owner(self):
        seen = []

        def upstream(request):
            seen.append(request.headers.get("range"))
            return httpx.Response(206, headers={"content-type": "video/mp4", "content-length": "3",
                                                "content-range": "bytes 2-4/10", "accept-ranges": "bytes"},
                                  content=b"234")

        app = create_app(self.config, self.store, self.anime,
                         proxy_transport=httpx.MockTransport(upstream))
        client = TestClient(app)
        try:
            ticket = app.state.tickets.create(42, "https://cdn.example/signed")
            response = client.get(f"/api/stream/{ticket}", headers={**self.headers(42), "Range": "bytes=2-4"})
        finally:
            client.close()
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"234")
        self.assertEqual(response.headers["content-range"], "bytes 2-4/10")
        self.assertEqual(seen, ["bytes=2-4"])

    def test_hls_proxy_rewrites_playlist_children_to_owner_bound_tickets(self):
        seen = []

        def upstream(request):
            seen.append(str(request.url))
            if request.url.path == "/master.m3u8":
                return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                                      text='''#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example/key.bin?secret=private"
#EXTINF:6,
segments/one.ts?signature=private
#EXT-X-STREAM-INF:BANDWIDTH=1000
variants/720.m3u8?signature=private
''')
            if request.url.path == "/segments/one.ts":
                return httpx.Response(206, headers={"content-type": "video/mp2t", "content-length": "3",
                                                      "content-range": "bytes 2-4/10", "accept-ranges": "bytes"},
                                      content=b"234")
            if request.url.path == "/variants/720.m3u8":
                return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                                      text="#EXTM3U\n#EXTINF:6,\n../segments/two.ts?signature=private\n")
            return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"key")

        app = create_app(self.config, self.store, self.anime,
                         proxy_transport=httpx.MockTransport(upstream))
        client = TestClient(app)
        try:
            ticket = app.state.tickets.create(42, "https://cdn.example/master.m3u8?signature=private")
            playlist = client.get(f"/api/stream/{ticket}", headers=self.headers(42))
            child_urls = re.findall(r"/api/stream/[A-Za-z0-9_-]+", playlist.text)
            media_urls = [line for line in playlist.text.splitlines() if line.startswith("/api/stream/")]
            segment_url, variant_url = media_urls
            self.store.add_allowed_user(7, owner_id=42)
            denied = client.get(segment_url, headers=self.headers(7))
            segment = client.get(segment_url, headers={**self.headers(42), "Range": "bytes=2-4"})
            variant = client.get(variant_url, headers=self.headers(42))
        finally:
            client.close()
        self.assertEqual(playlist.status_code, 200)
        self.assertTrue(playlist.headers["content-type"].startswith("application/vnd.apple.mpegurl"))
        self.assertGreaterEqual(len(child_urls), 3)
        self.assertIn('URI="/api/stream/', playlist.text)
        self.assertNotIn("signature=private", playlist.text)
        self.assertNotIn("secret=private", playlist.text)
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(segment.status_code, 206)
        self.assertEqual(segment.content, b"234")
        self.assertIn("/api/stream/", variant.text)
        self.assertNotIn("signature=private", variant.text)
        self.assertIn("https://cdn.example/master.m3u8?signature=private", seen)
        self.assertIn("https://cdn.example/segments/one.ts?signature=private", seen)

    def test_hls_proxy_rejects_unsafe_child_urls_without_fetching_them(self):
        seen = []

        def upstream(request):
            seen.append(str(request.url))
            return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                                  text="#EXTM3U\n#EXTINF:6,\nhttp://127.0.0.1/private.ts\n")

        app = create_app(self.config, self.store, self.anime,
                         proxy_transport=httpx.MockTransport(upstream))
        client = TestClient(app)
        try:
            ticket = app.state.tickets.create(42, "https://cdn.example/master.m3u8")
            response = client.get(f"/api/stream/{ticket}", headers=self.headers(42))
        finally:
            client.close()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(seen, ["https://cdn.example/master.m3u8"])

    def test_shikimori_status_is_private_and_unconfigured_connect_is_rejected(self):
        status = self.client.get("/api/shikimori/status", headers=self.headers(42))
        self.assertEqual(status.json(), {
            "configured": False, "connected": False, "background_import": None,
        })
        self.assertEqual(self.client.post("/api/shikimori/connect", headers=self.headers(42)).status_code, 409)
        self.assertEqual(self.client.get("/api/shikimori/status", headers=self.headers(99)).status_code, 403)

    def test_catalog_normalizes_anime365_results_and_uses_only_confirmed_cached_poster(self):
        self.anime.search = AsyncMock(return_value=[{
            "id": 55, "titles": {"ru": "Русское название", "en": "English title"},
            "year": 2026, "typeTitle": "TV", "myAnimeListId": 700,
        }])
        self.store.save_external_id(55, "shikimori", "700")
        self.store.save_external_anime_metadata(
            "shikimori", "700", poster_url="https://shikimori.one/system/animes/preview/700.jpg")

        result = self.client.get("/api/catalog?query=название", headers=self.headers(42))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["items"], [{
            "series_id": 55, "title": "Русское название", "year": 2026,
            "series_type": "TV", "poster_url": "https://shikimori.one/system/animes/preview/700.jpg",
        }])

    def test_catalog_resolves_shikimori_permalink_by_exact_mal_id_with_anime365_poster(self):
        self.anime.series_by_mal_id = AsyncMock(return_value=[{
            "id": 39395, "titles": {"en": "Roll Over and Die"}, "year": 2026,
            "typeTitle": "ТВ сериал", "myAnimeListId": 61587,
            "posterUrlSmall": "https://smotret-anime.app/posters/39395.example.200x600.0.jpg",
        }])
        slug = "61587-omae-gotoki-ga-maou-ni-kateru-to-omouna-to-yuusha-party-wo-tsuihou-sareta-node-outo-de-kimama-ni-kurashitai"
        result = self.client.get("/api/catalog?query=" + slug, headers=self.headers(42))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["items"][0], {
            "series_id": 39395, "title": "Roll Over and Die", "year": 2026,
            "series_type": "ТВ сериал",
            "poster_url": "https://smotret-anime.app/posters/39395.example.200x600.0.jpg",
        })
        self.anime.series_by_mal_id.assert_awaited_once_with("61587")
        self.anime.search.assert_not_awaited()

    def test_catalog_numeric_year_can_be_added_to_library(self):
        result = self.client.post("/api/library", headers=self.headers(42), json={
            "series_id": 39395, "title": "Roll Over and Die", "year": 2026,
            "series_type": "ТВ сериал",
        })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.get_watchlist(42, 39395)["year"], "2026")

    def test_mini_app_shell_and_assets_are_not_cached_between_deploys(self):
        self.assertEqual(self.client.get("/").headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(self.client.get("/assets/app.js").headers["cache-control"], "no-store, max-age=0")
        hls = self.client.get("/assets/hls-1.7.3.min.js")
        self.assertEqual(hls.status_code, 200)
        self.assertTrue(hls.headers["content-type"].startswith(("text/javascript", "application/javascript")))
        self.assertEqual(hls.headers["cache-control"], "public, max-age=31536000, immutable")
        hero = self.client.get("/assets/anime-night.webp")
        self.assertEqual(hero.status_code, 200)
        self.assertTrue(hero.headers["content-type"].startswith("image/webp"))
        self.assertEqual(hero.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(self.client.get("/assets/../web.py").status_code, 404)

    def test_background_shikimori_import_is_owner_bound_and_durable(self):
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "123")
        shikimori = type("Shikimori", (), {})()
        shikimori.user_rates = AsyncMock(return_value=[])
        self.app.state.shikimori = shikimori
        queued = self.client.post("/api/shikimori/import/background", headers=self.headers(42),
                                  json={"statuses": ["watching", "planned"]})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(set(queued.json()["background_import"]["statuses"]), {"watching", "planned"})
        self.assertIsNotNone(self.store.external_import_state(42, "shikimori"))
        self.store.add_allowed_user(7, owner_id=42)
        self.assertEqual(self.client.post("/api/shikimori/import/background", headers=self.headers(7),
                                          json={"statuses": ["watching"]}).status_code, 409)

    def test_shikimori_sync_setting_is_private_and_persistent(self):
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "123")
        result = self.client.patch("/api/shikimori/settings", headers=self.headers(42),
                                   json={"sync_enabled": False, "auto_complete": True})
        self.assertFalse(result.json()["sync_enabled"])
        self.assertTrue(result.json()["auto_complete"])
        self.assertFalse(self.store.external_account_status(42, "shikimori")["sync_enabled"])
        self.assertTrue(self.store.external_account_status(42, "shikimori")["auto_complete"])
        self.assertEqual(self.client.patch("/api/shikimori/settings", headers=self.headers(99),
                                           json={"sync_enabled": True}).status_code, 403)

    def test_shikimori_status_update_is_owner_bound_and_updates_only_selected_status(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "123")
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "700", "status": "watching",
            "episodes": 2, "title": "Title",
        }])
        self.store.link_external_user_rate(42, "shikimori", "50", 55)
        shikimori = type("Shikimori", (), {})()
        shikimori.update_user_rate = AsyncMock()
        self.app.state.shikimori = shikimori
        result = self.client.patch("/api/library/55/shikimori-status", headers=self.headers(42),
                                   json={"status": "completed"})
        self.assertEqual(result.json(), {"status": "completed"})
        self.assertEqual(shikimori.update_user_rate.await_args.args, ("access", "50"))
        self.assertEqual(shikimori.update_user_rate.await_args.kwargs, {"status": "completed"})
        self.assertEqual(self.store.external_user_rate(42, "shikimori", "50")["status"], "completed")
        self.store.add_allowed_user(7, owner_id=42)
        self.assertEqual(self.client.patch("/api/library/55/shikimori-status", headers=self.headers(7),
                                           json={"status": "watching"}).status_code, 404)

    def test_auto_complete_marks_only_the_last_episode_completed_in_shikimori(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "123")
        self.store.set_external_auto_complete(42, "shikimori", True)
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "700", "status": "watching",
            "episodes": 0, "title": "Title",
        }])
        self.store.link_external_user_rate(42, "shikimori", "50", 55)
        self.anime.episodes = AsyncMock(return_value=[
            {"id": 700, "episodeFull": "1", "episodeInt": 1},
            {"id": 701, "episodeFull": "2", "episodeInt": 2},
        ])
        shikimori = type("Shikimori", (), {})()
        shikimori.update_user_rate = AsyncMock()
        self.app.state.shikimori = shikimori
        first = self.client.post("/api/progress", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 700, "position_seconds": 100,
            "duration_seconds": 100, "ended": True,
        })
        self.assertTrue(first.json()["completed"])
        for _ in range(20):
            if shikimori.update_user_rate.await_count:
                break
            time.sleep(0.01)
        self.assertEqual(shikimori.update_user_rate.await_args.kwargs, {"episodes": 1})
        self.assertEqual(self.store.external_user_rate(42, "shikimori", "50")["status"], "watching")
        shikimori.update_user_rate.reset_mock()
        result = self.client.post("/api/progress", headers=self.headers(42), json={
            "series_id": 55, "episode_id": 701, "position_seconds": 100,
            "duration_seconds": 100, "ended": True,
        })
        self.assertTrue(result.json()["completed"])
        for _ in range(20):
            if shikimori.update_user_rate.await_count:
                break
            time.sleep(0.01)
        self.assertEqual(shikimori.update_user_rate.await_args.args, ("access", "50"))
        self.assertEqual(shikimori.update_user_rate.await_args.kwargs,
                         {"episodes": 2, "status": "completed"})
        self.assertEqual(self.store.external_user_rate(42, "shikimori", "50")["status"], "completed")

    def test_shikimori_import_uses_anime_id_mal_bridge_and_alternative_title_searches(self):
        self.store.save_external_account(42, "shikimori", "shiki-access", "shiki-refresh",
                                         time.time() + 3600, "123")
        shikimori = type("Shikimori", (), {})()
        shikimori.user_rates = AsyncMock(return_value=[{
            "id": 50, "target_id": 700, "target_type": "Anime", "status": "watching",
            "episodes": 7, "target": {"id": 700, "russian": "Тайтл"},
        }])
        shikimori.anime = AsyncMock(return_value={
            "id": 700, "russian": "Не найдено", "name": "Roman", "english": ["Тайтл"],
            "mal_id": None,
        })
        self.app.state.shikimori = shikimori
        self.anime.search = AsyncMock(side_effect=lambda query: [{
            "id": 55, "titles": {"ru": "Тайтл"}, "year": 2024, "typeTitle": "TV",
            "myAnimeListId": 700,
        }] if query == "Тайтл" else [])
        self.anime.series_by_mal_id = AsyncMock(return_value=[{
            "id": 55, "titles": {"ru": "Тайтл"}, "year": 2024, "typeTitle": "TV",
            "myAnimeListId": 700,
        }])
        imported = self.client.post("/api/shikimori/import", headers=self.headers(42),
                                    json={"statuses": ["watching"]})
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["unmatched"][0]["external_rate_id"], "50")
        self.assertEqual(imported.json()["unmatched_total"], 1)
        automatically_linked = self.client.post("/api/shikimori/imports/auto-link", headers=self.headers(42))
        self.assertEqual(automatically_linked.json()["linked"], 1)
        self.assertEqual(automatically_linked.json()["remaining"], 0)
        self.assertTrue(self.store.has_watchlist(42, 55))

        # Re-importing preserves the mapping.  The per-title screen continues
        # to expose the same verified bridge without accepting a fuzzy result.
        imported = self.client.post("/api/shikimori/import", headers=self.headers(42),
                                    json={"statuses": ["watching"]})
        self.assertEqual(imported.json()["linked"], 1)
        candidates = self.client.get("/api/shikimori/imports/50/candidates", headers=self.headers(42))
        self.assertTrue(candidates.json()["candidates"][0]["verified_mal"])
        self.assertEqual(candidates.json()["candidates"][0]["match_reason"], "MAL ID совпал")
        self.assertIn("Тайтл", [call.args[0] for call in self.anime.search.await_args_list])
        linked = self.client.post("/api/shikimori/imports/50/link", headers=self.headers(42),
                                  json={"series_id": 55})
        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.json()["verified_mal"])
        self.assertTrue(self.store.has_watchlist(42, 55))
        self.assertEqual(self.store.get_watchlist(42, 55)["last_watched_episode_id"], 700)
        self.assertEqual(self.client.get("/api/shikimori/imports", headers=self.headers(99)).status_code, 403)

    def test_shikimori_import_hydrates_titleless_rates_in_one_batch(self):
        self.store.save_external_account(42, "shikimori", "shiki-access", "shiki-refresh",
                                         time.time() + 3600, "123")
        shikimori = type("Shikimori", (), {})()
        shikimori.user_rates = AsyncMock(return_value=[{
            "id": 51, "target_id": 701, "target_type": "Anime", "status": "planned", "episodes": 0,
        }])
        shikimori.animes = AsyncMock(return_value={"701": {
            "id": 701, "russian": "Нормальное название", "name": "Normal title",
        }})
        self.app.state.shikimori = shikimori
        result = self.client.post("/api/shikimori/import", headers=self.headers(42),
                                  json={"statuses": ["planned"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["unmatched"][0]["title"], "Нормальное название")
        self.assertEqual(shikimori.animes.await_args.args[0], ["701"])

    def test_censored_shikimori_title_can_link_by_exact_mal_id_without_page_or_poster(self):
        """A censored Shikimori card remains importable through the stable ID bridge."""
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "61587-rate", "external_anime_id": "61587",
            "status": "planned", "episodes": 0,
            "title": "Неужели ты думаешь, что сможешь победить Короля Демонов?",
        }])
        self.anime.series_by_mal_id = AsyncMock(return_value=[{
            "id": 39395, "titles": {"ru": "Неужели ты думаешь, что сможешь победить Короля Демонов?"},
            "year": 2026, "typeTitle": "TV", "myAnimeListId": 61587,
        }])

        found = self.client.get("/api/shikimori/imports?linked=false&query=61587",
                                headers=self.headers(42))
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["items"][0]["external_rate_id"], "61587-rate")

        linked = self.client.post("/api/shikimori/imports/61587-rate/auto-link",
                                  headers=self.headers(42))
        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.json()["verified_mal"])
        self.assertTrue(self.store.has_watchlist(42, 39395))
        self.assertEqual(self.store.external_user_rate(42, "shikimori", "61587-rate")
                         ["anime365_series_id"], 39395)
        self.anime.search.assert_not_awaited()

    def test_large_shikimori_import_replies_after_first_metadata_batch(self):
        self.store.save_external_account(42, "shikimori", "shiki-access", "shiki-refresh",
                                         time.time() + 3600, "123")
        upstream = [{"id": index, "target_id": 1000 + index, "target_type": "Anime",
                     "status": "planned", "episodes": 0} for index in range(51)]
        shikimori = type("Shikimori", (), {})()
        shikimori.user_rates = AsyncMock(return_value=upstream)
        shikimori.animes = AsyncMock(side_effect=lambda ids: {
            str(item_id): {"id": item_id, "russian": f"Title {item_id}"} for item_id in ids
        })
        self.app.state.shikimori = shikimori
        result = self.client.post("/api/shikimori/import", headers=self.headers(42),
                                  json={"statuses": ["planned"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["imported"], 51)
        self.assertTrue(result.json()["metadata_refreshing"])
        self.assertEqual(len(shikimori.animes.await_args_list[0].args[0]), 50)

    def test_library_exposes_only_owner_linked_shikimori_poster(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.save_external_id(55, "shikimori", "700")
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "700", "status": "watching",
            "episodes": 2, "title": "Title",
        }])
        self.store.link_external_user_rate(42, "shikimori", "50", 55)
        self.store.save_external_anime_metadata(
            "shikimori", "700", poster_url="https://shikimori.one/system/animes/preview/700.jpg")
        self.assertEqual(self.client.get("/api/library", headers=self.headers(42)).json()["items"][0]["poster_url"],
                         "https://shikimori.one/system/animes/preview/700.jpg")
        self.store.add_allowed_user(7, owner_id=42)
        self.assertNotIn("poster_url", self.client.get("/api/library", headers=self.headers(7)).json()["items"])

    def test_shikimori_import_reuses_shared_metadata_for_another_user(self):
        self.store.add_allowed_user(7, owner_id=42)
        self.store.save_external_account(7, "shikimori", "access", "refresh", time.time() + 3600, "7000")
        self.store.save_external_anime_metadata(
            "shikimori", "701", title="Shared title",
            poster_url="https://shikimori.one/system/animes/preview/701.jpg")
        shikimori = type("Shikimori", (), {})()
        shikimori.user_rates = AsyncMock(return_value=[{
            "id": 51, "target_id": 701, "target_type": "Anime", "status": "watching", "episodes": 1,
        }])
        shikimori.animes = AsyncMock()
        self.app.state.shikimori = shikimori
        result = self.client.post("/api/shikimori/import", headers=self.headers(7),
                                  json={"statuses": ["watching"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.store.external_user_rate(7, "shikimori", "51")["title"], "Shared title")
        shikimori.animes.assert_not_awaited()

    def test_library_backfills_old_linked_shikimori_posters_in_one_batch(self):
        self.store.add_watchlist(42, 55, "Old linked title")
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "123")
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "700", "status": "watching",
            "episodes": 1, "title": "Old linked title",
        }])
        self.store.link_external_user_rate(42, "shikimori", "50", 55)
        shikimori = type("Shikimori", (), {})()
        shikimori.animes = AsyncMock(return_value={"700": {
            "id": 700, "russian": "Old linked title", "kind": "tv",
            "image": {"preview": "/system/animes/preview/700.jpg"},
        }})
        self.app.state.shikimori = shikimori
        library = self.client.get("/api/library", headers=self.headers(42))
        self.assertEqual(library.status_code, 200)
        self.assertEqual(library.json()["items"][0]["poster_url"],
                         "https://shikimori.one/system/animes/preview/700.jpg")
        self.assertEqual(shikimori.animes.await_args.args, (["700"],))
        self.client.get("/api/library", headers=self.headers(42))
        self.assertEqual(shikimori.animes.await_count, 1)

    def test_library_uses_graphql_poster_for_new_shikimori_title(self):
        self.store.add_watchlist(42, 40361, "Убивая юность")
        self.store.save_external_account(42, "shikimori", "access", "refresh", time.time() + 3600, "123")
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "62391", "status": "watching",
            "episodes": 0, "title": "Убивая юность",
        }])
        self.store.link_external_user_rate(42, "shikimori", "50", 40361)
        shikimori = type("Shikimori", (), {})()
        shikimori.animes = AsyncMock(return_value={"62391": {
            "id": 62391, "russian": "Убивая юность", "image": {"preview": "/assets/globals/missing_preview.jpg"},
        }})
        shikimori.posters = AsyncMock(return_value={"62391": {
            "id": "62391", "russian": "Убивая юность", "poster": {
                "previewUrl": "https://shikimori.io/uploads/poster/animes/62391/preview-hash.webp",
            },
        }})
        self.app.state.shikimori = shikimori
        library = self.client.get("/api/library", headers=self.headers(42))
        self.assertEqual(library.status_code, 200)
        self.assertEqual(library.json()["items"][0]["poster_url"],
                         "https://shikimori.io/uploads/poster/animes/62391/preview-hash.webp")
        self.assertEqual(shikimori.posters.await_args.args, (["62391"],))

    def test_card_shikimori_link_is_private_and_does_not_change_global_mapping(self):
        self.store.add_watchlist(42, 55, "Local title")
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "700", "status": "watching",
            "episodes": 2, "title": "Shikimori title",
        }])
        shikimori = type("Shikimori", (), {})()
        shikimori.anime = AsyncMock(return_value={
            "id": 700, "russian": "Shikimori title", "kind": "tv", "image": {"preview": "/system/animes/preview/700.jpg"},
        })
        self.app.state.shikimori = shikimori
        found = self.client.get("/api/library/55/shikimori-rates?query=Shikimori", headers=self.headers(42))
        self.assertEqual(found.json()["items"][0]["external_rate_id"], "50")
        linked = self.client.post("/api/library/55/shikimori-link", headers=self.headers(42),
                                  json={"external_rate_id": "50"})
        self.assertEqual(linked.status_code, 200)
        self.assertEqual(self.store.external_rate_for_series(42, "shikimori", 55)["external_rate_id"], "50")
        self.assertEqual(self.store.external_series_id("shikimori", "700"), None)
        self.assertEqual(self.client.get("/api/library", headers=self.headers(42)).json()["items"][0]["poster_url"],
                         "https://shikimori.one/system/animes/preview/700.jpg")
        self.store.add_allowed_user(7, owner_id=42)
        self.store.add_watchlist(7, 55, "Other local title")
        self.assertEqual(self.client.get("/api/library/55/shikimori-rates?query=Shikimori", headers=self.headers(7)).json()["items"], [])
        self.assertEqual(self.client.post("/api/library/55/shikimori-link", headers=self.headers(7),
                                          json={"external_rate_id": "50"}).status_code, 404)

    def test_library_new_episodes_uses_cached_watcher_data_for_owner_only(self):
        self.store.add_watchlist(42, 55, "Title")
        self.store.update_progress(42, 55, {"id": 700, "episodeFull": "7"})
        self.store.update_available(42, 55, {"id": 701, "episodeFull": "8"})
        payload = self.client.get("/api/library", headers=self.headers(42)).json()
        self.assertEqual([item["series_id"] for item in payload["new_episodes"]], [55])
        self.store.add_allowed_user(7, owner_id=42)
        self.assertEqual(self.client.get("/api/library", headers=self.headers(7)).json()["new_episodes"], [])

    def test_completed_shikimori_titles_are_in_library_but_not_active_sections(self):
        self.store.add_watchlist(42, 55, "Finished")
        self.store.update_progress(42, 55, {"id": 700, "episodeFull": "7"})
        self.store.update_available(42, 55, {"id": 701, "episodeFull": "8"})
        self.store.record_playback_progress(42, 55, {"id": 700, "episodeFull": "7"}, 20, 100)
        self.store.import_external_rates(42, "shikimori", [{
            "external_rate_id": "50", "external_anime_id": "700", "status": "completed",
            "episodes": 7, "title": "Finished",
        }])
        self.store.link_external_user_rate(42, "shikimori", "50", 55)
        payload = self.client.get("/api/library", headers=self.headers(42)).json()
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["continue"], [])
        self.assertEqual(payload["new_episodes"], [])
        self.assertEqual([item["series_id"] for item in payload["groups"]["completed"]], [55])


if __name__ == "__main__":
    unittest.main()
