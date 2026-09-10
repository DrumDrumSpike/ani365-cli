"""HTTPS API and static Telegram Mini App.

The API deliberately has no user-id parameter.  Every private operation starts
with a Telegram WebApp signature or a short-lived, HttpOnly session established
from one.  Anime365 credentials and signed media URLs remain backend-only except
for the selected direct-playback URL returned to its authenticated owner.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .api import APIError, Anime365, number
from .downloads import DownloadManager
from .http import HTTPClient
from .translations import group_translations


LOG = logging.getLogger(__name__)
INIT_DATA_MAX_AGE = 24 * 60 * 60
SESSION_MAX_AGE = 6 * 60 * 60
TICKET_TTL = 5 * 60


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
    app.state.limiter = RateLimiter()
    app.state.sessions = SessionSigner(config.bot_token)
    app.state.config = config
    app.state.download_tickets = EphemeralTickets()
    app.state.downloads = DownloadManager(config, store, anime)
    app.state.proxy_transport = proxy_transport

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
        # Direct playback is first choice. The private ticket is only used after
        # a WebView/CDN incompatibility, never for normal video traffic.
        return {"media_url": source.urls[0], "subtitle_url": source.subtitle_url,
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
        return store.record_playback_progress(
            user_id, payload.series_id, payload.episode_id, payload.position_seconds,
            payload.duration_seconds, str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
            ended=payload.ended, completion_threshold=config.playback_completion_threshold)

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

    @app.get("/api/downloads")
    async def downloads(user_id=Depends(authenticated_user)):
        return {"items": store.list_download_jobs(user_id)}

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

    return app
