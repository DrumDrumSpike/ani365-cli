"""HTTPS API and static Telegram Mini App.

The API deliberately has no user-id parameter.  Every private operation starts
with a Telegram WebApp signature or a short-lived, HttpOnly session established
from one.  Anime365 credentials and signed media URLs remain backend-only except
for the selected direct-playback URL returned to its authenticated owner.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from .api import APIError, Anime365, number, title
from .downloads import DownloadManager
from .http import HTTPClient
from .mal import MyAnimeList, MyAnimeListError
from .media import MAX_SUBTITLE_FILE, MediaError, _run
from .matching import rank_anime365_candidates, shikimori_titles
from .shikimori import Shikimori, ShikimoriError
from .translations import group_translations, viewing_type


LOG = logging.getLogger(__name__)
INIT_DATA_MAX_AGE = 24 * 60 * 60
SESSION_MAX_AGE = 6 * 60 * 60
TICKET_TTL = 5 * 60
HLS_TICKET_TTL = 60 * 60
HLS_PLAYLIST_MAX_BYTES = 2 * 1024 * 1024
HLS_PLAYLIST_MAX_URIS = 3000
MAX_POSTER_BYTES = 5 * 1024 * 1024
TRAVEL_BATCH_LIMIT = 25
BATCH_DOWNLOAD_LIMIT = 50
SHIKIMORI_AUTO_LINK_BATCH = 50
SHIKIMORI_IMPORT_PAGE_SIZE = 50
SHIKIMORI_IMPORT_FOREGROUND_METADATA_BATCH = 50
SHIKIMORI_BACKGROUND_STATUSES = ("watching", "planned", "completed")
SHIKIMORI_BACKGROUND_RETRY_SECONDS = 15 * 60
SHIKIMORI_BACKGROUND_POLL_SECONDS = 60
SHIKIMORI_ANIME_SLUG = re.compile(r"(?:^|/)([1-9][0-9]{0,8})-[a-z0-9-]+/?$", re.IGNORECASE)
HLS_URI_ATTRIBUTE = re.compile(r"(?P<prefix>\bURI=)(?P<quote>[\"'])(?P<value>.*?)(?P=quote)")
# SQLite and the legacy bot identify a title by one positive integer.  Keep the
# optional Hentai365 catalogue in a disjoint range without rewriting existing
# user data or allowing same-numbered titles from two services to collide.
HENTAI_SERIES_OFFSET = 1_000_000_000_000


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


def _is_hls(url, content_type):
    """Detect a playlist without relying solely on the CDN's MIME type."""
    media_type = str(content_type or "").split(";", 1)[0].strip().casefold()
    return media_type in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"} \
        or urlsplit(str(url)).path.casefold().endswith(".m3u8")


def _hls_target(base_url, value):
    """Resolve an upstream playlist reference; the browser never supplies it."""
    target = urljoin(base_url, str(value).strip())
    parts = urlsplit(target)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
            or parts.hostname.casefold() == "localhost":
        return None
    try:
        if not ipaddress.ip_address(parts.hostname).is_global:
            return None
    except ValueError:
        pass
    return target


def _rewrite_hls_playlist(payload, base_url, user_id, tickets):
    """Replace HLS child URLs with ephemeral, owner-bound proxy tickets."""
    lines, count = [], 0

    def ticket_url(value):
        nonlocal count
        target = _hls_target(base_url, value)
        if target is None:
            raise ValueError("unsafe HLS URL")
        count += 1
        if count > HLS_PLAYLIST_MAX_URIS:
            raise ValueError("too many HLS URLs")
        return "/api/stream/" + tickets.create(user_id, target, ttl=HLS_TICKET_TTL)

    for line in payload.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            ending = line[len(line.rstrip("\r\n")):]
            lines.append(ticket_url(stripped) + ending)
            continue
        if "URI=" in line:
            lines.append(HLS_URI_ATTRIBUTE.sub(
                lambda match: match.group("prefix") + match.group("quote")
                + ticket_url(match.group("value")) + match.group("quote"), line))
            continue
        lines.append(line)
    return "".join(lines)


async def _read_hls_playlist(upstream):
    """Buffer only a bounded text playlist; video remains stream-through."""
    chunks, total = [], 0
    async for chunk in upstream.aiter_bytes(64 * 1024):
        total += len(chunk)
        if total > HLS_PLAYLIST_MAX_BYTES:
            raise ValueError("HLS playlist is too large")
        chunks.append(chunk)
    try:
        return b"".join(chunks).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid HLS playlist") from exc


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
    # Anime365 returns a numeric year while old bot callers may send text.
    year: str | int | None = None
    series_type: str | None = Field(default=None, max_length=80)
    provider: str = Field(default="anime365", pattern="^(anime365|hentai365)$")


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


class BatchDownloadItem(BaseModel):
    """One deliberately chosen episode and its episode-specific media choice."""

    episode_id: int = Field(gt=0)
    translation_id: int = Field(gt=0)
    quality: int = Field(gt=0, le=10000)


class BatchDownloadRequest(BaseModel):
    series_id: int = Field(gt=0)
    items: list[BatchDownloadItem] = Field(min_length=1, max_length=BATCH_DOWNLOAD_LIMIT)
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
    statuses: list[str] = Field(default_factory=lambda: ["watching", "planned", "completed"], max_length=6)


class ShikimoriLinkRequest(BaseModel):
    series_id: int = Field(gt=0)
    # Required for non-MAL matches.  The UI only sends this after the person has
    # selected the exact Anime365 result from the displayed candidates.
    confirm_manual: bool = False


class ShikimoriLibraryLinkRequest(BaseModel):
    external_rate_id: str = Field(min_length=1, max_length=100)


class ShikimoriSettingsRequest(BaseModel):
    sync_enabled: bool | None = None
    auto_complete: bool | None = None


class ShikimoriStatusRequest(BaseModel):
    status: str = Field(pattern="^(planned|watching|rewatching|completed|on_hold|dropped)$")


class RecommendationCandidateRequest(BaseModel):
    shikimori_anime_id: str = Field(min_length=1, max_length=32, pattern="^[0-9]+$")
    mal_id: str | None = Field(default=None, max_length=32, pattern="^[0-9]+$")


class RecommendationCandidateResolveRequest(BaseModel):
    items: list[RecommendationCandidateRequest] = Field(min_length=1, max_length=100)


class RecommendationItemRequest(BaseModel):
    anime365_series_id: int = Field(gt=0)
    shikimori_anime_id: str = Field(min_length=1, max_length=32, pattern="^[0-9]+$")
    score: float = Field(ge=-100000, le=100000)
    title: str = Field(min_length=1, max_length=500)
    year: str | int | None = None
    series_type: str | None = Field(default=None, max_length=64)
    poster_url: str | None = Field(default=None, max_length=1000)
    reason: str = Field(min_length=1, max_length=300)


class RecommendationReplaceRequest(BaseModel):
    items: list[RecommendationItemRequest] = Field(max_length=50)


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
SHIKIMORI_METADATA_BACKFILL_BATCH = 50
SHIKIMORI_METADATA_RETRY_SECONDS = 300
SHIKIMORI_METADATA_REFRESH_SECONDS = 3600
MAL_METADATA_BACKFILL_BATCH = 3
MAL_METADATA_RETRY_SECONDS = 15 * 60
MAL_METADATA_REFRESH_SECONDS = 7 * 24 * 60 * 60


def _shikimori_title(rate, anime=None):
    target = rate.get("target") if isinstance(rate.get("target"), dict) else {}
    anime = anime if isinstance(anime, dict) else {}
    return str(target.get("russian") or target.get("name") or target.get("title") or
               anime.get("russian") or anime.get("name") or anime.get("title")
               or rate.get("title") or f"Shikimori #{rate.get('target_id') or target.get('id') or '?'}")[:500]


def _shikimori_public_metadata(anime):
    """Keep only an allow-listed public Shikimori poster URL and small labels."""
    if not isinstance(anime, dict):
        return {"poster_url": None, "shikimori_kind": None, "shikimori_aired_on": None}
    image = anime.get("image") if isinstance(anime.get("image"), dict) else {}
    poster_data = anime.get("poster") if isinstance(anime.get("poster"), dict) else {}
    value = poster_data.get("previewUrl") or image.get("preview")
    parts = urlsplit(value) if isinstance(value, str) else None
    poster = None
    if parts and ".." not in parts.path and "missing_" not in parts.path:
        if not parts.scheme and not parts.netloc and parts.path.startswith("/system/animes/"):
            poster = "https://shikimori.one" + parts.path
        elif parts.scheme == "https" and parts.netloc in {"shikimori.one", "shikimori.io"} \
                and parts.path.startswith("/uploads/poster/animes/"):
            poster = f"https://{parts.netloc}{parts.path}"
    return {"poster_url": poster, "shikimori_kind": str(anime.get("kind") or "")[:32] or None,
            "shikimori_aired_on": str(anime.get("aired_on") or "")[:32] or None}


