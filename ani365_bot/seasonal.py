"""Shared seasonal discovery: monthly membership, daily status/availability."""
import asyncio
from datetime import datetime
import logging
import time
from zoneinfo import ZoneInfo

from .api import number, title
from .translations import viewing_type

LOG = logging.getLogger(__name__)


def season_at(timestamp):
    date = datetime.fromtimestamp(timestamp, ZoneInfo('Europe/Moscow'))
    season = ('winter', 'spring', 'summer', 'fall')[(date.month - 1) // 3]
    return f'{season}_{date.year}', date.strftime('%Y-%m')


class SeasonalCatalog:
    refresh_interval = 24 * 60 * 60
    retry_interval = 15 * 60

    def __init__(self, store, shikimori, anime, *, clock=time.time, sleep=asyncio.sleep):
        self.store, self.shikimori, self.anime = store, shikimori, anime
        self.clock, self.sleep = clock, sleep
        self.lock = asyncio.Lock()
        self.retry_at = 0
        self.failed = False

    def snapshot(self):
        season, _ = season_at(self.clock())
        saved = self.store.seasonal_snapshot()
        if not saved or saved['season'] != season:
            return {'season': season, 'items': [], 'updated_at': None, 'stale': True}
        return {key: saved[key] for key in ('season', 'items', 'updated_at')} | {
            'stale': self.failed or self.clock() - saved['updated_at'] >= self.refresh_interval}

    async def refresh(self):
        async with self.lock:
            now = self.clock()
            if now < self.retry_at:
                return
            season, month = season_at(now)
            saved = self.store.seasonal_snapshot()
            discover = not saved or saved['season'] != season or saved['month'] != month
            if not discover and now - saved['updated_at'] < self.refresh_interval:
                return
            try:
                candidates = await self.shikimori.seasonal_animes(season) if discover else saved['candidates']
                statuses = await self.shikimori.animes([row['id'] for row in candidates])
                items = []
                for candidate in candidates:
                    status = statuses.get(str(candidate['id']))
                    if status is None:
                        raise ValueError('Incomplete status batch')
                    if status.get('status') != 'ongoing':
                        continue
                    # Use the explicit GraphQL MAL bridge, never fuzzy title matches.
                    mal_id = candidate.get('malId')
                    if not mal_id:
                        continue
                    matches = await self.anime.series_by_mal_id(mal_id)
                    await self.sleep(.7)
                    matches = [row for row in matches if str(row.get('myAnimeListId')) == str(mal_id)]
                    if len(matches) != 1:
                        continue
                    row = matches[0]
                    episodes = await self.anime.episodes(row['id'])
                    await self.sleep(.7)
                    episode_ids = {str(ep['id']) for ep in episodes
                                   if ep.get('episodeType') in ('tv', 'ona', 'ova') and number(ep.get('episodeInt')) > 0}
                    translations = await self.anime.series_translations(row['id'])
                    await self.sleep(.7)
                    available = {str(tr.get('episodeId')) for tr in translations
                                 if viewing_type(tr) in (('sub', 'ru'), ('voice', 'ru'))
                                 and str(tr.get('episodeId')) in episode_ids}
                    if not available:
                        continue
                    items.append({'series_id': int(row['id']), 'title': candidate.get('russian') or title(row),
                                  'poster_url': (candidate.get('poster') or {}).get('previewUrl') or row.get('posterUrlSmall'),
                                  'year': row.get('year'), 'series_type': row.get('typeTitle') or row.get('type'),
                                  'score': candidate.get('score'), 'available_episodes': len(available)})
                # Publish only complete refreshes; upstream failures keep the last good snapshot.
                self.store.save_seasonal_snapshot({'season': season, 'month': month,
                                                  'candidates': candidates, 'items': items, 'updated_at': now})
                self.failed = False
            except Exception as exc:
                self.failed = True
                self.retry_at = now + self.retry_interval
                LOG.warning('Seasonal refresh deferred (type=%s)', type(exc).__name__)

    async def run(self):
        while True:
            await self.refresh()
            await self.sleep(15 * 60)
