"""Bounded public Shikimori GraphQL reads used by the weekly worker."""
from __future__ import annotations

import asyncio

import httpx


class ShikimoriPublicError(RuntimeError):
    """Credential-free error suitable for a scheduled task log."""


class ShikimoriPublic:
    url = "https://shikimori.io/api/graphql"
    batch_size = 50
    request_delay = 0.25

    def __init__(self, *, transport=None, user_agent="ani365-recommender"):
        self.transport = transport
        self.user_agent = user_agent

    async def _query(self, query, variables):
        try:
            async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
                response = await client.post(self.url, json={"query": query, "variables": variables},
                                             headers={"Accept": "application/json",
                                                      "User-Agent": self.user_agent})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ShikimoriPublicError("Shikimori is temporarily unavailable") from None
        rows = payload.get("data", {}).get("animes") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ShikimoriPublicError("Shikimori returned an unknown recommendation format")
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _normalize(row):
        identifier = str(row.get("id") or "").strip()
        if not identifier.isdigit() or int(identifier) <= 0:
            return None
        aired = row.get("airedOn") if isinstance(row.get("airedOn"), dict) else {}
        genres = []
        for genre in row.get("genres", ()):
            if isinstance(genre, dict) and str(genre.get("id") or "").strip() and str(genre.get("name") or "").strip():
                genres.append({"id": str(genre["id"]), "name": str(genre["name"])[:80]})
        franchise = str(row.get("franchise") or "").strip()
        return {"id": identifier, "mal_id": str(row.get("malId") or "").strip() or None,
                "title": str(row.get("russian") or row.get("name") or "").strip()[:500],
                "score": row.get("score"), "kind": str(row.get("kind") or "").strip()[:32] or None,
                "franchise": franchise[:120] or None, "year": aired.get("year"), "genres": genres}

    async def metadata(self, anime_ids):
        identifiers = list(dict.fromkeys(str(value) for value in anime_ids
                                         if str(value).isdigit() and int(value) > 0))
        result = {}
        query = """query RecommendationMetadata($ids: String!) {
          animes(ids: $ids) { id malId name russian score kind franchise airedOn { year } genres { id name } }
        }"""
        for offset in range(0, len(identifiers), self.batch_size):
            if offset:
                await asyncio.sleep(self.request_delay)
            rows = await self._query(query, {"ids": ",".join(identifiers[offset:offset + self.batch_size])})
            for row in rows:
                item = self._normalize(row)
                if item:
                    result[item["id"]] = item
        return result

    async def candidates(self, genre_ids, *, per_genre=20):
        query = """query RecommendationCandidates($genre: String!) {
          animes(genre: $genre, limit: 20, order: ranked) {
            id malId name russian score kind franchise airedOn { year } genres { id name }
          }
        }"""
        result = {}
        for offset, identifier in enumerate(dict.fromkeys(str(value) for value in genre_ids)):
            if not identifier.isdigit() or int(identifier) <= 0:
                continue
            if offset:
                await asyncio.sleep(self.request_delay)
            rows = await self._query(query, {"genre": identifier})
            for row in rows[:max(1, min(50, int(per_genre)))]:
                item = self._normalize(row)
                if item:
                    result[item["id"]] = item
        return list(result.values())
