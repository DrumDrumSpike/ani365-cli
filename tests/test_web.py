import hashlib
import hmac
import json
import tempfile
import time
import unittest
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
                             media_dir=Path(self.temp.name) / "jobs", web_cookie_secure=False)
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
        self.assertEqual(result.json()["queued"], 1)
        self.assertEqual(result.json()["skipped_episodes"], ["10"])
        self.assertEqual(self.app.state.downloads.enqueue.call_count, 1)
        queued = self.store.list_download_jobs(42)
        self.assertEqual([job["episode_id"] for job in queued], [702])
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

    def test_shikimori_status_is_private_and_unconfigured_connect_is_rejected(self):
        status = self.client.get("/api/shikimori/status", headers=self.headers(42))
        self.assertEqual(status.json(), {"configured": False, "connected": False})
        self.assertEqual(self.client.post("/api/shikimori/connect", headers=self.headers(42)).status_code, 409)
        self.assertEqual(self.client.get("/api/shikimori/status", headers=self.headers(99)).status_code, 403)

    def test_shikimori_import_requires_user_selection_or_verified_mal_mapping(self):
        self.store.save_external_account(42, "shikimori", "shiki-access", "shiki-refresh",
                                         time.time() + 3600, "123")
        shikimori = type("Shikimori", (), {})()
        shikimori.user_rates = AsyncMock(return_value=[{
            "id": 50, "target_id": 600, "target_type": "Anime", "status": "watching",
            "episodes": 7, "target": {"id": 600, "russian": "Тайтл"},
        }])
        shikimori.anime = AsyncMock(return_value={"id": 600, "russian": "Тайтл", "mal_id": 700})
        self.app.state.shikimori = shikimori
        self.anime.search = AsyncMock(return_value=[{
            "id": 55, "titles": {"ru": "Тайтл"}, "year": 2024, "typeTitle": "TV",
            "myAnimeListId": 700,
        }])
        imported = self.client.post("/api/shikimori/import", headers=self.headers(42),
                                    json={"statuses": ["watching"]})
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()["unmatched"][0]["external_rate_id"], "50")
        candidates = self.client.get("/api/shikimori/imports/50/candidates", headers=self.headers(42))
        self.assertTrue(candidates.json()["candidates"][0]["verified_mal"])
        linked = self.client.post("/api/shikimori/imports/50/link", headers=self.headers(42),
                                  json={"series_id": 55})
        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.json()["verified_mal"])
        self.assertTrue(self.store.has_watchlist(42, 55))
        self.assertEqual(self.store.get_watchlist(42, 55)["last_watched_episode_id"], 700)
        self.assertEqual(self.client.get("/api/shikimori/imports", headers=self.headers(99)).status_code, 403)


if __name__ == "__main__":
    unittest.main()
