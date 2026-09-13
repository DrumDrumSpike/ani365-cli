"""HTTPS API and static Telegram Mini App.

The API deliberately has no user-id parameter.  Every private operation starts
with a Telegram WebApp signature or a short-lived, HttpOnly session established
from one.  Anime365 credentials and signed media URLs remain backend-only except
for the selected direct-playback URL returned to its authenticated owner.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .api import APIError, Anime365, number, title
from .downloads import DownloadManager
from .http import HTTPClient
from .media import MAX_SUBTITLE_FILE, MediaError, _run
from .matching import rank_anime365_candidates, shikimori_titles
from .shikimori import Shikimori, ShikimoriError
from .translations import group_translations, viewing_type


LOG = logging.getLogger(__name__)
INIT_DATA_MAX_AGE = 24 * 60 * 60
SESSION_MAX_AGE = 6 * 60 * 60
TICKET_TTL = 5 * 60
TRAVEL_BATCH_LIMIT = 25
SHIKIMORI_AUTO_LINK_BATCH = 50


class WebAuthError(ValueError):
    """A fixed authentication error that cannot echo Telegram initData."""


def validate_init_data(init_data, bot_token, *, now=None, max_age=INIT_DATA_MAX_AGE):
    """Validate Telegram's WebApp hash and return the authenticated user id.

    This follows Telegram's two-stage HMAC construction.  ``parse_qsl`` is used
    only after the entire raw payload is received; no caller-supplied user id is
    trusted and initData is never logged.
    """
    if not isinstance(init_data, str) or not init_data or len(init_data) > 8192:
        raise WebAuthError("Invalid Telegram Mini App authorization.")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise WebAuthError("Invalid Telegram Mini App authorization.") from None
    values = {}
    for key, value in pairs:
        if key in values:
            raise WebAuthError("Invalid Telegram Mini App authorization.")
        values[key] = value
    supplied_hash = values.pop("hash", "")
    if not supplied_hash or not all(char in "0123456789abcdefABCDEF" for char in supplied_hash):
        raise WebAuthError("Invalid Telegram Mini App authorization.")
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_hash, expected):
        raise WebAuthError("Invalid Telegram Mini App authorization.")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise WebAuthError("Invalid Telegram Mini App authorization.") from None
    moment = time.time() if now is None else float(now)
    if user_id <= 0 or auth_date > moment + 60 or moment - auth_date > max_age:
        raise WebAuthError("Telegram Mini App authorization expired.")
    return user_id


class SessionSigner:
    """Small signed cookie used for media elements, which cannot set headers."""

    def __init__(self, bot_token, clock=time.time):
        self._key = hmac.new(b"ani365-mini-app-session", bot_token.encode(), hashlib.sha256).digest()
        self.clock = clock

    def create(self, user_id):
        expires = int(self.clock() + SESSION_MAX_AGE)
        payload = f"{int(user_id)}.{expires}"
        signature = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def verify(self, value):
        try:
            user_id, expires, signature = str(value).split(".", 2)
            payload = f"{int(user_id)}.{int(expires)}"
        except (TypeError, ValueError):
            return None
        expected = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expires) < self.clock() or int(user_id) <= 0:
            return None
        return int(user_id)


@dataclass(frozen=True)
class StreamTicket:
    user_id: int
    url: str
    expires_at: float


class EphemeralTickets:
    """In-memory owner-bound tickets; signed Anime365 URLs never hit SQLite."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._items = {}

    def create(self, user_id, url, ttl=TICKET_TTL):
        self.cleanup()
        ticket = secrets.token_urlsafe(32)
        self._items[ticket] = StreamTicket(int(user_id), str(url), self.clock() + ttl)
        return ticket

    def get(self, ticket, user_id):
        self.cleanup()
        item = self._items.get(str(ticket))
        if item is None or item.user_id != int(user_id):
            return None
        return item

    def cleanup(self):
        moment = self.clock()
        for key, item in list(self._items.items()):
            if item.expires_at <= moment:
                self._items.pop(key, None)


class RateLimiter:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._buckets = {}

    def check(self, user_id, action, limit, window=60):
        key, now = (int(user_id), action), self.clock()
        values = [item for item in self._buckets.get(key, ()) if item > now - window]
        if len(values) >= limit:
            raise HTTPException(429, "Too many requests. Try again shortly.")
        values.append(now)
        self._buckets[key] = values


class AddLibraryRequest(BaseModel):
    series_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=500)
    year: str | None = Field(default=None, max_length=30)
    series_type: str | None = Field(default=None, max_length=80)


class NotificationRequest(BaseModel):
    enabled: bool
    mode: str = Field(default="any", pattern="^(any|subtitles|voice)$")


