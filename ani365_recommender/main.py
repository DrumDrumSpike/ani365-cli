"""Run the bounded weekly recommendation refresh against the internal web API."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

from .scoring import genre_taste, rank, top_genres
from .shikimori import ShikimoriPublic, ShikimoriPublicError


LOG = logging.getLogger(__name__)
MAX_SHARED_GENRES = 8


@dataclass(frozen=True)
class Config:
    main_api_url: str
    shared_secret: str

    @classmethod
    def from_env(cls):
        base = os.environ.get("MAIN_API_URL", "http://web:8000").rstrip("/")
        secret = os.environ.get("RECOMMENDER_SHARED_SECRET", "").strip()
        if not base.startswith("http://") and not base.startswith("https://"):
            raise ValueError("MAIN_API_URL must be HTTP(S)")
        if len(secret) < 32 or any(char.isspace() for char in secret):
            raise ValueError("RECOMMENDER_SHARED_SECRET must contain at least 32 non-space characters")
        return cls(base, secret)


class MainAPI:
    def __init__(self, config, *, transport=None):
        self.config, self.transport = config, transport

    async def _request(self, method, path, *, json_body=None):
        try:
            async with httpx.AsyncClient(base_url=self.config.main_api_url, timeout=60,
                                         transport=self.transport) as client:
                response = await client.request(method, path, json=json_body, headers={
                    "X-Recommender-Token": self.config.shared_secret,
                    "Accept": "application/json",
                })
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("The main ani365 API is temporarily unavailable") from None

    async def profiles(self):
        return (await self._request("GET", "/internal/recommendations/profiles")).get("profiles", [])

    async def resolve(self, candidates):
        result = []
        for offset in range(0, len(candidates), 100):
            values = [{"shikimori_anime_id": row["id"], "mal_id": row.get("mal_id")}
                      for row in candidates[offset:offset + 100]]
            if values:
                result.extend((await self._request("POST", "/internal/recommendations/resolve",
                                                   json_body={"items": values})).get("items", []))
        return result

    async def save(self, user_id, items):
        return await self._request("PUT", f"/internal/recommendations/{int(user_id)}",
                                   json_body={"items": items})


async def refresh(config=None, *, main_api=None, shikimori=None):
    config = config or Config.from_env()
    main_api = main_api or MainAPI(config)
    shikimori = shikimori or ShikimoriPublic()
    profiles = [profile for profile in await main_api.profiles() if isinstance(profile, dict)]
    rated_ids = [rate.get("external_anime_id") for profile in profiles
                 for rate in profile.get("rates", ()) if (
                     rate.get("status") == "dropped" or (
                         rate.get("status") == "completed" and isinstance(rate.get("score"), int)
                         and 1 <= rate["score"] <= 10))]
    anime_by_id = await shikimori.metadata(rated_ids)
    wanted_genres = []
    for profile in profiles:
        wanted_genres.extend(top_genres(genre_taste(profile, anime_by_id)))
    # The strongest shared genres make one compact weekly pool for this small,
    # private installation.  It keeps both Shikimori and Anime365 requests bounded.
    selected_genres = [item[0] for item in sorted(
        ((identifier, sum(genre_taste(profile, anime_by_id).get(identifier, {}).get("weight", 0)
                          for profile in profiles)) for identifier in set(wanted_genres)),
        key=lambda item: (-item[1], item[0])) if item[1] > 0][:MAX_SHARED_GENRES]
    candidates = await shikimori.candidates(selected_genres)
    resolved = await main_api.resolve(candidates)
    saved = 0
    for profile in profiles:
        items = rank(profile, anime_by_id, candidates, resolved)
        await main_api.save(profile["user_id"], items)
        saved += len(items)
    return {"profiles": len(profiles), "candidates": len(candidates), "saved": saved}


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
    try:
        result = asyncio.run(refresh())
    except (RuntimeError, ShikimoriPublicError, ValueError) as exc:
        LOG.error("Recommendation refresh deferred: %s", exc)
        raise SystemExit(1) from None
    LOG.info("Recommendation refresh complete: profiles=%s candidates=%s saved=%s",
             result["profiles"], result["candidates"], result["saved"])


if __name__ == "__main__":
    main()
