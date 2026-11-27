"""Public Shikimori GraphQL used to describe taste and find diverse candidates."""
from __future__ import annotations

import asyncio
import re

import httpx


class CatalogError(RuntimeError):
    """Credential-free Shikimori failure."""


_ANIME_FIELDS = """
  id malId name russian score kind franchise airedOn { year }
  description
  genres { id name russian kind }
"""

_MARKUP = re.compile(r"\[/?[a-z]+(?:=[^\]]+)?\]", re.IGNORECASE)
AWARD_WINNING_ID = "114"

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
    genres, themes = [], []
    raw_genres = row.get("genres")
    if isinstance(raw_genres, (list, tuple)):
        for genre in raw_genres:
            if not isinstance(genre, dict):
                continue
            genre_id = str(genre.get("id") or "").strip()
            name = str(genre.get("russian") or genre.get("name") or "").strip()
            if not genre_id or not name:
                continue
            item = {"id": genre_id, "name": name[:80],
                    "kind": str(genre.get("kind") or "genre").strip().casefold()[:16]}
            genres.append(item)
            if item["kind"] == "theme":
                themes.append(item["name"])
    description = " ".join(_MARKUP.sub(" ", str(row.get("description") or "")).split())[:400]
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
        "themes": themes[:8],
        "description": description,
        "related": related,
        "related_loaded": bool(include_related),
    }


class ShikimoriCatalog:
    url = "https://shikimori.io/api/graphql"
    rest_url = "https://shikimori.io/api"
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

    async def _rest(self, path):
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                response = await client.get(self.rest_url + path, headers={
                    "Accept": "application/json", "User-Agent": self.user_agent})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise CatalogError("Shikimori is temporarily unavailable") from None
        if not isinstance(payload, list):
            raise CatalogError("Shikimori returned an unknown catalog response")
        return [row for row in payload if isinstance(row, dict)]

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

    async def candidates(self, genre_ids, searches=()):
        """Mix ranked hits with other years, plus mood searches when a vibe is set."""
        genres = []
        for value in genre_ids or ():
            text = str(value).strip()
            parts = [part for part in text.split(",") if part.isdigit() and int(part) > 0
                     and part != AWARD_WINNING_ID]
            text = ",".join(parts)
            if text and text not in genres:
                genres.append(text)
            if len(genres) == 6:
                break
        seasons = [None, "2000_2012", "2013_2019", "2020_2024"]
        queries = []
        for index, genre in enumerate(genres):
            queries.append({"genre": genre, "order": "ranked", "season": None, "limit": 10})
            queries.append({"genre": genre, "order": "popularity", "season": seasons[index % len(seasons)],
                            "limit": 6})
        if genres:
            queries.append({"genre": genres[0], "order": "aired_on", "season": "1990_2009", "limit": 8})
        terms = []
        for value in searches or ():
            text = " ".join(str(value or "").split())[:40]
            if text and text not in terms:
                terms.append(text)
            if len(terms) == 3:
                break
        query = """query RecommendationCandidates($genre: String!, $order: OrderEnum!, $season: SeasonString, $limit: PositiveInt) {
          animes(genre: $genre, limit: $limit, order: $order, season: $season,
                 kind: "tv,movie,ona,ova", status: "released", censored: true) {
            id malId name russian score kind franchise airedOn { year }
            description
            genres { id name russian kind }
            related { relationKind anime { id } }
          }
        }"""
        search_query = """query MoodCandidates($search: String!, $limit: PositiveInt) {
          animes(search: $search, limit: $limit, order: ranked,
                 kind: "tv,movie,ona,ova", status: "released", censored: true) {
            id malId name russian score kind franchise airedOn { year }
            description
            genres { id name russian kind }
            related { relationKind anime { id } }
          }
        }"""
        result, errors, planned = {}, 0, len(queries) + len(terms)
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
        for term in terms:
            if (queries or result or errors) and self.delay:
                await asyncio.sleep(self.delay)
            try:
                rows = await self._query(search_query, {"search": term, "limit": 15})
            except CatalogError:
                errors += 1
                continue
            for row in rows:
                item = normalize_anime(row, include_related=True)
                if item:
                    result.setdefault(item["id"], item)
        if planned and errors == planned:
            raise CatalogError("Shikimori is temporarily unavailable")
        return list(result.values())

    async def similar(self, anime_ids, *, per_title=8, limit=32):
        """Similar titles for the person's own top picks, then hydrated with genres."""
        seeds = []
        for value in anime_ids or ():
            text = str(value).strip()
            if text.isdigit() and int(text) > 0 and text not in seeds:
                seeds.append(text)
            if len(seeds) == 8:
                break
        found = {}
        failures = 0
        for index, anime_id in enumerate(seeds):
            if index and self.delay:
                await asyncio.sleep(self.delay)
            try:
                rows = await self._rest(f"/animes/{int(anime_id)}/similar")
            except CatalogError:
                failures += 1
                continue
            ranked = sorted(rows, key=lambda row: (-_rest_score(row), str(row.get("id") or "")))
            for row in ranked[:per_title]:
                item = _rest_anime(row)
                if item:
                    found.setdefault(item["id"], item)
            if len(found) >= limit:
                break
        if seeds and failures == len(seeds):
            return []
        chosen = list(found.values())[:limit]
        if not chosen:
            return []
        try:
            detailed = await self.metadata([item["id"] for item in chosen], related=True)
        except CatalogError:
            return chosen
        hydrated = []
        for item in chosen:
            hydrated.append({**item, **detailed.get(item["id"], {})})
        return hydrated


def _rest_score(row):
    try:
        return float(row.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0


def _rest_anime(row):
    identifier = str(row.get("id") or "").strip()
    if not identifier.isdigit() or int(identifier) <= 0:
        return None
    aired = str(row.get("aired_on") or "")
    year = int(aired[:4]) if aired[:4].isdigit() else None
    return {
        "id": identifier,
        "mal_id": None,
        "title": str(row.get("russian") or row.get("name") or "").strip()[:180],
        "score": _rest_score(row),
        "kind": str(row.get("kind") or "").strip().casefold()[:32] or None,
        "franchise": str(row.get("franchise") or "").strip()[:120] or None,
        "year": year,
        "genres": [],
        "themes": [],
        "description": "",
        "related": [],
        "related_loaded": False,
    }