class PlayRequest(BaseModel):
    series_id: int = Field(gt=0)
    episode_id: int = Field(gt=0)
    translation_id: int = Field(gt=0)
    quality: int = Field(gt=0, le=10000)


class ProgressRequest(BaseModel):
    series_id: int = Field(gt=0)
    episode_id: int = Field(gt=0)
    position_seconds: float = Field(ge=0, le=86400)
    duration_seconds: float = Field(ge=0, le=86400)
    ended: bool = False


class DownloadRequest(BaseModel):
    series_id: int = Field(gt=0)
    episode_id: int = Field(gt=0)
    translation_id: int = Field(gt=0)
    quality: int = Field(gt=0, le=10000)
    delivery: str = Field(pattern="^(browser|telegram)$")


class TravelRequest(BaseModel):
    series_id: int = Field(gt=0)
    anchor_episode_id: int = Field(gt=0)
    translation_id: int = Field(gt=0)
    quality: int = Field(gt=0, le=10000)
    count: int = Field(default=3, ge=1, le=5)
    all_available: bool = False
    delivery: str = Field(pattern="^(browser|telegram)$")


class ShikimoriImportRequest(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: ["watching", "planned"], max_length=6)


class ShikimoriLinkRequest(BaseModel):
    series_id: int = Field(gt=0)
    # Required for non-MAL matches.  The UI only sends this after the person has
    # selected the exact Anime365 result from the displayed candidates.
    confirm_manual: bool = False


class ShikimoriSettingsRequest(BaseModel):
    sync_enabled: bool


def _api_error(exc):
    raise HTTPException(409 if exc.code in (401, 403) else 502, str(exc)) from None


def _episode_payload(episode, watched_id, watched_number):
    episode_id = int(episode["id"])
    watched = episode_id == watched_id or (
        watched_number is not None and number(episode.get("episodeInt") or episode.get("episodeFull"))
        <= number(watched_number))
    return {
        "id": episode_id,
        "number": str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
        "title": str(episode.get("episodeTitle") or ""),
        "type": str(episode.get("episodeType") or "tv"),
        "watched": watched,
    }


def _translation_profile(item):
    kind, language = viewing_type(item)
    author = str(item.get("authorsSummary") or item.get("title") or "").strip().casefold()
    return kind, language, author


def _preferred_translation(rows, profile):
    """Use the selected studio if present, then safely fall back to its type/language."""
    same_format = [row for row in rows if _translation_profile(row)[:2] == profile[:2]]
    if not same_format:
        return None
    exact = next((row for row in same_format if _translation_profile(row)[2] == profile[2]), None)
    return exact or same_format[0]


SHIKIMORI_STATUSES = {"planned", "watching", "rewatching", "completed", "on_hold", "dropped"}


def _shikimori_title(rate, anime=None):
    target = rate.get("target") if isinstance(rate.get("target"), dict) else {}
    anime = anime if isinstance(anime, dict) else {}
    return str(target.get("russian") or target.get("name") or target.get("title") or
               anime.get("russian") or anime.get("name") or anime.get("title")
               or rate.get("title") or f"Shikimori #{rate.get('target_id') or target.get('id') or '?'}")[:500]


def _shikimori_rates(rows, statuses, anime_by_id=None):
    """Keep only safe, normalized Anime rates from an untrusted API payload."""
    result, seen = [], set()
    for row in rows:
        if row.get("status") not in statuses or row.get("target_type", "Anime") != "Anime":
            continue
        target = row.get("target") if isinstance(row.get("target"), dict) else {}
        rate_id = row.get("id")
        anime_id = row.get("target_id") or target.get("id")
        if rate_id is None or anime_id is None:
            continue
        rate_id, anime_id = str(rate_id).strip(), str(anime_id).strip()
        if not rate_id or not anime_id or rate_id in seen:
            continue
        try:
            episodes = max(0, int(row.get("episodes") or 0))
        except (TypeError, ValueError):
            episodes = 0
        anime = (anime_by_id or {}).get(anime_id)
        result.append({"external_rate_id": rate_id, "external_anime_id": anime_id,
                       "status": str(row["status"]), "episodes": episodes,
                       "title": _shikimori_title(row, anime)})
        seen.add(rate_id)
    return result


async def _shikimori_rates_with_titles(shikimori_client, rows, statuses):
    """Hydrate only title-less user rates without doing one request per anime."""
    preliminary = _shikimori_rates(rows, statuses)
    missing = [item["external_anime_id"] for item in preliminary
               if item["title"].startswith("Shikimori #")]
    if not missing:
        return preliminary
    details = await shikimori_client.animes(missing)
    return _shikimori_rates(rows, statuses, details)