def _shikimori_rates(rows, statuses, anime_by_id=None, metadata_by_id=None):
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
        try:
            score = int(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        anime = (anime_by_id or {}).get(anime_id)
        cached = (metadata_by_id or {}).get(anime_id, {})
        title_value = _shikimori_title(row, anime)
        if title_value.startswith("Shikimori #") and cached.get("title"):
            title_value = cached["title"]
        public = _shikimori_public_metadata(anime)
        result.append({"external_rate_id": rate_id, "external_anime_id": anime_id,
                       "status": str(row["status"]), "episodes": episodes,
                       "score": score if 1 <= score <= 10 else None,
                       "title": title_value,
                       "poster_url": public.get("poster_url") or cached.get("poster_url"),
                       "shikimori_kind": public.get("shikimori_kind") or cached.get("kind"),
                       "shikimori_aired_on": public.get("shikimori_aired_on") or cached.get("aired_on")})
        seen.add(rate_id)
    return result


async def _shikimori_rates_with_titles(shikimori_client, store, rows, statuses, *, max_missing=None):
    """Use shared cached metadata before requesting a batch from Shikimori."""
    external_ids = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        target = row.get("target") if isinstance(row.get("target"), dict) else {}
        external_ids.append(str(row.get("target_id") or target.get("id") or ""))
    cached = store.external_anime_metadata_many("shikimori", external_ids)
    preliminary = _shikimori_rates(rows, statuses, metadata_by_id=cached)
    missing = [item["external_anime_id"] for item in preliminary
               if item["title"].startswith("Shikimori #")]
    if max_missing is not None:
        missing = missing[:max(0, int(max_missing))]
    if not missing:
        return preliminary
    details = await shikimori_client.animes(missing)
    return _shikimori_rates(rows, statuses, details, cached)

async def _shikimori_candidates(anime_client, shikimori_client, rate):
    """Search every stable Shikimori title and retain only safe candidate data."""
    external = await shikimori_client.anime(rate["external_anime_id"])
    rows = []
    for query in shikimori_titles(external, rate["title"]):
        rows.extend(await anime_client.search(query))
    return external, rank_anime365_candidates(
        external, rows, fallback_title=rate["title"], fallback_mal_id=rate["external_anime_id"])


def create_app(config=None, store=None, anime=None, hentai=None, mal=None, *, proxy_transport=None):
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
    if hentai is None and config.hentai_url:
        hentai = Anime365(HTTPClient(), config.hentai_url)
    if mal is None:
        mal = MyAnimeList(config.mal_client_id)

    app = FastAPI(title="ani365 Mini App", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.anime = anime
    app.state.hentai = hentai
    app.state.mal = mal
    app.state.tickets = EphemeralTickets()
    app.state.subtitle_tickets = EphemeralTickets()
    app.state.poster_tickets = EphemeralTickets()
    app.state.limiter = RateLimiter()
    app.state.sessions = SessionSigner(config.bot_token)
    app.state.config = config
    app.state.download_tickets = EphemeralTickets()
    def source_for_series(series_id):
        value = int(series_id)
        if value >= HENTAI_SERIES_OFFSET:
            if hentai is None or not config.hentai_token:
                raise APIError("Hentai365 не настроен на сервере.")
            return hentai, config.hentai_token, value - HENTAI_SERIES_OFFSET, "hentai365"
        return anime, config.anime_token, value, "anime365"

    def source_client(series_id):
        client, token, _upstream_id, _provider = source_for_series(series_id)
        return client, token

    def is_hentai_series(series_id):
        return int(series_id) >= HENTAI_SERIES_OFFSET

    def public_series_id(provider, upstream_id):
        value = int(upstream_id)
        return HENTAI_SERIES_OFFSET + value if provider == "hentai365" else value

    def source_item(item):
        provider = "hentai365" if is_hentai_series(item["series_id"]) else "anime365"
        return {**item, "provider": provider,
                "upstream_series_id": int(item["series_id"]) - HENTAI_SERIES_OFFSET
                if provider == "hentai365" else int(item["series_id"])}

    def is_hentai_poster_url(value):
        parts = urlsplit(value) if isinstance(value, str) else None
        hosts = {host for host in (urlsplit(config.hentai_url).hostname, "hentai365.ru", "h365-art.org") if host}
        if not parts or parts.scheme != "https" or parts.hostname not in hosts \
                or parts.username or parts.password or parts.query or parts.fragment \
                or not parts.path.startswith("/posters/"):
            return False
        return True

    def hentai_poster(row):
        value = row.get("posterUrlSmall") or row.get("posterUrl")
        return value if is_hentai_poster_url(value) else None

    def poster_proxy_url(user_id, value):
        if not is_hentai_poster_url(value):
            return None
        return "/api/posters/" + app.state.poster_tickets.create(user_id, value)

    def require_shikimori_series(series_id):
        if is_hentai_series(series_id):
            raise HTTPException(404, "Shikimori недоступен для этого каталога.")

    app.state.downloads = DownloadManager(config, store, anime, hentai=hentai,
                                          source_for_series=source_client)
    app.state.proxy_transport = proxy_transport
    app.state.shikimori = Shikimori(config.shikimori_client_id, config.shikimori_client_secret,
                                    config.shikimori_redirect_uri)
    app.state.shikimori_metadata_refresh_after = {}
    app.state.mal_metadata_refresh_after = {}
    app.state.mal_discovery_refresh_after = {}
    app.state.mal_discovery_tasks = {}
    app.state.mal_discovery_semaphore = asyncio.Semaphore(1)
    app.state.shikimori_import_tasks = {}
    app.state.shikimori_background_import_tasks = {}
    app.state.shikimori_background_semaphore = asyncio.Semaphore(1)
    app.state.shikimori_scheduler_task = None

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

    async def require_recommender(
            token: str | None = Header(default=None, alias="X-Recommender-Token")):
        """Authenticate the co-located batch worker without exposing SQLite."""
        secret = str(config.recommender_secret or "")
        if not secret or token is None or not hmac.compare_digest(secret, token):
            # The endpoints are not part of the public API; do not reveal if a
            # recommender has been configured to an unauthenticated caller.
            raise HTTPException(404, "Not found")
        return True

    def require_token(user_id):
        token = config.anime_token
        if not token:
            raise HTTPException(409, "Anime365 token is not configured on the server.")
        return token

    def library_metadata(user_id, series_ids):
        """Merge private Shikimori status with shared, verified public covers."""
        shikimori_metadata = store.shikimori_library_metadata(user_id)
        mal_metadata = store.external_anime_metadata_for_series("mal", series_ids)
        result = {}
        for series_id in series_ids:
            value = dict(shikimori_metadata.get(series_id, {}))
            mal_value = mal_metadata.get(series_id, {})
            # The MAL mapping exists only after Anime365 confirmed its own
            # myAnimeListId, so its public cover can be reused by every user.
            if mal_value.get("poster_url"):
                value["poster_url"] = mal_value["poster_url"]
            result[series_id] = value
        return result

    async def cache_mal_metadata(mal_id, *, force=False):
        """Cache one public MAL record. It contains neither user data nor tokens."""
        cached = store.external_anime_metadata("mal", mal_id)
        if not force and cached and cached.get("poster_url"):
            return cached
        public = await app.state.mal.anime(mal_id)
        store.save_external_anime_metadata(
            "mal", str(mal_id), title=public.get("title"), poster_url=public.get("poster_url"),
            kind=public.get("kind"), aired_on=public.get("aired_on"))
        return store.external_anime_metadata("mal", mal_id)

    async def backfill_mal_metadata(series_ids):
        """Warm a small shared MAL cover cache without blocking on every card."""
        if not getattr(app.state.mal, "configured", False):
            return
        mappings = store.external_ids_for_series("mal", series_ids)
        cached = store.external_anime_metadata_many("mal", mappings.values())
        now, pending = time.time(), []
        for _series_id, mal_id in mappings.items():
            metadata = cached.get(mal_id)
            fresh = metadata and metadata.get("poster_url") and \
                now - float(metadata.get("updated_at") or 0) < MAL_METADATA_REFRESH_SECONDS
            if not fresh and app.state.mal_metadata_refresh_after.get(mal_id, 0) <= now:
                pending.append(mal_id)
        for mal_id in pending[:MAL_METADATA_BACKFILL_BATCH]:
            app.state.mal_metadata_refresh_after[mal_id] = now + MAL_METADATA_RETRY_SECONDS
            try:
                await cache_mal_metadata(mal_id, force=True)
            except MyAnimeListError:
                LOG.info("MyAnimeList metadata backfill unavailable (mal_id=%s)", mal_id)
            else:
                app.state.mal_metadata_refresh_after[mal_id] = now + MAL_METADATA_REFRESH_SECONDS

    async def discover_mal_mapping(series_id, lookup_title):
        """Accept a MAL ID only from an Anime365 row with the same series ID."""
        if not getattr(app.state.mal, "configured", False):
            return
        try:
            async with app.state.mal_discovery_semaphore:
                existing = store.external_ids_for_series("mal", [series_id]).get(series_id)
                if existing:
                    await cache_mal_metadata(existing)
                    return
                rows = await anime.search(lookup_title)
                row = next((item for item in rows if int(item.get("id", 0) or 0) == int(series_id)), None)
                mal_id = str((row or {}).get("myAnimeListId") or "").strip()
                if not mal_id.isdigit() or int(mal_id) <= 0:
                    return
                store.save_external_id(series_id, "mal", mal_id)
                await cache_mal_metadata(mal_id)
        except (APIError, MyAnimeListError, TypeError, ValueError):
            LOG.info("MyAnimeList mapping refresh skipped (series=%s)", series_id)

    def schedule_mal_discovery(series_id, lookup_title):
        if not getattr(app.state.mal, "configured", False):
            return
        now = time.time()
        if app.state.mal_discovery_refresh_after.get(series_id, 0) > now:
            return
        current = app.state.mal_discovery_tasks.get(series_id)
        if current and not current.done():
            return
        app.state.mal_discovery_refresh_after[series_id] = now + MAL_METADATA_RETRY_SECONDS
        task = asyncio.create_task(discover_mal_mapping(series_id, lookup_title))
        app.state.mal_discovery_tasks[series_id] = task
        task.add_done_callback(lambda _: app.state.mal_discovery_tasks.pop(series_id, None))

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

    async def shikimori_exact_mal_series(user_id, rate):
        """Resolve only an Anime365 result that confirms the imported MAL ID.

        A Shikimori title can be censored or have no public poster.  That must
        not prevent a person's own rate from being linked: the stable provider
        ID is enough when Anime365 returns the same MAL ID.  No Shikimori page,
        title search, or poster request is needed here.
        """
        try:
            rows = await anime.series_by_mal_id(rate["external_anime_id"])
        except APIError:
            LOG.info("Shikimori MAL lookup unavailable (user=%s, rate=%s)",
                     user_id, rate["external_rate_id"])
            return None
        if len(rows) != 1:
            return None
        selected = rows[0]
        if str(selected.get("myAnimeListId") or "") != rate["external_anime_id"]:
            LOG.warning("Anime365 MAL lookup returned an unverified result (series=%s)",
                        selected.get("id"))
            return None
        return selected

    async def link_shikimori_exact_mal(user_id, rate, selected):
        """Persist one verified bridge and restore it to this user's library."""
        series_id = int(selected["id"])
        store.add_watchlist(user_id, series_id, title(selected), selected.get("year"),
                            selected.get("typeTitle") or selected.get("type"))
        store.save_external_id(series_id, "shikimori", rate["external_anime_id"])
        store.save_external_id(series_id, "mal", str(selected["myAnimeListId"]))
        bound = store.link_external_user_rate(user_id, "shikimori", rate["external_rate_id"], series_id)
        if bound:
            await merge_shikimori_progress(user_id, series_id, bound)
        return bound

    async def cache_shikimori_metadata(rate, *, force=False):
        """Cache one public record globally; credentials and user rates stay private."""
        cached = store.external_anime_metadata("shikimori", rate["external_anime_id"])
        if not force and cached and cached.get("title") and cached.get("poster_url"):
            return cached
        external = await app.state.shikimori.anime(rate["external_anime_id"])
        public = _shikimori_public_metadata(external)
        cached_title = _shikimori_title({}, external)
        if cached_title.startswith("Shikimori #"):
            cached_title = rate["title"]
        if not public["poster_url"]:
            try:
                graphql = (await app.state.shikimori.posters([rate["external_anime_id"]])).get(
                    rate["external_anime_id"])
            except ShikimoriError:
                graphql = None
            graphql_public = _shikimori_public_metadata(graphql)
            public["poster_url"] = graphql_public["poster_url"] or public["poster_url"]
            if cached_title.startswith("Shikimori #"):
                cached_title = _shikimori_title({}, graphql)
        store.save_external_anime_metadata(
            "shikimori", rate["external_anime_id"], title=cached_title,
            poster_url=public["poster_url"], kind=public["shikimori_kind"],
            aired_on=public["shikimori_aired_on"])
        return store.external_anime_metadata("shikimori", rate["external_anime_id"])

    async def backfill_shikimori_metadata(user_id):
        """Warm old linked cards in one bounded public request, never per card."""
        if not store.external_account_status(user_id, "shikimori")["connected"]:
            return
        now = time.time()
        retry_after = app.state.shikimori_metadata_refresh_after
        pending = store.shikimori_rates_missing_metadata(user_id, SHIKIMORI_METADATA_BACKFILL_BATCH)
        pending = [item for item in pending
                   if retry_after.get(item["external_anime_id"], 0) <= now]
        if not pending:
            return
        pending = pending[:SHIKIMORI_METADATA_BACKFILL_BATCH]
        for item in pending:
            retry_after[item["external_anime_id"]] = now + SHIKIMORI_METADATA_RETRY_SECONDS
        try:
            details = await app.state.shikimori.animes([item["external_anime_id"] for item in pending])
        except ShikimoriError:
            for item in pending:
                retry_after[item["external_anime_id"]] = now + SHIKIMORI_METADATA_RETRY_SECONDS
            LOG.info("Shikimori metadata backfill unavailable (user=%s count=%s)", user_id, len(pending))
            return
        if not isinstance(details, dict):
            details = {}
        public_by_id = {item["external_anime_id"]: _shikimori_public_metadata(
            details.get(item["external_anime_id"])) for item in pending}
        missing_posters = [item["external_anime_id"] for item in pending
                           if not public_by_id[item["external_anime_id"]]["poster_url"]]
        graphql_posters = {}
        if missing_posters:
            try:
                graphql_posters = await app.state.shikimori.posters(missing_posters)
            except ShikimoriError:
                LOG.info("Shikimori GraphQL poster backfill unavailable (user=%s count=%s)",
                         user_id, len(missing_posters))
            if not isinstance(graphql_posters, dict):
                graphql_posters = {}
        for item in pending:
            external = details.get(item["external_anime_id"])
            if isinstance(external, dict):
                public = public_by_id[item["external_anime_id"]]
                graphql_public = _shikimori_public_metadata(
                    graphql_posters.get(item["external_anime_id"]))
                public["poster_url"] = graphql_public["poster_url"] or public["poster_url"]
                cached_title = _shikimori_title({}, external)
                if cached_title.startswith("Shikimori #"):
                    cached_title = item["title"]
                store.save_external_anime_metadata(
                    "shikimori", item["external_anime_id"], title=cached_title,
                    poster_url=public["poster_url"], kind=public["shikimori_kind"],
                    aired_on=public["shikimori_aired_on"])
                retry_after[item["external_anime_id"]] = now + SHIKIMORI_METADATA_REFRESH_SECONDS
            else:
                retry_after[item["external_anime_id"]] = now + SHIKIMORI_METADATA_RETRY_SECONDS

    async def sync_shikimori_progress(user_id, series_id, episode_number, *, series_complete=False):
        """Best-effort completion sync; playback is never blocked by Shikimori."""
        try:
            if is_hentai_series(series_id):
                return
            watched = number(episode_number)
            if watched <= 0 or watched != int(watched):
                return
            rate = store.external_rate_for_series(user_id, "shikimori", series_id)
            account = store.external_account(user_id, "shikimori")
            if not rate or not account or not account["sync_enabled"]:
                return
            update_progress = watched > rate["episodes"]
            update_status = bool(series_complete and account["auto_complete"]
                                 and rate.get("status") != "completed")
            if not update_progress and not update_status:
                return
            account = await shikimori_account(user_id)
            values = {}
            if update_progress:
                values["episodes"] = int(watched)
            if update_status:
                values["status"] = "completed"
            await app.state.shikimori.update_user_rate(account["access_token"], rate["external_rate_id"],
                                                       **values)
            if update_progress:
                store.update_external_rate_episodes(user_id, "shikimori", rate["external_rate_id"], int(watched))
            if update_status:
                store.update_external_rate_status(user_id, "shikimori", rate["external_rate_id"], "completed")
        except (ShikimoriError, HTTPException, ValueError):
            LOG.warning("Shikimori progress sync failed (user=%s series=%s)", user_id, series_id)

    def persist_shikimori_rates(user_id, rates):
        """Store private rates and shared public metadata without credentials."""
        store.import_external_rates(user_id, "shikimori", rates)
        for rate in rates:
            metadata = {key: rate.get(key) for key in
                        ("poster_url", "shikimori_kind", "shikimori_aired_on")}
            if any(metadata.values()) or not rate["title"].startswith("Shikimori #"):
                store.save_external_anime_metadata(
                    "shikimori", rate["external_anime_id"], title=rate["title"],
                    poster_url=metadata["poster_url"], kind=metadata["shikimori_kind"],
                    aired_on=metadata["shikimori_aired_on"])

    async def restore_linked_shikimori_rates(user_id, rates):
        """Restore confirmed links after an import without changing list status."""
        imported_ids = {item["external_rate_id"] for item in rates}
        linked = []
        for rate in store.external_user_rates(user_id, "shikimori", linked=True):
            if rate["external_rate_id"] not in imported_ids:
                continue
            series_id = rate["anime365_series_id"]
            store.add_watchlist(user_id, series_id, rate["title"])
            await merge_shikimori_progress(user_id, series_id, rate)
            linked.append(rate)
        return linked

    async def run_shikimori_background_import(user_id):
        """Refresh a saved list slowly in the background, with durable retries."""
        state = store.start_external_import(user_id, "shikimori")
        if not state:
            return
        statuses = set(state["statuses"])
        try:
            async with app.state.shikimori_background_semaphore:
                account = await shikimori_account(user_id)
                upstream = await app.state.shikimori.user_rates(
                    account["access_token"], account["external_user_id"])
                external_ids = []
                for row in upstream:
                    target = row.get("target") if isinstance(row.get("target"), dict) else {}
                    external_ids.append(str(row.get("target_id") or target.get("id") or ""))
                cached = store.external_anime_metadata_many("shikimori", external_ids)
                initial = _shikimori_rates(upstream, statuses, metadata_by_id=cached)
                # First persist rate IDs and any cached labels. A restart or a
                # temporary metadata throttle never makes the user re-import.
                persist_shikimori_rates(user_id, initial)
                await restore_linked_shikimori_rates(user_id, initial)
                hydrated = await _shikimori_rates_with_titles(
                    app.state.shikimori, store, upstream, statuses)
                persist_shikimori_rates(user_id, hydrated)
                await restore_linked_shikimori_rates(user_id, hydrated)
        except asyncio.CancelledError:
            store.fail_external_import(user_id, "shikimori", "cancelled",
                                       retry_at=time.time())
            raise
        except (ShikimoriError, HTTPException, ValueError):
            store.fail_external_import(user_id, "shikimori", "shikimori_unavailable",
                                       retry_at=time.time() + SHIKIMORI_BACKGROUND_RETRY_SECONDS)
            LOG.info("Shikimori background import deferred (user=%s)", user_id)
        except Exception:
            store.fail_external_import(user_id, "shikimori", "unexpected_error",
                                       retry_at=time.time() + SHIKIMORI_BACKGROUND_RETRY_SECONDS)
            LOG.exception("Shikimori background import failed (user=%s)", user_id)
        else:
            store.finish_external_import(user_id, "shikimori", len(hydrated),
                                         next_run_at=time.time() + config.shikimori_import_interval)

    def schedule_shikimori_background_import(user_id):
        current = app.state.shikimori_background_import_tasks.get(user_id)
        if current and not current.done():
            return current
        task = asyncio.create_task(run_shikimori_background_import(user_id))
        app.state.shikimori_background_import_tasks[user_id] = task
        task.add_done_callback(lambda _: app.state.shikimori_background_import_tasks.pop(user_id, None))
        return task

    async def shikimori_background_scheduler():
        """Resume due imports after deploy/restart and refresh them periodically."""
        while True:
            try:
                for user_id in store.external_account_user_ids("shikimori"):
                    if store.external_import_state(user_id, "shikimori") is None:
                        store.queue_external_import(user_id, "shikimori", SHIKIMORI_BACKGROUND_STATUSES)
                for item in store.due_external_imports("shikimori"):
                    schedule_shikimori_background_import(item["user_id"])
            except Exception:
                LOG.exception("Shikimori background scheduler failed")
            await asyncio.sleep(SHIKIMORI_BACKGROUND_POLL_SECONDS)

    def schedule_shikimori_metadata_import(user_id, upstream, statuses):
        """Finish a large public title hydration after the responsive import reply."""
        current = app.state.shikimori_import_tasks.get(user_id)
        if current and not current.done():
            return

        async def worker():
            try:
                rates = await _shikimori_rates_with_titles(
                    app.state.shikimori, store, upstream, statuses)
                persist_shikimori_rates(user_id, rates)
            except (ShikimoriError, ValueError):
                LOG.info("Shikimori title hydration deferred (user=%s)", user_id)
            except Exception:
                LOG.exception("Shikimori title hydration failed (user=%s)", user_id)

        task = asyncio.create_task(worker())
        app.state.shikimori_import_tasks[user_id] = task
        task.add_done_callback(lambda _: app.state.shikimori_import_tasks.pop(user_id, None))

    @app.on_event("shutdown")
    async def shutdown():
        scheduler = app.state.shikimori_scheduler_task
        if scheduler:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        for task in app.state.shikimori_import_tasks.values():
            task.cancel()
        if app.state.shikimori_import_tasks:
            await asyncio.gather(*app.state.shikimori_import_tasks.values(), return_exceptions=True)
        for task in app.state.shikimori_background_import_tasks.values():
            task.cancel()
        if app.state.shikimori_background_import_tasks:
            await asyncio.gather(*app.state.shikimori_background_import_tasks.values(), return_exceptions=True)
        for task in app.state.mal_discovery_tasks.values():
            task.cancel()
        if app.state.mal_discovery_tasks:
            await asyncio.gather(*app.state.mal_discovery_tasks.values(), return_exceptions=True)
        await app.state.downloads.stop()
        if owns_store:
            store.close()

    @app.on_event("startup")
    async def startup():
        await app.state.downloads.start()
        app.state.shikimori_scheduler_task = asyncio.create_task(shikimori_background_scheduler())

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).with_name("web_static") / "index.html",
                            headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/assets/{asset_name}")
    async def assets(asset_name: str):
        allowed = {"app.js", "app.css", "anime-night.webp", "hls-1.7.3.min.js"}
        if asset_name not in allowed:
            raise HTTPException(404, "Not found")
        return FileResponse(Path(__file__).with_name("web_static") / asset_name,
                            headers={"Cache-Control": "public, max-age=31536000, immutable"}
                            if asset_name == "hls-1.7.3.min.js"
                            else {"Cache-Control": "no-store, max-age=0"})

    @app.get("/internal/recommendations/profiles")
    async def recommendation_profiles(_=Depends(require_recommender)):
        """Small, credential-free input snapshot for the weekly local worker."""
        profiles = []
        for candidate_user_id in store.external_account_user_ids("shikimori"):
            if not store.is_allowed(candidate_user_id, config.owner_id):
                continue
            profile = store.recommendation_profile(candidate_user_id)
            if profile is not None:
                profiles.append(profile)
        return {"profiles": profiles}

    @app.post("/internal/recommendations/resolve")
    async def resolve_recommendation_candidates(payload: RecommendationCandidateResolveRequest,
                                                _=Depends(require_recommender)):
        """Keep only candidates Anime365 confirms through an exact MAL bridge."""
        origin = urlsplit(config.anime_url)

        def safe_poster(row):
            value = row.get("posterUrlSmall") or row.get("posterUrl")
            parts = urlsplit(value) if isinstance(value, str) else None
            if (not parts or parts.scheme != "https" or parts.hostname != origin.hostname
                    or parts.username or parts.password or parts.query or parts.fragment
                    or not parts.path.startswith("/posters/")):
                return None
            return value

        async def resolve_one(item):
            bridges = []
            for value in (item.mal_id, item.shikimori_anime_id):
                if value and value not in bridges:
                    bridges.append(value)
            for bridge in bridges:
                try:
                    rows = await anime.series_by_mal_id(bridge)
                except APIError:
                    continue
                if not rows:
                    continue
                row = rows[0]
                try:
                    series_id = int(row["id"])
                except (KeyError, TypeError, ValueError):
                    continue
                value = {"shikimori_anime_id": item.shikimori_anime_id,
                         "anime365_series_id": series_id, "title": title(row),
                         "year": row.get("year"),
                         "series_type": row.get("typeTitle") or row.get("type"),
                         "poster_url": safe_poster(row)}
                store.save_external_id(series_id, "shikimori", item.shikimori_anime_id)
                store.save_external_anime_metadata(
                    "shikimori", item.shikimori_anime_id, title=value["title"],
                    poster_url=value["poster_url"], kind=value["series_type"], aired_on=value["year"])
                return value
            return None

        semaphore = asyncio.Semaphore(3)

        async def bounded(item):
            async with semaphore:
                return await resolve_one(item)

        resolved = await asyncio.gather(*(bounded(item) for item in payload.items))
        return {"items": [item for item in resolved if item is not None]}

    @app.put("/internal/recommendations/{target_user_id}")
    async def save_recommendations(target_user_id: int, payload: RecommendationReplaceRequest,
                                   _=Depends(require_recommender)):
        if not store.is_allowed(target_user_id, config.owner_id):
            raise HTTPException(404, "Not found")
        saved = store.replace_recommendations(
            target_user_id, [item.model_dump() for item in payload.items])
        return {"saved": saved}

    @app.get("/api/me")
    async def me(response: Response, user_id=Depends(authenticated_user)):
        response.set_cookie("ani365_mini_session", app.state.sessions.create(user_id),
                            max_age=SESSION_MAX_AGE, httponly=True, secure=config.web_cookie_secure,
                            samesite="strict", path="/")
        return {"user_id": user_id, "anime365_connected": bool(config.anime_token)}

    @app.get("/api/recommendations")
    async def recommendations(offset: int = Query(default=0, ge=0),
                              limit: int = Query(default=12, ge=1, le=36),
                              user_id=Depends(authenticated_user)):
        state = store.recommendation_state(user_id)
        items = store.recommendations(user_id, limit=limit, offset=offset)
        return {**state, "items": items, "offset": offset,
                "next_offset": offset + len(items) if len(items) == limit else None}

    @app.get("/api/shikimori/status")
    async def shikimori_status(user_id=Depends(authenticated_user)):
        return {"configured": app.state.shikimori.configured,
                **store.external_account_status(user_id, "shikimori"),
                "background_import": store.external_import_state(user_id, "shikimori")}

    @app.patch("/api/shikimori/settings")
    async def shikimori_settings(payload: ShikimoriSettingsRequest,
                                 user_id=Depends(authenticated_user)):
        if payload.sync_enabled is None and payload.auto_complete is None:
            raise HTTPException(422, "Выберите настройку Shikimori.")
        if not store.external_account_status(user_id, "shikimori")["connected"]:
            raise HTTPException(409, "Сначала подключите Shikimori.")
        if payload.sync_enabled is not None:
            store.set_external_sync_enabled(user_id, "shikimori", payload.sync_enabled)
        if payload.auto_complete is not None:
            store.set_external_auto_complete(user_id, "shikimori", payload.auto_complete)
        return store.external_account_status(user_id, "shikimori")

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
        store.queue_external_import(user_id, "shikimori", SHIKIMORI_BACKGROUND_STATUSES)
        schedule_shikimori_background_import(user_id)
        return HTMLResponse("<h1>Shikimori подключён.</h1><p>Список импортируется в фоне. Вернитесь в Telegram Mini App.</p>")

    @app.delete("/api/shikimori")
    async def shikimori_disconnect(user_id=Depends(authenticated_user)):
        task = app.state.shikimori_background_import_tasks.get(user_id)
        if task and not task.done():
            task.cancel()
        store.forget_external_account(user_id, "shikimori")
        store.forget_external_import_state(user_id, "shikimori")
        return Response(status_code=204)

    @app.post("/api/shikimori/import/background", status_code=202)
    async def shikimori_background_import(payload: ShikimoriImportRequest,
                                          user_id=Depends(authenticated_user)):
        """Queue a durable import and return immediately to the Mini App."""
        selected = set(payload.statuses)
        if not selected or not selected <= SHIKIMORI_STATUSES:
            raise HTTPException(422, "Некорректный статус Shikimori.")
        if not store.external_account(user_id, "shikimori"):
            raise HTTPException(409, "Сначала подключите Shikimori.")
        state = store.queue_external_import(user_id, "shikimori", selected)
        schedule_shikimori_background_import(user_id)
        return {"background_import": state}

    @app.get("/api/shikimori/import/preview")
    async def shikimori_import_preview(statuses: list[str] = Query(default=["watching", "planned", "completed"]),
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
            items = await _shikimori_rates_with_titles(
                app.state.shikimori, store, rates, selected,
                max_missing=SHIKIMORI_IMPORT_FOREGROUND_METADATA_BATCH)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"items": items, "count": len(items),
                "metadata_refreshing": any(item["title"].startswith("Shikimori #") for item in items),
                "policy": "progress=max(local, shikimori)"}

    @app.post("/api/shikimori/import")
    async def shikimori_import(payload: ShikimoriImportRequest, user_id=Depends(authenticated_user)):
        selected = set(payload.statuses)
        if not selected or not selected <= SHIKIMORI_STATUSES:
            raise HTTPException(422, "Некорректный статус Shikimori.")
        task = app.state.shikimori_background_import_tasks.get(user_id)
        if task and not task.done():
            raise HTTPException(409, "Фоновый импорт уже выполняется. Дождитесь его завершения.")
        store.queue_external_import(user_id, "shikimori", selected)
        store.start_external_import(user_id, "shikimori")
        try:
            account = await shikimori_account(user_id)
            upstream = await app.state.shikimori.user_rates(account["access_token"], account["external_user_id"])
        except ShikimoriError as exc:
            store.fail_external_import(user_id, "shikimori", "shikimori_unavailable",
                                       retry_at=time.time() + SHIKIMORI_BACKGROUND_RETRY_SECONDS)
            raise HTTPException(502, str(exc)) from None
        except HTTPException:
            store.fail_external_import(user_id, "shikimori", "account_unavailable",
                                       retry_at=time.time() + SHIKIMORI_BACKGROUND_RETRY_SECONDS)
            raise
        try:
            rates = await _shikimori_rates_with_titles(
                app.state.shikimori, store, upstream, selected,
                max_missing=SHIKIMORI_IMPORT_FOREGROUND_METADATA_BATCH)
        except ShikimoriError as exc:
            store.fail_external_import(user_id, "shikimori", "shikimori_unavailable",
                                       retry_at=time.time() + SHIKIMORI_BACKGROUND_RETRY_SECONDS)
            raise HTTPException(502, str(exc)) from None
        persist_shikimori_rates(user_id, rates)
        metadata_refreshing = any(rate["title"].startswith("Shikimori #") for rate in rates)
        if metadata_refreshing:
            schedule_shikimori_metadata_import(user_id, upstream, selected)
        linked = await restore_linked_shikimori_rates(user_id, rates)
        store.finish_external_import(user_id, "shikimori", len(rates),
                                     next_run_at=time.time() + config.shikimori_import_interval)
        unmatched_total = store.external_user_rate_count(user_id, "shikimori", linked=False)
        unmatched = store.external_user_rates(user_id, "shikimori", linked=False,
                                               limit=SHIKIMORI_IMPORT_PAGE_SIZE)
        return {"imported": len(rates), "linked": len(linked), "unmatched": unmatched,
                "unmatched_total": unmatched_total,
                "next_offset": len(unmatched) if len(unmatched) < unmatched_total else None,
                "metadata_refreshing": metadata_refreshing,
                "policy": "progress=max(local, shikimori); Shikimori status is primary"}

    @app.get("/api/shikimori/imports")
    async def shikimori_imports(linked: bool | None = None, query: str | None = Query(default=None, max_length=500),
                                offset: int = Query(default=0, ge=0),
                                limit: int = Query(default=SHIKIMORI_IMPORT_PAGE_SIZE, ge=1, le=100),
                                user_id=Depends(authenticated_user)):
        if query is not None and query.strip():
            if linked is not False:
                raise HTTPException(422, "Поиск доступен только среди непривязанных тайтлов.")
            items = store.search_unlinked_external_user_rates(user_id, "shikimori", query, limit=limit)
            return {"items": items, "total": len(items), "next_offset": None,
                    "query": query.strip(),
                    "policy": "progress=max(local, shikimori); Shikimori status is primary"}
        total = store.external_user_rate_count(user_id, "shikimori", linked=linked)
        items = store.external_user_rates(user_id, "shikimori", linked=linked, offset=offset, limit=limit)
        next_offset = offset + len(items)
        return {"items": items, "total": total,
                "next_offset": next_offset if next_offset < total else None,
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
                return rate, await shikimori_exact_mal_series(user_id, rate)

        matches = await asyncio.gather(*(lookup(rate) for rate in rates))
        linked = 0
        for rate, selected in matches:
            if selected is None:
                continue
            bound = await link_shikimori_exact_mal(user_id, rate, selected)
            if bound:
                linked += 1
        remaining = store.external_user_rate_count(user_id, "shikimori", linked=False)
        return {"checked": len(rates), "linked": linked, "remaining": remaining,
                "batch_limited": remaining > 0 and len(rates) == SHIKIMORI_AUTO_LINK_BATCH}

    @app.post("/api/shikimori/imports/{rate_id}/auto-link")
    async def shikimori_exact_auto_link(rate_id: str, user_id=Depends(authenticated_user)):
        """Link one imported item by its verified MAL ID without title matching."""
        app.state.limiter.check(user_id, "shikimori-exact-auto-link", 20)
        rate = store.external_user_rate(user_id, "shikimori", rate_id)
        if not rate:
            raise HTTPException(404, "Импортированный тайтл не найден.")
        if rate["anime365_series_id"] is not None:
            return {"item": rate, "verified_mal": True, "already_linked": True}
        selected = await shikimori_exact_mal_series(user_id, rate)
        if selected is None:
            raise HTTPException(404, "Anime365 не подтвердил точное совпадение MAL ID.")
        linked = await link_shikimori_exact_mal(user_id, rate, selected)
        if not linked:
            raise HTTPException(409, "Не удалось сохранить привязку.")
        return {"item": linked, "verified_mal": True, "already_linked": False}

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
        metadata = _shikimori_public_metadata(_external)
        if any(metadata.values()):
            store.save_external_anime_metadata(
                "shikimori", rate["external_anime_id"], title=_shikimori_title({}, _external),
                poster_url=metadata["poster_url"],
                kind=metadata["shikimori_kind"], aired_on=metadata["shikimori_aired_on"])
        store.add_watchlist(user_id, series_id, title(selected), selected.get("year"),
                            selected.get("typeTitle") or selected.get("type"))
        if verified:
            store.save_external_id(series_id, "shikimori", rate["external_anime_id"])
            store.save_external_id(series_id, "mal", str(selected["myAnimeListId"]))
        linked = store.link_external_user_rate(user_id, "shikimori", rate_id, series_id)
        await merge_shikimori_progress(user_id, series_id, linked)
        return {"item": linked, "verified_mal": verified}

    @app.get("/api/library")
    async def library(user_id=Depends(authenticated_user)):
        await backfill_shikimori_metadata(user_id)
        watched = store.list_watchlist(user_id)
        anime_series_ids = [item["series_id"] for item in watched if not is_hentai_series(item["series_id"])]
        known_mal = store.external_ids_for_series("mal", anime_series_ids)
        # Older local cards predate the MAL mapping. Resolve only a few in the
        # background and accept a result solely when Anime365 returns the same
        # series ID, so opening a large library stays responsive and safe.
        missing_mal = [item for item in watched if not is_hentai_series(item["series_id"])
                       and item["series_id"] not in known_mal]
        for item in missing_mal[:MAL_METADATA_BACKFILL_BATCH]:
            schedule_mal_discovery(item["series_id"], item["title"])
        await backfill_mal_metadata(anime_series_ids)
        metadata = library_metadata(user_id, anime_series_ids)
        hentai_metadata = store.external_anime_metadata_many(
            "hentai365", [int(item["series_id"]) - HENTAI_SERIES_OFFSET for item in watched
                          if is_hentai_series(item["series_id"])])
        all_items = [{**source_item(item), **(
                 {"poster_url": hentai_metadata.get(str(int(item["series_id"]) - HENTAI_SERIES_OFFSET), {}).get("poster_url")}
                 if is_hentai_series(item["series_id"])
                 else metadata.get(item["series_id"], {}))} for item in watched]
        hentai_items = [item for item in all_items if item["provider"] == "hentai365"]
        for item in hentai_items:
            item["poster_url"] = poster_proxy_url(user_id, item.get("poster_url"))
        items = [item for item in all_items if item["provider"] == "anime365"]
        # A local title without Shikimori status remains "watching", preserving
        # the old bot workflow. Once a status exists, Shikimori is the source
        # of truth for its library section.
        groups = {status: [] for status in SHIKIMORI_STATUSES}
        groups["watching"] = []
        for item in items:
            status = item.get("shikimori_status")
            groups[status if status in SHIKIMORI_STATUSES else "watching"].append(item)
        active = groups["watching"] + groups["rewatching"]
        active_ids = {item["series_id"] for item in active}
        recent = [{**source_item(item), **(
                  {"poster_url": hentai_metadata.get(str(int(item["series_id"]) - HENTAI_SERIES_OFFSET), {}).get("poster_url")}
                  if is_hentai_series(item["series_id"])
                  else metadata.get(item["series_id"], {}))} for item in store.recent_playback(user_id)
                  if item["series_id"] in active_ids]
        new_episodes = [item for item in active
                        if item.get("last_watched_episode_number") is not None
                        and number(item.get("last_available_episode_number"))
                        > number(item.get("last_watched_episode_number"))]
        return {"items": active, "continue": recent, "new_episodes": new_episodes,
                "groups": groups, "hentai_items": hentai_items}

    @app.post("/api/library")
    async def add_library(payload: AddLibraryRequest, user_id=Depends(authenticated_user)):
        if payload.provider == "hentai365":
            if not is_hentai_series(payload.series_id):
                raise HTTPException(422, "Некорректный источник каталога.")
            source_for_series(payload.series_id)
        elif is_hentai_series(payload.series_id):
            raise HTTPException(422, "Некорректный источник каталога.")
        item = store.add_watchlist(user_id, payload.series_id, payload.title, payload.year,
                                   payload.series_type)
        if payload.provider == "anime365":
            schedule_mal_discovery(payload.series_id, payload.title)
        return item

    @app.delete("/api/library/{series_id}")
    async def delete_library(series_id: int, user_id=Depends(authenticated_user)):
        if not store.remove_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        return Response(status_code=204)

    @app.patch("/api/library/{series_id}/notifications")
    async def update_library_notifications(series_id: int, payload: NotificationRequest,
                                           user_id=Depends(authenticated_user)):
        if is_hentai_series(series_id):
            raise HTTPException(422, "Уведомления недоступны для Hentai365.")
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

    @app.post("/api/library/{series_id}/episodes/{episode_id}/watched")
    async def mark_library_episode_watched(series_id: int, episode_id: int,
                                           user_id=Depends(authenticated_user)):
        """Manually complete an episode when a player event was missed."""
        app.state.limiter.check(user_id, "manual-progress", 30)
        if not store.has_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            source, _token, upstream_series_id, _provider = source_for_series(series_id)
            rows = await source.episodes(upstream_series_id)
        except APIError as exc:
            _api_error(exc)
        episode = next((row for row in rows if int(row.get("id", 0)) == episode_id), None)
        if episode is None:
            raise HTTPException(422, "Выбранная серия больше недоступна.")
        episode_number = str(episode.get("episodeFull") or episode.get("episodeInt") or "?")
        result = store.record_playback_progress(
            user_id, series_id, episode_id, 0, 0, episode_number, ended=True,
            completion_threshold=config.playback_completion_threshold)
        if result and result["completed"] and not is_hentai_series(series_id):
            asyncio.create_task(sync_shikimori_progress(
                user_id, series_id, episode_number,
                series_complete=bool(rows) and int(rows[-1].get("id", 0) or 0) == episode_id))
        return result

    @app.patch("/api/library/{series_id}/shikimori-status")
    async def update_shikimori_status(series_id: int, payload: ShikimoriStatusRequest,
                                      user_id=Depends(authenticated_user)):
        require_shikimori_series(series_id)
        if not store.has_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        rate = store.external_rate_for_series(user_id, "shikimori", series_id)
        if not rate:
            raise HTTPException(409, "Сначала привяжите этот тайтл к Shikimori.")
        account = await shikimori_account(user_id)
        try:
            await app.state.shikimori.update_user_rate(account["access_token"], rate["external_rate_id"],
                                                       status=payload.status)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        store.update_external_rate_status(user_id, "shikimori", rate["external_rate_id"], payload.status)
        return {"status": payload.status}

    @app.get("/api/library/{series_id}/shikimori-rates")
    async def shikimori_rates_for_library_item(series_id: int, query: str = Query(default="", max_length=200),
                                                user_id=Depends(authenticated_user)):
        require_shikimori_series(series_id)
        if not store.has_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        rates = store.search_unlinked_external_user_rates(user_id, "shikimori", query)
        return {"items": rates, "query": query.strip()}

    @app.post("/api/library/{series_id}/shikimori-link")
    async def link_shikimori_library_item(series_id: int, payload: ShikimoriLibraryLinkRequest,
                                          user_id=Depends(authenticated_user)):
        require_shikimori_series(series_id)
        if not store.has_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        rate = store.external_user_rate(user_id, "shikimori", payload.external_rate_id)
        if not rate:
            raise HTTPException(404, "Тайтл Shikimori не найден в вашем импорте.")
        if rate["anime365_series_id"] not in (None, series_id):
            raise HTTPException(409, "Этот тайтл Shikimori уже привязан к другому Anime365 тайтлу.")
        linked = store.replace_external_user_rate_link(user_id, "shikimori", rate["external_rate_id"], series_id)
        if linked is None:
            raise HTTPException(409, "Не удалось сохранить привязку.")
        try:
            await cache_shikimori_metadata(linked)
        except ShikimoriError:
            LOG.info("Shikimori metadata refresh skipped (user=%s series=%s)", user_id, series_id)
        await merge_shikimori_progress(user_id, series_id, linked)
        return {"item": linked}

    @app.post("/api/library/{series_id}/shikimori-metadata")
    async def refresh_shikimori_library_metadata(series_id: int, user_id=Depends(authenticated_user)):
        require_shikimori_series(series_id)
        if not store.has_watchlist(user_id, series_id):
            raise HTTPException(404, "Anime is not in your library.")
        rate = store.external_rate_for_series(user_id, "shikimori", series_id)
        if not rate:
            raise HTTPException(409, "Сначала привяжите этот тайтл к Shikimori.")
        app.state.limiter.check(user_id, "shikimori-metadata", 12)
        try:
            metadata = await cache_shikimori_metadata(rate, force=True)
        except ShikimoriError as exc:
            raise HTTPException(502, str(exc)) from None
        return {"poster_url": metadata.get("poster_url"), "updated": True}

    @app.get("/api/catalog")
    async def catalog(query: str, user_id=Depends(authenticated_user)):
        if not query.strip() or len(query) > 200:
            raise HTTPException(422, "Введите название до 200 символов.")
        query = query.strip()
        # A copied Shikimori permalink starts with its stable anime ID.  Its
        # slug is not an Anime365 search term, so use the exact MAL bridge
        # instead of asking either provider to resolve a blocked page.
        shikimori_slug = SHIKIMORI_ANIME_SLUG.search(query)
        try:
            rows = await anime.series_by_mal_id(shikimori_slug.group(1)) if shikimori_slug \
                else await anime.search(query)
        except APIError as exc:
            _api_error(exc)
        metadata = store.external_anime_metadata_for_series(
            "shikimori", [int(row["id"]) for row in rows if str(row.get("id", "")).isdigit()])
        mal_metadata = store.external_anime_metadata_many(
            "mal", [str(row.get("myAnimeListId") or "") for row in rows])
        # Anime365 search intentionally requests only its stable catalogue
        # fields.  A public Shikimori cover is used only when a confirmed global
        # mapping already exists; searching must not create one API request per
        # result or guess a title-to-poster association.
        anime_origin = urlsplit(config.anime_url)

        def anime365_poster(row):
            value = row.get("posterUrlSmall") or row.get("posterUrl")
            parts = urlsplit(value) if isinstance(value, str) else None
            if not parts or parts.scheme != "https" or parts.hostname != anime_origin.hostname \
                    or parts.username or parts.password or parts.query or parts.fragment \
                    or not parts.path.startswith("/posters/"):
                return None
            return value

        return {"items": [{
            "series_id": int(row["id"]), "title": title(row),
            "year": row.get("year"), "series_type": row.get("typeTitle") or row.get("type"),
            "poster_url": anime365_poster(row)
                          or mal_metadata.get(str(row.get("myAnimeListId") or ""), {}).get("poster_url")
                          or metadata.get(int(row["id"]), {}).get("poster_url"),
        } for row in rows if str(row.get("id", "")).isdigit()]}

    @app.get("/api/hentai/catalog")
    async def hentai_catalog(query: str, user_id=Depends(authenticated_user)):
        if not query.strip() or len(query) > 200:
            raise HTTPException(422, "Введите название до 200 символов.")
        if hentai is None or not config.hentai_token:
            raise HTTPException(409, "Hentai365 не настроен на сервере.")
        try:
            rows = await hentai.search(query.strip())
        except APIError as exc:
            _api_error(exc)
        items = []
        for row in rows:
            if not str(row.get("id", "")).isdigit():
                continue
            poster_url = hentai_poster(row)
            external_id = str(row["id"])
            store.save_external_anime_metadata("hentai365", external_id, title=title(row),
                                               poster_url=poster_url)
            items.append({
                "series_id": public_series_id("hentai365", row["id"]), "title": title(row),
                "year": row.get("year"), "series_type": row.get("typeTitle") or row.get("type"),
                "poster_url": poster_proxy_url(user_id, poster_url), "provider": "hentai365",
            })
        return {"items": items}

    @app.get("/api/library/{series_id}")
    async def library_item(series_id: int, user_id=Depends(authenticated_user)):
        item = store.get_watchlist(user_id, series_id)
        if item is None:
            raise HTTPException(404, "Anime is not in your library.")
        if not is_hentai_series(series_id):
            schedule_mal_discovery(series_id, item["title"])
            await backfill_mal_metadata([series_id])
        item = {**source_item(item), **(
                {"poster_url": (store.external_anime_metadata(
                    "hentai365", int(series_id) - HENTAI_SERIES_OFFSET) or {}).get("poster_url")}
                if is_hentai_series(series_id)
                else library_metadata(user_id, [series_id]).get(series_id, {}))}
        if is_hentai_series(series_id):
            item["poster_url"] = poster_proxy_url(user_id, item.get("poster_url"))
        try:
            source, _token, upstream_series_id, _provider = source_for_series(series_id)
            episodes = await source.episodes(upstream_series_id)
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
            source, _token, upstream_series_id, _provider = source_for_series(series_id)
            rows = await source.episodes(upstream_series_id)
        except APIError as exc:
            _api_error(exc)
        item = store.get_watchlist(user_id, series_id)
        return {"items": [_episode_payload(row, item.get("last_watched_episode_id"),
                                             item.get("last_watched_episode_number")) for row in rows]}

    @app.get("/api/episodes/{episode_id}/translations")
    async def translations(episode_id: int, series_id: int | None = Query(default=None, gt=0),
                           user_id=Depends(authenticated_user)):
        try:
            source, _token, _upstream_series_id, _provider = source_for_series(series_id or 1)
            rows = await source.translations(episode_id)
        except APIError as exc:
            _api_error(exc)
        return {"groups": [{"kind": group.kind, "language": group.language, "label": group.label,
                             "items": group.translations} for group in group_translations(rows)]}

    @app.get("/api/translations/{translation_id}/qualities")
    async def available_qualities(translation_id: int, series_id: int | None = Query(default=None, gt=0),
                                  user_id=Depends(authenticated_user)):
        try:
            source, token, _upstream_series_id, _provider = source_for_series(series_id or 1)
            values = await source.available_qualities(translation_id, token)
        except APIError as exc:
            _api_error(exc)
        return {"items": values}

    @app.post("/api/play")
    async def play(payload: PlayRequest, user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "play", 30)
        if not store.has_watchlist(user_id, payload.series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            source_client, token, upstream_series_id, _provider = source_for_series(payload.series_id)
            episodes = await source_client.episodes(upstream_series_id)
            episode = next((row for row in episodes if int(row.get("id", 0)) == payload.episode_id), None)
            if episode is None:
                raise HTTPException(422, "Выбранная серия больше недоступна.")
            translations = await source_client.translations(payload.episode_id)
            if not any(int(row.get("id", 0)) == payload.translation_id for row in translations):
                raise HTTPException(422, "Выбранный перевод больше недоступен.")
            source = await source_client.media_source(payload.translation_id, payload.quality, token)
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
            source, _token, upstream_series_id, _provider = source_for_series(payload.series_id)
            rows = await source.episodes(upstream_series_id)
        except APIError as exc:
            _api_error(exc)
        episode = next((row for row in rows if int(row.get("id", 0)) == payload.episode_id), None)
        if episode is None:
            raise HTTPException(422, "Выбранная серия больше недоступна.")
        result = store.record_playback_progress(
            user_id, payload.series_id, payload.episode_id, payload.position_seconds,
            payload.duration_seconds, str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
            ended=payload.ended, completion_threshold=config.playback_completion_threshold)
        if result and result["completed"] and not is_hentai_series(payload.series_id):
            # Keep local playback durable even when Shikimori is temporarily
            # unavailable. The task catches all expected remote failures.
            asyncio.create_task(sync_shikimori_progress(
                user_id, payload.series_id,
                str(episode.get("episodeFull") or episode.get("episodeInt") or "?"),
                series_complete=bool(rows) and int(rows[-1].get("id", 0) or 0) == payload.episode_id))
        return result

    @app.post("/api/downloads")
    async def create_download(payload: DownloadRequest, user_id=Depends(authenticated_user)):
        app.state.limiter.check(user_id, "download", 12)
        if not store.has_watchlist(user_id, payload.series_id):
            raise HTTPException(404, "Anime is not in your library.")
        try:
            source, _token, upstream_series_id, _provider = source_for_series(payload.series_id)
            episodes = await source.episodes(upstream_series_id)
            episode = next((row for row in episodes if int(row.get("id", 0)) == payload.episode_id), None)
            translations = await source.translations(payload.episode_id) if episode else ()
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

    @app.post("/api/downloads/batch")
    async def create_batch_download(payload: BatchDownloadRequest, user_id=Depends(authenticated_user)):
        """Queue explicitly configured Anime365 episodes in one request.

        Translation identifiers belong to a particular episode.  Unlike travel
        mode, this endpoint never tries to infer a replacement translation or
        quality: the Mini App has already shown the available choices for each
        selected episode.  A title with OVA, duplicate numbering, or uneven
        releases can therefore be prepared without a surprising fallback.
        """
        app.state.limiter.check(user_id, "batch-download", 4)
        if is_hentai_series(payload.series_id):
            raise HTTPException(422, "Пакетная настройка доступна в каталоге Anime365.")
        if not store.has_watchlist(user_id, payload.series_id):
            raise HTTPException(404, "Anime is not in your library.")
        selected_ids = [item.episode_id for item in payload.items]
        if len(set(selected_ids)) != len(selected_ids):
            raise HTTPException(422, "Каждую серию можно настроить только один раз.")
        try:
            source, _token, upstream_series_id, _provider = source_for_series(payload.series_id)
            episodes = await source.episodes(upstream_series_id)
        except APIError as exc:
            _api_error(exc)
        by_episode = {int(row.get("id", 0)): row for row in episodes}
        jobs, skipped = [], []
        for item in payload.items:
            episode = by_episode.get(item.episode_id)
            episode_number = str((episode or {}).get("episodeFull") or
                                 (episode or {}).get("episodeInt") or item.episode_id)
            if episode is None:
                skipped.append(episode_number)
                continue
            try:
                translations = await source.translations(item.episode_id)
            except APIError:
                # One deleted or temporarily unavailable release must not make
                # the other explicitly configured episodes disappear.
                skipped.append(episode_number)
                continue
            if not any(int(row.get("id", 0)) == item.translation_id for row in translations):
                skipped.append(episode_number)
                continue
            job_id = secrets.token_urlsafe(24)
            job = store.create_download_job(
                job_id, user_id, payload.series_id, item.episode_id, episode_number,
                item.translation_id, item.quality, payload.delivery)
            if job is not None:
                app.state.downloads.enqueue(job_id)
                jobs.append(job)
        return {"requested": len(payload.items), "queued": len(jobs), "jobs": jobs,
                "skipped_episodes": skipped,
                "message": "Каждая серия будет скачана с выбранным для неё переводом и качеством."}

    @app.post("/api/travel")
    async def travel(payload: TravelRequest, user_id=Depends(authenticated_user)):
        """Queue a small, explicit batch using an existing translation preference.

        Translation IDs are episode-specific.  The selected anchor therefore
        supplies a kind/language/studio preference, which is resolved afresh for
        every episode in the batch rather than incorrectly reusing its ID.
        """
        app.state.limiter.check(user_id, "travel", 4)
        watch = store.get_watchlist(user_id, payload.series_id)
        if watch is None:
            raise HTTPException(404, "Anime is not in your library.")
        try:
            source, _token, upstream_series_id, _provider = source_for_series(payload.series_id)
            episodes = await source.episodes(upstream_series_id)
            anchor = next((row for row in episodes if int(row.get("id", 0)) == payload.anchor_episode_id), None)
            anchor_rows = await source.translations(payload.anchor_episode_id) if anchor else ()
        except APIError as exc:
            _api_error(exc)
        selected = next((row for row in anchor_rows if int(row.get("id", 0)) == payload.translation_id), None)
        if anchor is None or selected is None:
            raise HTTPException(422, "Серия или перевод больше недоступны.")
        profile = _translation_profile(selected)
        anchor_index = next(index for index, row in enumerate(episodes)
                            if int(row.get("id", 0)) == payload.anchor_episode_id)
        # Start with the selected episode when it is not watched yet. This lets a
        # person prepare the episode they are currently on plus following ones;
        # a completed anchor is naturally skipped.
        unseen = [row for row in episodes[anchor_index:] if not _episode_payload(
            row, watch.get("last_watched_episode_id"), watch.get("last_watched_episode_number"))["watched"]]
        requested = len(unseen) if payload.all_available else payload.count
        chosen = unseen[:min(requested, TRAVEL_BATCH_LIMIT)]
        jobs, skipped = [], []
        for episode in chosen:
            try:
                rows = anchor_rows if int(episode["id"]) == payload.anchor_episode_id \
                    else await source.translations(int(episode["id"]))
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

    @app.get("/api/posters/{ticket}")
    async def poster(ticket: str, user_id=Depends(authenticated_user)):
        """Serve a small Hentai365 cover from the Mini App origin.

        The ticket is short-lived, bound to its Telegram owner and created only
        from a validated ``/posters/`` URL.  This avoids both arbitrary fetches
        and WebView/RKN failures for cross-origin cover requests.
        """
        item = app.state.poster_tickets.get(ticket, user_id)
        if item is None or not is_hentai_poster_url(item.url):
            raise HTTPException(404, "Обложка больше недоступна.")
        client = httpx.AsyncClient(transport=app.state.proxy_transport, follow_redirects=False,
                                   timeout=httpx.Timeout(15, read=30))
        try:
            upstream = await client.send(client.build_request(
                "GET", item.url, headers={"Accept": "image/avif,image/webp,image/*"}), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(502, "Не удалось получить обложку.") from None
        content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        try:
            content_length = int(upstream.headers.get("content-length", "0") or 0)
        except ValueError:
            content_length = 0
        if upstream.status_code != 200 or not content_type.startswith("image/") \
                or content_length > MAX_POSTER_BYTES:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(502, "Не удалось получить обложку.")

        async def body():
            received = 0
            try:
                async for chunk in upstream.aiter_bytes(64 * 1024):
                    received += len(chunk)
                    if received > MAX_POSTER_BYTES:
                        break
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        return StreamingResponse(body(), media_type=content_type,
                                 headers={"Cache-Control": "private, max-age=300"})

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

        # A direct HLS URL is preferred.  When a WebView needs the fallback,
        # rewrite only the small manifest: variants, segments and key URLs stay
        # behind owner-bound tickets while video bytes are never buffered here.
        if not request.headers.get("range") and _is_hls(item.url, upstream.headers.get("content-type")):
            try:
                playlist = await _read_hls_playlist(upstream)
                rewritten = _rewrite_hls_playlist(playlist, item.url, user_id, app.state.tickets)
            except ValueError:
                await upstream.aclose()
                await client.aclose()
                raise HTTPException(502, "Не удалось подготовить HLS-поток Anime365.") from None
            await upstream.aclose()
            await client.aclose()
            return Response(content=rewritten, media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})

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
