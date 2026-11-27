"""Confirm candidates on Anime365 by an exact MAL id. Shikimori ids are not a fallback."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from ..api import APIError, title


def _positive_mal(value):
    text = str(value or "").strip()
    return text if text.isdigit() and int(text) > 0 else None


def _poster(row, host):
    value = row.get("posterUrlSmall") or row.get("posterUrl")
    parts = urlsplit(value) if isinstance(value, str) else None
    if (not parts or parts.scheme != "https" or parts.hostname != host or parts.username
            or parts.password or parts.query or parts.fragment or not parts.path.startswith("/posters/")):
        return None
    return value


async def resolve_candidates(anime, candidates, *, anime_url, store=None, concurrency=3):
    """Keep only rows whose MAL id Anime365 itself confirms.

    A missing MAL id is skipped. The Shikimori id is never sent to
    ``series_by_mal_id``.
    """
    host = urlsplit(anime_url).hostname
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def resolve_one(candidate):
        mal_id = _positive_mal(candidate.get("mal_id"))
        if not mal_id:
            return None
        async with semaphore:
            try:
                rows = await anime.series_by_mal_id(mal_id)
            except (APIError, TypeError, ValueError):
                return None
        if not rows:
            return None
        row = rows[0]
        try:
            series_id = int(row["id"])
        except (KeyError, TypeError, ValueError):
            return None
        if str(row.get("myAnimeListId") or "") != mal_id:
            return None
        resolved = dict(candidate)
        resolved.update({
            "mal_id": mal_id,
            "anime365_series_id": series_id,
            "title": title(row) if row.get("titles") else (candidate.get("title") or title(row)),
            "year": row.get("year") if row.get("year") not in (None, "") else candidate.get("year"),
            "series_type": row.get("typeTitle") or row.get("type") or candidate.get("kind"),
            "poster_url": _poster(row, host),
        })
        if store is not None:
            store.save_external_id(series_id, "shikimori", candidate["id"])
        return resolved

    resolved = await asyncio.gather(*(resolve_one(candidate) for candidate in candidates or ()))
    return [item for item in resolved if item is not None]