async def _shikimori_candidates(anime_client, shikimori_client, rate):
    """Search every stable Shikimori title and retain only safe candidate data."""
    external = await shikimori_client.anime(rate["external_anime_id"])
    rows = []
    for query in shikimori_titles(external, rate["title"]):
        rows.extend(await anime_client.search(query))
    return external, rank_anime365_candidates(
        external, rows, fallback_title=rate["title"], fallback_mal_id=rate["external_anime_id"])


def create_app(config=None, store=None, anime=None, *, proxy_transport=None):
    """Create an app with injectable dependencies for isolated integration tests."""
    if config is None:
        from .config import Config
        config = Config.from_env()
    owns_store = store is None
    if store is None:
        from .store import Store
        store = Store(config.data_dir)
    if anime is None:
        anime = Anime365(HTTPClient(), config.anime_url)

    app = FastAPI(title="ani365 Mini App", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.anime = anime
    app.state.tickets = EphemeralTickets()
    app.state.subtitle_tickets = EphemeralTickets()
    app.state.limiter = RateLimiter()
    app.state.sessions = SessionSigner(config.bot_token)
    app.state.config = config
    app.state.download_tickets = EphemeralTickets()
    app.state.downloads = DownloadManager(config, store, anime)
    app.state.proxy_transport = proxy_transport
    app.state.shikimori = Shikimori(config.shikimori_client_id, config.shikimori_client_secret,
                                    config.shikimori_redirect_uri)

    async def authenticated_user(request: Request,
                                 init_data: str | None = Header(default=None,
                                                                  alias="X-Telegram-Init-Data")):
        user_id = None
        if init_data:
            try:
                user_id = validate_init_data(init_data, config.bot_token)
            except WebAuthError as exc:
                raise HTTPException(401, str(exc)) from None
        else:
            user_id = app.state.sessions.verify(request.cookies.get("ani365_mini_session"))
            if user_id is None:
                raise HTTPException(401, "Telegram Mini App authorization is required.")
        if not store.is_allowed(user_id, config.owner_id):
            raise HTTPException(403, "Mini App access is not allowed.")
        return user_id

    def require_token(user_id):
        token = store.token(user_id)
        if not token:
            raise HTTPException(409, "Сначала подключите Anime365 через /auth в боте.")
        return token

    async def shikimori_account(user_id):
        account = store.external_account(user_id, "shikimori")
        if not account:
            raise HTTPException(409, "Сначала подключите Shikimori.")
        if account["expires_at"] > time.time() + 30:
            return account
        try:
            refreshed = await app.state.shikimori.refresh(account["refresh_token"])
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        store.save_external_account(user_id, "shikimori", refreshed["access_token"],
                                    refreshed["refresh_token"], app.state.shikimori.expires_at(refreshed),
                                    account["external_user_id"])
        return store.external_account(user_id, "shikimori")

    async def merge_shikimori_progress(user_id, series_id, rate):
        """Apply the documented import policy: never lower local progress."""
        remote = max(0, int(rate["episodes"]))
        local = store.get_watchlist(user_id, series_id)
        if not local or remote <= number(local.get("last_watched_episode_number")):
            return
        try:
            rows = await anime.episodes(series_id)
        except APIError:
            LOG.warning("Shikimori progress import skipped (user=%s series=%s)", user_id, series_id)
            return
        exact = [row for row in rows if number(row.get("episodeInt") or row.get("episodeFull")) == remote]
        if exact:
            episode = exact[-1]
            store.update_progress(user_id, series_id, episode)

    async def sync_shikimori_progress(user_id, series_id, episode_number):
        """Best-effort completion sync; playback is never blocked by Shikimori."""
        try:
            watched = number(episode_number)
            if watched <= 0 or watched != int(watched):
                return
            rate = store.external_rate_for_series(user_id, "shikimori", series_id)
            account = store.external_account(user_id, "shikimori")
            if not rate or not account or not account["sync_enabled"] or watched <= rate["episodes"]:
                return
            account = await shikimori_account(user_id)
            await app.state.shikimori.update_user_rate(account["access_token"], rate["external_rate_id"],
                                                       episodes=int(watched))
            store.update_external_rate_episodes(user_id, "shikimori", rate["external_rate_id"], int(watched))
        except (ShikimoriError, HTTPException, ValueError):
            LOG.warning("Shikimori progress sync failed (user=%s series=%s)", user_id, series_id)

    @app.on_event("shutdown")
    async def shutdown():
        await app.state.downloads.stop()
        if owns_store:
            store.close()

    @app.on_event("startup")
    async def startup():
        await app.state.downloads.start()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).with_name("web_static") / "index.html")

    @app.get("/assets/{asset_name}")
    async def assets(asset_name: str):
        allowed = {"app.js", "app.css"}
        if asset_name not in allowed:
            raise HTTPException(404, "Not found")
        return FileResponse(Path(__file__).with_name("web_static") / asset_name)

    @app.get("/api/me")
    async def me(response: Response, user_id=Depends(authenticated_user)):
        response.set_cookie("ani365_mini_session", app.state.sessions.create(user_id),
                            max_age=SESSION_MAX_AGE, httponly=True, secure=config.web_cookie_secure,
                            samesite="strict", path="/")
        return {"user_id": user_id, "anime365_connected": bool(store.token(user_id))}

    @app.get("/api/shikimori/status")
    async def shikimori_status(user_id=Depends(authenticated_user)):
        return {"configured": app.state.shikimori.configured,
                **store.external_account_status(user_id, "shikimori")}

    @app.patch("/api/shikimori/settings")
    async def shikimori_settings(payload: ShikimoriSettingsRequest,
                                 user_id=Depends(authenticated_user)):
        if not store.set_external_sync_enabled(user_id, "shikimori", payload.sync_enabled):
            raise HTTPException(409, "Сначала подключите Shikimori.")
        return {"sync_enabled": payload.sync_enabled}

    @app.post("/api/shikimori/connect")
    async def shikimori_connect(user_id=Depends(authenticated_user)):
        if not app.state.shikimori.configured:
            raise HTTPException(409, "Shikimori OAuth ещё не настроен на сервере.")
        state = secrets.token_urlsafe(32)
        store.create_oauth_state(user_id, "shikimori", state)
        return {"authorization_url": app.state.shikimori.authorize_url(state)}

    @app.get("/api/shikimori/callback")
    async def shikimori_callback(code: str = "", state: str = ""):
        # A callback is authenticated by its one-time, owner-bound state; it does
        # not accept a Telegram user id and does not log either state or code.
        user_id = store.consume_oauth_state("shikimori", state)
        if not user_id or not app.state.shikimori.configured or not code or len(code) > 4096:
            return HTMLResponse("<h1>Не удалось подтвердить Shikimori.</h1>", status_code=400)
        try:
            token = await app.state.shikimori.exchange_code(code)
            profile = await app.state.shikimori.whoami(token["access_token"])
            store.save_external_account(user_id, "shikimori", token["access_token"], token["refresh_token"],
                                        app.state.shikimori.expires_at(token), str(profile["id"]))
        except ShikimoriError:
            return HTMLResponse("<h1>Shikimori временно недоступен. Попробуйте ещё раз.</h1>", status_code=502)
        return HTMLResponse("<h1>Shikimori подключён.</h1><p>Вернитесь в Telegram Mini App.</p>")

    @app.delete("/api/shikimori")
    async def shikimori_disconnect(user_id=Depends(authenticated_user)):
        store.forget_external_account(user_id, "shikimori")
        return Response(status_code=204)

    @app.get("/api/shikimori/import/preview")
    async def shikimori_import_preview(statuses: list[str] = Query(default=["watching", "planned"]),
                                       user_id=Depends(authenticated_user)):
        selected = set(statuses)
        if not selected or not selected <= SHIKIMORI_STATUSES:
            raise HTTPException(422, "Некорректный статус Shikimori.")
        account = await shikimori_account(user_id)
        try:
            rates = await app.state.shikimori.user_rates(account["access_token"], account["external_user_id"])
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        try:
            items = await _shikimori_rates_with_titles(app.state.shikimori, rates, selected)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"items": items, "count": len(items), "policy": "progress=max(local, shikimori)"}

    @app.post("/api/shikimori/import")
    async def shikimori_import(payload: ShikimoriImportRequest, user_id=Depends(authenticated_user)):
        selected = set(payload.statuses)
        if not selected or not selected <= SHIKIMORI_STATUSES:
            raise HTTPException(422, "Некорректный статус Shikimori.")
        account = await shikimori_account(user_id)
        try:
            upstream = await app.state.shikimori.user_rates(account["access_token"], account["external_user_id"])
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        try:
            rates = await _shikimori_rates_with_titles(app.state.shikimori, upstream, selected)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        store.import_external_rates(user_id, "shikimori", rates)
        linked = []
        for rate in store.external_user_rates(user_id, "shikimori", linked=True):
            # A previous explicit mapping or an exact provider-ID mapping can
            # safely restore this title to the local playback library.
            if rate["external_rate_id"] not in {item["external_rate_id"] for item in rates}:
                continue
            series_id = rate["anime365_series_id"]
            store.add_watchlist(user_id, series_id, rate["title"])
            await merge_shikimori_progress(user_id, series_id, rate)
            linked.append(rate)
        unmatched = store.external_user_rates(user_id, "shikimori", linked=False)
        return {"imported": len(rates), "linked": len(linked), "unmatched": unmatched,
                "policy": "progress=max(local, shikimori); Shikimori status is primary"}

    @app.get("/api/shikimori/imports")
    async def shikimori_imports(linked: bool | None = None, user_id=Depends(authenticated_user)):
        return {"items": store.external_user_rates(user_id, "shikimori", linked=linked),
                "policy": "progress=max(local, shikimori); Shikimori status is primary"}

    @app.post("/api/shikimori/imports/auto-link")
    async def shikimori_auto_link(user_id=Depends(authenticated_user)):
        """Link a bounded batch only where Anime365 itself confirms the MAL ID."""
        app.state.limiter.check(user_id, "shikimori-auto-link", 1, window=60)
        rates = store.external_user_rates(user_id, "shikimori", linked=False,
                                          limit=SHIKIMORI_AUTO_LINK_BATCH)
        semaphore = asyncio.Semaphore(4)

        async def lookup(rate):
            async with semaphore:
                try:
                    rows = await anime.series_by_mal_id(rate["external_anime_id"])
                except APIError:
                    LOG.info("Shikimori MAL lookup unavailable (user=%s, rate=%s)",
                             user_id, rate["external_rate_id"])
                    return rate, None
            return rate, rows[0] if len(rows) == 1 else None

        matches = await asyncio.gather(*(lookup(rate) for rate in rates))
        linked = 0
        for rate, selected in matches:
            if selected is None:
                continue
            series_id = int(selected["id"])
            store.add_watchlist(user_id, series_id, title(selected), selected.get("year"),
                                selected.get("typeTitle") or selected.get("type"))
            store.save_external_id(series_id, "shikimori", rate["external_anime_id"])
            store.save_external_id(series_id, "mal", str(selected["myAnimeListId"]))
            bound = store.link_external_user_rate(user_id, "shikimori", rate["external_rate_id"], series_id)
            if bound:
                await merge_shikimori_progress(user_id, series_id, bound)
                linked += 1
        remaining = len(store.external_user_rates(user_id, "shikimori", linked=False))
        return {"checked": len(rates), "linked": linked, "remaining": remaining,
                "batch_limited": remaining > 0 and len(rates) == SHIKIMORI_AUTO_LINK_BATCH}

    @app.get("/api/shikimori/imports/{rate_id}/candidates")
    async def shikimori_candidates(rate_id: str, user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "shikimori-candidates", 30)
        rate = store.external_user_rate(user_id, "shikimori", rate_id)
        if not rate:
            raise HTTPException(404, "Импортированный тайтл не найден.")
        try:
            _external, matched = await _shikimori_candidates(anime, app.state.shikimori, rate)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        except APIError as exc:
            _api_error(exc)
        candidates = [{"series_id": item["series_id"], "title": title(item["row"]),
                       "year": item["row"].get("year"),
                       "type": item["row"].get("typeTitle") or item["row"].get("type"),
                       "verified_mal": item["verified_mal"], "match_reason": item["match_reason"]}
                      for item in matched[:20]]
        return {"rate": rate, "candidates": candidates,
                "verified_mal_available": any(item["verified_mal"] for item in candidates)}

    @app.post("/api/shikimori/imports/{rate_id}/link")
    async def shikimori_link(rate_id: str, payload: ShikimoriLinkRequest,
                             user_id=Depends(authenticated_user)):
        rate = store.external_user_rate(user_id, "shikimori", rate_id)
        if not rate:
            raise HTTPException(404, "Импортированный тайтл не найден.")
        try:
            _external, candidates = await _shikimori_candidates(anime, app.state.shikimori, rate)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        except APIError as exc:
            _api_error(exc)
        candidate = next((item for item in candidates if item["series_id"] == payload.series_id), None)
        if candidate is None:
            raise HTTPException(422, "Выбранный Anime365 тайтл отсутствует среди кандидатов.")
        selected, verified = candidate["row"], candidate["verified_mal"]
        if not verified and not payload.confirm_manual:
            raise HTTPException(409, "Подтвердите ручную привязку: MAL ID не совпал.")
        series_id = int(selected["id"])
        store.add_watchlist(user_id, series_id, title(selected), selected.get("year"),
                            selected.get("typeTitle") or selected.get("type"))
        store.save_external_id(series_id, "shikimori", rate["external_anime_id"])
        if verified:
            store.save_external_id(series_id, "mal", str(selected["myAnimeListId"]))
        linked = store.link_external_user_rate(user_id, "shikimori", rate_id, series_id)
        await merge_shikimori_progress(user_id, series_id, linked)
        return {"item": linked, "verified_mal": verified}

    @app.get("/api/library")
    async def library(user_id=Depends(authenticated_user)):
        return {"items": store.list_watchlist(user_id), "continue": store.recent_playback(user_id)}

    @app.post("/api/library")
    async def add_library(payload: AddLibraryRequest, user_id=Depends(authenticated_user)):
        return store.add_watchlist(user_id, payload.series_id, payload.title, payload.year,
                                   payload.series_type)

    @app.delete("/api/library/{series_id}")
    async def delete_library(series_id: int, user_id=Depends(authenticated_user)):
        if not store.remove_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        return Response(status_code=204)

    @app.patch("/api/library/{series_id}/notifications")
    async def update_library_notifications(series_id: int, payload: NotificationRequest,
                                           user_id=Depends(authenticated_user)):
        watch = store.get_watchlist(user_id, series_id)
        if watch is None:
            raise HTTPException(404, "Anime is not in your library.")
        rows = ()
        if payload.enabled:
            try:
                rows = await anime.episodes(series_id)
            except APIError as exc:
                _api_error(exc)
        item = store.configure_notifications(user_id, series_id, payload.enabled, payload.mode, rows)
        if item is None:
            raise HTTPException(404, "Anime is not in your library.")
        return item

    @app.get("/api/catalog")
    async def catalog(query: str, user_id=Depends(authenticated_user)):
        if not query.strip() or len(query) > 200:
            raise HTTPException(422, "Введите название до 200 символов.")
        try:
            rows = await anime.search(query.strip())
        except APIError as exc:
            _api_error(exc)
        return {"items": rows}

    @app.get("/api/library/{series_id}")
    async def library_item(series_id: int, user_id=Depends(authenticated_user)):
        item = store.get_watchlist(user_id, series_id)
        if item is None:
            raise HTTPException(404, "Anime is not in your library.")
        try:
            episodes = await anime.episodes(series_id)
        except APIError as exc:
            _api_error(exc)
        progress = store.playback_progress(user_id, series_id)
        watched_id, watched_number = item.get("last_watched_episode_id"), item.get("last_watched_episode_number")
        return {"item": item, "playback": progress,
                "episodes": [_episode_payload(row, watched_id, watched_number) for row in episodes]}

    @app.get("/api/series/{series_id}/episodes")
    async def episodes(series_id: int, user_id=Depends(authenticated_user)):
        if not store.has_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            rows = await anime.episodes(series_id)
        except APIError as exc:
            _api_error(exc)
        item = store.get_watchlist(user_id, series_id)
        return {"items": [_episode_payload(row, item.get("last_watched_episode_id"),
                                             item.get("last_watched_episode_number")) for row in rows]}

    @app.get("/api/episodes/{episode_id}/translations")
    async def translations(episode_id: int, user_id=Depends(authenticated_user)):
        try:
            rows = await anime.translations(episode_id)
        except APIError as exc:
            _api_error(exc)
        return {"groups": [{"kind": group.kind, "language": group.language, "label": group.label,
                             "items": group.translations} for group in group_translations(rows)]}

    @app.get("/api/translations/{translation_id}/qualities")
    async def available_qualities(translation_id: int, user_id=Depends(authenticated_user)):
        try:
            values = await anime.available_qualities(translation_id, require_token(user_id))
        except APIError as exc:
            _api_error(exc)
        return {"items": values}

    @app.post("/api/play")
    async def play(payload: PlayRequest, user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "play", 30)
        if not store.has_watchlist(user_id, payload.series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            episodes = await anime.episodes(payload.series_id)
            episode = next((row for row in episodes if int(row.get("id", 0)) == payload.episode_id), None)
            if episode is None:
                raise HTTPException(422, "Выбранная серия больше недоступна.")
            translations = await anime.translations(payload.episode_id)
            if not any(int(row.get("id", 0)) == payload.translation_id for row in translations):
                raise HTTPException(422, "Выбранный перевод больше недоступен.")
            source = await anime.media_source(payload.translation_id, payload.quality, require_token(user_id))
        except APIError as exc:
            _api_error(exc)
        proxy_ticket = app.state.tickets.create(user_id, source.urls[0])
        subtitle_url = None
        if source.subtitle_url:
            # Subtitle CDNs do not consistently allow cross-origin <track>
            # requests from Telegram WebView. Keep the signed URL server-side;
            # a short-lived owner-bound ticket returns WebVTT from this origin.
            subtitle_ticket = app.state.subtitle_tickets.create(user_id, source.subtitle_url)
            subtitle_url = f"/api/subtitles/{subtitle_ticket}"
        # Direct playback is first choice. The private ticket is only used after
        # a WebView/CDN incompatibility, never for normal video traffic.
        return {"media_url": source.urls[0], "subtitle_url": subtitle_url,
                "proxy_url": f"/api/stream/{proxy_ticket}", "episode_number": str(
                    episode.get("episodeFull") or episode.get("episodeInt") or "?")}

    @app.post("/api/progress")
    async def progress(payload: ProgressRequest, user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "progress", 180)
        if not store.has_watchlist(user_id, payload.series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            rows = await anime.episodes(payload.series_id)
        except APIError as exc:
            _api_error(exc)
        episode = next((row for row in rows if int(row.get("id", 0)) == payload.episode_id), None)
        if episode is None:
            raise HTTPException(422, "Выбранная серия больше недоступна.")
        result = store.record_playback_progress(
            user_id, payload.series_id, payload.episode_id, payload.position_seconds,
            payload.duration_seconds, str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
            ended=payload.ended, completion_threshold=config.playback_completion_threshold)
        if result and result["completed"]:
            # Keep local playback durable even when Shikimori is temporarily
            # unavailable. The task catches all expected remote failures.
            asyncio.create_task(sync_shikimori_progress(
                user_id, payload.series_id,
                str(episode.get("episodeFull") or episode.get("episodeInt") or "?")))
        return result

    @app.post("/api/downloads")
    async def create_download(payload: DownloadRequest, user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "download", 12)
        if not store.has_watchlist(user_id, payload.series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            episodes = await anime.episodes(payload.series_id)
            episode = next((row for row in episodes if int(row.get("id", 0)) == payload.episode_id), None)
            translations = await anime.translations(payload.episode_id) if episode else ()
        except APIError as exc:
            _api_error(exc)
        if episode is None or not any(int(row.get("id", 0)) == payload.translation_id for row in translations):
            raise HTTPException(422, "Серия или перевод больше недоступны.")
        job_id = secrets.token_urlsafe(24)
        job = store.create_download_job(
            job_id, user_id, payload.series_id, payload.episode_id,
            str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
            payload.translation_id, payload.quality, payload.delivery)
        if job is None:
            raise HTTPException(404, "Anime is not in your library.")
        app.state.downloads.enqueue(job_id)
        return job

    @app.post("/api/travel")
    async def travel(payload: TravelRequest, user_id=Depends(authenticated_user)):
        """Queue a small, explicit batch using an existing translation preference.

        Translation IDs are episode-specific.  The selected anchor therefore
        supplies a kind/language/studio preference, which is resolved afresh for
        every following episode rather than incorrectly reusing its ID.
        """
        app.state.limiter.check(user_id, "travel", 4)
        watch = store.get_watchlist(user_id, payload.series_id)
        if watch is None:
            raise HTTPException(404, "Anime is not in your library.")
        try:
            episodes = await anime.episodes(payload.series_id)
            anchor = next((row for row in episodes if int(row.get("id", 0)) == payload.anchor_episode_id), None)
            anchor_rows = await anime.translations(payload.anchor_episode_id) if anchor else ()
        except APIError as exc:
            _api_error(exc)
        selected = next((row for row in anchor_rows if int(row.get("id", 0)) == payload.translation_id), None)
        if anchor is None or selected is None:
            raise HTTPException(422, "Серия или перевод больше недоступны.")
        profile = _translation_profile(selected)
        anchor_index = next(index for index, row in enumerate(episodes)
                            if int(row.get("id", 0)) == payload.anchor_episode_id)
        # “Next” is defined by the episode the person just selected, rather
        # than by the 90%-completion marker. A partially watched episode must
        # therefore still lead to its following episode, never back to #1.
        unseen = [row for row in episodes[anchor_index + 1:] if not _episode_payload(
            row, watch.get("last_watched_episode_id"), watch.get("last_watched_episode_number"))["watched"]]
        requested = len(unseen) if payload.all_available else payload.count
        chosen = unseen[:min(requested, TRAVEL_BATCH_LIMIT)]
        jobs, skipped = [], []
        for episode in chosen:
            try:
                rows = anchor_rows if int(episode["id"]) == payload.anchor_episode_id \
                    else await anime.translations(int(episode["id"]))
            except APIError:
                skipped.append(str(episode.get("episodeFull") or episode.get("episodeInt") or "?"))
                continue
            translation = _preferred_translation(rows, profile)
            if translation is None:
                skipped.append(str(episode.get("episodeFull") or episode.get("episodeInt") or "?"))
                continue
            job_id = secrets.token_urlsafe(24)
            job = store.create_download_job(
                job_id, user_id, payload.series_id, int(episode["id"]),
                str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
                int(translation["id"]), payload.quality, payload.delivery)
            if job:
                app.state.downloads.enqueue(job_id)
                jobs.append(job)
        return {"available": len(unseen), "requested": requested, "queued": len(jobs), "jobs": jobs,
                "skipped_episodes": skipped, "batch_limited": requested > TRAVEL_BATCH_LIMIT,
                "message": "Фактический размер станет известен после загрузки."}

    @app.get("/api/downloads")
    async def downloads(user_id=Depends(authenticated_user)):
        return {"items": store.list_download_jobs(user_id)}

    @app.post("/api/downloads/clear")
    async def clear_finished_downloads(user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "download-clear", 6)
        return {"hidden": store.hide_finished_download_jobs(user_id)}

    @app.get("/api/downloads/{job_id}")
    async def download_status(job_id: str, user_id=Depends(authenticated_user)):
        job = store.download_job(user_id, job_id)
        if job is None:
            raise HTTPException(404, "Download is not available.")
        return job

    @app.delete("/api/downloads/{job_id}")
    async def cancel_download(job_id: str, user_id=Depends(authenticated_user)):
        if not await app.state.downloads.cancel(user_id, job_id):
            raise HTTPException(409, "Download cannot be cancelled.")
        return Response(status_code=204)

    @app.post("/api/downloads/{job_id}/ticket")
    async def download_ticket(job_id: str, user_id=Depends(authenticated_user)):
        job = store.download_job(user_id, job_id)
        if not job or job["status"] != "ready" or not job["filename"]:
            raise HTTPException(409, "Файл ещё не готов или уже удалён.")
        path = app.state.downloads._ready_dir(job_id) / Path(job["filename"]).name
        if not path.is_file():
            raise HTTPException(410, "Файл больше недоступен.")
        ticket = app.state.download_tickets.create(user_id, str(path))
        return {"url": f"/api/download/{ticket}", "expires_in": TICKET_TTL}

    @app.get("/api/download/{ticket}")
    async def download(ticket: str, user_id=Depends(authenticated_user)):
        item = app.state.download_tickets.get(ticket, user_id)
        if item is None:
            raise HTTPException(404, "Download link is unavailable or expired.")
        path = Path(item.url)
        root = Path(config.media_dir).resolve()
        try:
            path.resolve().relative_to(root)
        except ValueError:
            raise HTTPException(404, "Download is unavailable.") from None
        if not path.is_file():
            raise HTTPException(410, "Файл больше недоступен.")
        return FileResponse(path, filename=path.name, media_type="video/x-matroska")

    @app.get("/api/stream/{ticket}")
    async def stream(ticket: str, request: Request, user_id=Depends(authenticated_user)):
        item = app.state.tickets.get(ticket, user_id)
        if item is None:
            raise HTTPException(404, "Streaming link is unavailable or expired.")
        headers = {"User-Agent": "ani365-mini-app/0.1"}
        if request.headers.get("range"):
            headers["Range"] = request.headers["range"]
        client = httpx.AsyncClient(transport=app.state.proxy_transport, follow_redirects=False,
                                   timeout=httpx.Timeout(30, read=120))
        try:
            upstream = await client.send(client.build_request("GET", item.url, headers=headers), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, "Не удалось получить поток Anime365.") from None
        if upstream.status_code not in (200, 206):
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, "Поток Anime365 временно недоступен.")

        async def body():
            try:
                async for chunk in upstream.aiter_bytes(128 * 1024):
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        copied = {name: value for name, value in upstream.headers.items()
                  if name.lower() in {"content-type", "content-length", "content-range", "accept-ranges"}}
        copied.setdefault("accept-ranges", "bytes")
        return StreamingResponse(body(), status_code=upstream.status_code, headers=copied)

    @app.get("/api/subtitles/{ticket}")
    async def subtitles(ticket: str, user_id=Depends(authenticated_user)):
        """Return a small subtitle file as same-origin WebVTT for <track>.

        This intentionally buffers only bounded subtitle text (never video) in
        a temporary directory.  ASS/SSA styling needs a libass renderer to be
        fully preserved; WebVTT is the reliable fallback in Telegram WebView.
        """
        item = app.state.subtitle_tickets.get(ticket, user_id)
        if item is None:
            raise HTTPException(404, "Subtitle link is unavailable or expired.")
        try:
            with tempfile.TemporaryDirectory(prefix="subtitle-", dir=config.media_dir) as name:
                directory = Path(name)
                subtitle = await asyncio.to_thread(app.state.downloads.media._download_subtitle,
                                                    item.url, directory)
                output = subtitle if subtitle.suffix.lower() == ".vtt" else directory / "subtitles.vtt"
                if output != subtitle:
                    await _run("ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(subtitle),
                               "-c:s", "webvtt", str(output))
                if not output.is_file() or output.stat().st_size == 0:
                    raise MediaError("Не удалось подготовить субтитры для плеера.")
                if output.stat().st_size > MAX_SUBTITLE_FILE:
                    raise MediaError("Файл субтитров оказался слишком большим.")
                body = await asyncio.to_thread(output.read_bytes)
        except MediaError as exc:
            raise HTTPException(502, str(exc)) from None
        except OSError:
            LOG.warning("Subtitle preparation failed (storage error)")
            raise HTTPException(502, "Не удалось подготовить субтитры для плеера.") from None
        return Response(content=body, media_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-store"})

    return app
