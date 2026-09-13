"""Small Shikimori OAuth client; all credential handling stays server-side."""
import time
from urllib.parse import urlencode

import httpx


class ShikimoriError(Exception):
    """Fixed safe diagnostic; never includes a response or OAuth code."""


class Shikimori:
    oauth_base = "https://shikimori.io/oauth"
    api_base = "https://shikimori.io/api"

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
        """Read the documented v2 list endpoint without putting tokens in URLs."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(self.api_base + "/v2/user_rates", params={
                    "user_id": user_id, "target_type": "Anime", "limit": 500,
                }, headers={"User-Agent": self.app_name, "Authorization": f"Bearer {access_token}",
                            "Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise ShikimoriError("Не удалось получить список Shikimori.") from None
        if not isinstance(payload, list):
            raise ShikimoriError("Shikimori вернул неизвестный формат списка.")
        return [row for row in payload if isinstance(row, dict)]

    @staticmethod
    def expires_at(payload):
        return time.time() + max(1, int(payload.get("expires_in", 0) or 0))
