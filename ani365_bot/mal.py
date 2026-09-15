"""Small, public MyAnimeList v2 metadata client.

MAL is deliberately an optional metadata and cover provider.  It never
participates in playback, user lists, or title matching: callers must already
have an Anime365-confirmed MyAnimeList ID.
"""
from urllib.parse import urlsplit

import httpx


class MyAnimeListError(Exception):
    """Credential-free diagnostic safe for logs and background tasks."""


class MyAnimeList:
    api_base = "https://api.myanimelist.net/v2"
    fields = "id,title,main_picture,alternative_titles,start_date,media_type,num_episodes"

    def __init__(self, client_id, *, transport=None):
        self.client_id = str(client_id or "").strip()
        self.transport = transport

    @property
    def configured(self):
        return bool(self.client_id)

    @staticmethod
    def _poster_url(value):
        parts = urlsplit(value) if isinstance(value, str) else None
        if not parts or parts.scheme != "https" or not parts.hostname \
                or not parts.hostname.endswith(".myanimelist.net") \
                or parts.username or parts.password or parts.query or parts.fragment \
                or not parts.path.startswith("/images/anime/"):
            return None
        return value

    @classmethod
    def public_metadata(cls, row):
        """Return only the small public fields that may enter SQLite."""
        if not isinstance(row, dict):
            return {"title": None, "poster_url": None, "kind": None, "aired_on": None}
        picture = row.get("main_picture") if isinstance(row.get("main_picture"), dict) else {}
        alternative = row.get("alternative_titles") if isinstance(row.get("alternative_titles"), dict) else {}
        title = str(row.get("title") or alternative.get("en") or alternative.get("ja") or "").strip()[:500]
        return {
            "title": title or None,
            "poster_url": cls._poster_url(picture.get("large") or picture.get("medium")),
            "kind": str(row.get("media_type") or "").strip()[:32] or None,
            "aired_on": str(row.get("start_date") or "").strip()[:32] or None,
        }

    async def anime(self, anime_id):
        try:
            anime_id = int(anime_id)
        except (TypeError, ValueError):
            raise MyAnimeListError("Некорректный MyAnimeList ID.") from None
        if anime_id <= 0 or not self.configured:
            raise MyAnimeListError("MyAnimeList metadata не настроен.")
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                response = await client.get(
                    f"{self.api_base}/anime/{anime_id}", params={"fields": self.fields}, headers={
                        "X-MAL-CLIENT-ID": self.client_id,
                        "Accept": "application/json",
                        "User-Agent": "ani365-mini-app",
                    })
                response.raise_for_status()
                row = response.json()
        except (httpx.HTTPError, ValueError):
            raise MyAnimeListError("MyAnimeList metadata временно недоступен.") from None
        if not isinstance(row, dict) or str(row.get("id") or "") != str(anime_id):
            raise MyAnimeListError("MyAnimeList вернул неполные metadata.")
        return self.public_metadata(row)
