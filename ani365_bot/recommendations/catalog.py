"""Public Shikimori GraphQL used to describe taste and find diverse candidates."""
from __future__ import annotations

import asyncio

import httpx


class CatalogError(RuntimeError):
    """Credential-free Shikimori failure."""


_ANIME_FIELDS = """
  id malId name russian score kind franchise airedOn { year }
  genres { id name }
"""

_RELATED_FIELDS = """
  related { relationKind anime { id malId name russian kind franchise airedOn { year } } }
"""


def normalize_anime(row, *, include_related=False):
    if not isinstance(row, dict):
        return None
    identifier = str(row.get("id") or "").strip()
    if not identifier.isdigit() or int(identifier) <= 0:
        return None
    aired = row.get("airedOn") if isinstance(row.get("airedOn"), dict) else {}
    try:
        year = int(aired.get("year") or 0) or None
    except (TypeError, ValueError):
        year = None
    genres = []
    raw_genres = row.get("genres")
    if isinstance(raw_genres, (list, tuple)):
        for genre in raw_genres:
            if isinstance(genre, dict) and str(genre.get("id") or "").strip() and str(genre.get("name") or "").strip():
                genres.append({"id": str(genre["id"]), "name": str(genre["name"])[:80]})
    related = []
    raw_related = row.get("related")
    if include_related and isinstance(raw_related, (list, tuple)):
        for relation in raw_related:
            if not isinstance(relation, dict):
                continue
            anime = normalize_anime(relation.get("anime"), include_related=False)
            kind = str(relation.get("relationKind") or "").strip().casefold()
            if anime and kind:
                related.append({"relation": kind, "anime": anime})
    return {
        "id": identifier,
        "mal_id": str(row.get("malId") or "").strip() or None,
        "title": str(row.get("russian") or row.get("name") or "").strip()[:180],
        "score": row.get("score"),
        "kind": str(row.get("kind") or "").strip().casefold()[:32] or None,
        "franchise": str(row.get("franchise") or "").strip()[:120] or None,
        "year": year,
        "genres": genres,
        "related": related,
    }


class ShikimoriCatalog:
    url = "https://shikimori.io/api/graphql"
    batch_size = 50

    def __init__(self, *, transport=None, delay=0.35, user_agent="ani365-mini-app"):
        self.transport = transport
        self.delay = delay
        self.user_agent = user_agent

    async def _query(self, query, variables):
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                response = await client.post(self.url, json={"query": query, "variables": variables},
                                             headers={"Accept": "application/json", "User-Agent": self.user_agent})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise CatalogError("Shikimori is temporarily unavailable") from None
        if not isinstance(payload, dict) or payload.get("errors"):
            raise CatalogError("Shikimori returned an unknown catalog response")
        rows = (payload.get("data") or {}).get("animes")
        if not isinstance(rows, list):
            raise CatalogError("Shikimori returned an unknown catalog response")
        return [row for row in rows if isinstance(row, dict)]

    async def metadata(self, anime_ids, *, related=False):
        identifiers, seen = [], set()
        for value in anime_ids or ():
            text = str(value).strip()
            if text.isdigit() and int(text) > 0 and text not in seen:
                identifiers.append(text)
                seen.add(text)
        fields = _ANIME_FIELDS + (_RELATED_FIELDS if related else "")
        query = f"query RecommendationMetadata($ids: String!) {{ animes(ids: $ids) {{ {fields} }} }}"
        result = {}
        for offset in range(0, len(identifiers), self.batch_size):
            if offset and self.delay:
                await asyncio.sleep(self.delay)
            rows = await self._query(query, {"ids": ",".join(identifiers[offset:offset + self.batch_size])})
            for row in rows:
                item = normalize_anime(row, include_related=related)
                if item:
                    result[item["id"]] = item
        return result

    async def candidates(self, genre_ids):
        """Mix ranked hits with other years instead of one shared top-ranked page."""
        genres = []
        for value in genre_ids or ():
            text = str(value).strip()
            if text.isdigit() and int(text) > 0 and text not in genres:
                genres.append(text)
            if len(genres) == 4:
                break
        seasons = [None, "2000_2012", "2013_2019", "2020_2024"]
        queries = []
        for index, genre in enumerate(genres):
            queries.append({"genre": genre, "order": "ranked", "season": None, "limit": 12})
            queries.append({"genre": genre, "order": "popularity", "season": seasons[index % len(seasons)],
                            "limit": 8})
        if genres:
            queries.append({"genre": genres[0], "order": "aired_on", "season": "1990_2009", "limit": 8})
        query = """query RecommendationCandidates($genre: String!, $order: OrderEnum!, $season: SeasonString, $limit: PositiveInt) {
          animes(genre: $genre, limit: $limit, order: $order, season: $season,
                 kind: "tv,movie,ona,ova", status: "released", censored: true) {
            id malId name russian score kind franchise airedOn { year }
            genres { id name }
            related { relationKind anime { id } }
          }
        }"""
        result, errors = {}, 0
        for offset, variables in enumerate(queries):
            if offset and self.delay:
                await asyncio.sleep(self.delay)
            try:
                rows = await self._query(query, variables)
            except CatalogError:
                errors += 1
                continue
            for row in rows:
                item = normalize_anime(row, include_related=True)
                if item:
                    result.setdefault(item["id"], item)
        if queries and errors == len(queries):
            raise CatalogError("Shikimori is temporarily unavailable")
        return list(result.values())
