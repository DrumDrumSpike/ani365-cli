import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from ani365_bot.api import APIError
from ani365_bot.seasonal import SeasonalCatalog, season_at
from ani365_bot.shikimori import Shikimori
from ani365_bot.store import Store


def timestamp(date):
    return datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp()


class SeasonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.now = timestamp('2026-10-21')
        self.shiki = AsyncMock()
        self.shiki.seasonal_animes.return_value = [
            {'id': '10', 'malId': '11', 'russian': 'Новинка', 'score': '8.2'}]
        self.shiki.animes.return_value = {'10': {'status': 'ongoing'}}
        self.anime = AsyncMock()
        self.anime.series_by_mal_id.return_value = [{'id': 20, 'myAnimeListId': 11, 'year': 2026}]
        self.anime.episodes.return_value = [
            {'id': 30, 'episodeInt': 1, 'episodeType': 'tv'},
            {'id': 31, 'episodeInt': 1, 'episodeType': 'preview'},
            {'id': 32, 'episodeInt': 2, 'episodeType': 'tv'}]
        self.anime.series_translations.return_value = [
            {'episodeId': 30, 'type': 'subRu'}, {'episodeId': 30, 'type': 'voiceRu'},
            {'episodeId': 31, 'type': 'subRu'}, {'episodeId': 32, 'type': 'subEn'}]
        self.catalog = SeasonalCatalog(self.store, self.shiki, self.anime,
                                       clock=lambda: self.now, sleep=AsyncMock())

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def test_available_count_ignores_trailers_english_and_duplicate_translations(self):
        await self.catalog.refresh()
        self.assertEqual(self.catalog.snapshot()['items'][0]['available_episodes'], 1)
        self.assertEqual(self.catalog.snapshot()['items'][0]['series_id'], 20)
        restored = SeasonalCatalog(self.store, self.shiki, self.anime, clock=lambda: self.now)
        self.assertEqual(restored.snapshot(), self.catalog.snapshot())

    async def test_daily_refresh_removes_completed_without_discovering_again(self):
        await self.catalog.refresh()
        self.now += 86400
        self.shiki.animes.return_value = {'10': {'status': 'released'}}
        await self.catalog.refresh()
        self.assertEqual(self.catalog.snapshot()['items'], [])
        self.shiki.seasonal_animes.assert_awaited_once()

    async def test_monthly_discovery_and_daily_availability(self):
        await self.catalog.refresh()
        await self.catalog.refresh()
        self.shiki.animes.assert_awaited_once()
        self.now += 86400
        self.anime.series_translations.return_value = []
        await self.catalog.refresh()
        self.assertEqual(self.catalog.snapshot()['items'], [])
        self.now = timestamp('2026-11-01')
        await self.catalog.refresh()
        self.assertEqual(self.shiki.seasonal_animes.await_count, 2)

    async def test_failure_preserves_snapshot_and_backs_off(self):
        await self.catalog.refresh()
        saved = self.store.seasonal_snapshot()
        self.now += 86400
        self.anime.series_translations.side_effect = APIError('Unavailable')
        await self.catalog.refresh()
        self.assertEqual(self.store.seasonal_snapshot(), saved)
        self.assertTrue(self.catalog.snapshot()['stale'])
        count = self.shiki.animes.await_count
        await self.catalog.refresh()
        self.assertEqual(self.shiki.animes.await_count, count)

    async def test_season_rollover_hides_previous_season_even_on_failure(self):
        await self.catalog.refresh()
        self.now = timestamp('2027-01-01')
        self.shiki.seasonal_animes.side_effect = APIError('Unavailable')
        await self.catalog.refresh()
        self.assertEqual(self.catalog.snapshot()['season'], 'winter_2027')
        self.assertEqual(self.catalog.snapshot()['items'], [])

    async def test_mismatched_or_ambiguous_mal_ids_never_match(self):
        for matches in ([{'id': 20, 'myAnimeListId': 12}],
                        [{'id': 20, 'myAnimeListId': 11}, {'id': 21, 'myAnimeListId': 11}]):
            self.anime.series_by_mal_id.return_value = matches
            self.now += 86400
            await self.catalog.refresh()
            self.assertEqual(self.catalog.snapshot()['items'], [])
        self.anime.episodes.assert_not_awaited()

    async def test_cancel_propagates_without_publishing(self):
        self.shiki.seasonal_animes.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.catalog.refresh()
        self.assertIsNone(self.store.seasonal_snapshot())

    def test_season_boundary_uses_moscow(self):
        self.assertEqual(season_at(timestamp('2026-09-30T22:00:00')), ('fall_2026', '2026-10'))


class SeasonalSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_premiere_filter_rejects_long_running_finished_and_unknown_dates(self):
        def row(id, date, status='ongoing'):
            return {'id': str(id), 'malId': str(id), 'status': status, 'airedOn': {'date': date}}
        def handler(request):
            return httpx.Response(200, json={'data': {'animes': [
                row(1, '1999-10-20'), row(2, '2026-10-02'), row(3, '2026-07-01'),
                row(4, None), row(5, '2026-10-01', 'released')]}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch('ani365_bot.shikimori.httpx.AsyncClient', return_value=client):
            result = await Shikimori('', '', '').seasonal_animes('fall_2026')
        self.assertEqual([row['id'] for row in result], ['2'])
