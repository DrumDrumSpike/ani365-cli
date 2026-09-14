"""Small Shikimori OAuth client; all credential handling stays server-side."""
import asyncio
import time
from urllib.parse import urlencode

import httpx


class ShikimoriError(Exception):
    """Fixed safe diagnostic; never includes a response or OAuth code."""


class Shikimori:
    oauth_base = "https://shikimori.io/oauth"
    api_base = "https://shikimori.io/api"
    anime_batch_size = 50
    # Keep well below both published per-second and per-minute request limits.
    anime_batch_delay = 0.7
    user_rate_statuses = {"planned", "watching", "rewatching", "completed", "on_hold", "dropped"}

    def __init__(self, client_id, client_secret, redirect_uri, app_name="ani365-mini-app"):
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.app_name = app_name

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def authorize_url(self, state):
        return self.oauth_base + "/authorize?" + urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": "user_rates", "state": state,
        })

    async def exchange_code(self, code):
        return await self._token({"grant_type": "authorization_code", "code": code})

    async def refresh(self, refresh_token):
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _token(self, values):
        values.update({"client_id": self.client_id, "client_secret": self.client_secret,
                       "redirect_uri": self.redirect_uri})
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(self.oauth_base + "/token", data=values,
                                             headers={"User-Agent": self.app_name, "Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ShikimoriError("Не удалось подключить Shikimori.") from None
        if not isinstance(payload, dict) or not payload.get("access_token") or not payload.get("refresh_token"):
            raise ShikimoriError("Shikimori вернул неполный OAuth-ответ.")
        return payload

    async def whoami(self, access_token):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.api_base + "/users/whoami", headers={
                    "User-Agent": self.app_name, "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ShikimoriError("Не удалось получить профиль Shikimori.") from None
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise ShikimoriError("Shikimori вернул неполный профиль.")
        return payload

    async def user_rates(self, access_token, user_id):
        """Read the v2 list endpoint; rate records contain IDs, not titles."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.api_base + "/v2/user_rates", params={
                    "user_id": user_id, "target_type": "Anime",
                }, headers={"User-Agent": self.app_name, "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ShikimoriError("Не удалось получить список Shikimori.") from None
        if not isinstance(payload, list):
            raise ShikimoriError("Shikimori вернул неизвестный формат списка.")
        return [row for row in payload if isinstance(row, dict)]

    async def animes(self, anime_ids):
        """Read public anime metadata in small, rate-limited ID batches.

        Shikimori's ``ids`` filter expects literal commas rather than encoded
        commas, so this URL is assembled only from validated integer IDs.
        """
        ids, seen = [], set()
        for value in anime_ids:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in seen:
                ids.append(value)
                seen.add(value)
        result = {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                for offset in range(0, len(ids), self.anime_batch_size):
                    batch = ids[offset:offset + self.anime_batch_size]
                    url = (self.api_base + "/animes?ids=" + ",".join(map(str, batch)) +
                           f"&limit={len(batch)}")
                    response = await client.get(url, headers={
                        "User-Agent": self.app_name, "Accept": "application/json"})
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise ValueError
                    for row in payload:
                        if isinstance(row, dict) and row.get("id") is not None:
                            result[str(row["id"])] = row
                    if offset + self.anime_batch_size < len(ids):
                        await asyncio.sleep(self.anime_batch_delay)
        except (httpx.HTTPError, ValueError):
            raise ShikimoriError("Не удалось получить названия аниме Shikimori.") from None
        return result

    async def anime(self, anime_id):
        """Fetch one public anime record to obtain its stable MAL bridge."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.api_base + f"/animes/{int(anime_id)}", headers={
                    "User-Agent": self.app_name, "Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            raise ShikimoriError("Не удалось получить данные аниме Shikimori.") from None
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise ShikimoriError("Shikimori вернул неполные данные аниме.")
        return payload

    async def update_user_rate(self, access_token, rate_id, *, episodes=None, status=None):
        """Update explicitly chosen user-rate fields without exposing tokens."""
        values = {}
        if episodes is not None:
            values["episodes"] = max(0, int(episodes))
        if status is not None:
            status = str(status)
            if status not in self.user_rate_statuses:
                raise ShikimoriError("Некорректный статус Shikimori.")
            values["status"] = status
        if not values:
            return
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.patch(self.api_base + f"/v2/user_rates/{int(rate_id)}",
                                              json={"user_rate": values}, headers={
                                                  "User-Agent": self.app_name,
                                                  "Authorization": f"Bearer {access_token}",
                                                  "Accept": "application/json"})
                response.raise_for_status()
        except (httpx.HTTPError, TypeError, ValueError):
            raise ShikimoriError("Не удалось обновить прогресс Shikimori.") from None

    @staticmethod
    def expires_at(payload):
        return time.time() + max(1, int(payload.get("expires_in", 0) or 0))
