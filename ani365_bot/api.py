import asyncio
from urllib.parse import urljoin

from .http import NetworkError


class APIError(Exception):
    """Safe to display: never contains a response body, URL or credential."""

    def __init__(self, message, code=0, retry_after=0, missing=False):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.missing = missing


class Telegram:
    def __init__(self, client, token):
        self.client = client
        self.base = f"https://api.telegram.org/bot{token}/"

    async def call(self, method, **params):
        try:
            response = await self.client.post(self.base + method, json=params, timeout=40)
            data = response.json()
        except (NetworkError, ValueError):
            raise APIError("Telegram временно недоступен.") from None
        if not isinstance(data, dict):
            raise APIError("Telegram вернул неизвестный ответ.")
        if not data.get("ok"):
            code = data.get("error_code", response.status_code)
            description = str(data.get("description", "")).lower()
            retry = data.get("parameters", {}).get("retry_after", 0)
            raise APIError("Не удалось выполнить запрос к Telegram.", code, retry,
                           missing="message to delete not found" in description)
        return data.get("result")


def title(series):
    titles = series.get("titles") or {}
    return str(titles.get("ru") or titles.get("romaji") or titles.get("en") or
               next(iter(titles.values()), None) or "Без названия")


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def subtitle_translation(item):
    kind = str(item.get("typeKind") or "").lower()
    raw_type = str(item.get("type") or "").lower()
    return kind in ("sub", "subtitles") or (not kind and raw_type.startswith("sub"))


def qualities(data):
    """Compatibility with the embed variants already handled by the CLI.

    Return only quality labels: signed media URLs must not enter sessions or DB.
    """
    if not isinstance(data, dict):
        raise APIError("Anime365 вернул неизвестный формат видео.")
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    streams = data.get("stream") or data.get("streams") or nested.get("stream") or nested.get("streams") or []
    if not isinstance(streams, list):
        raise APIError("Anime365 вернул неизвестный формат видео.")
    result = set()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        urls = stream.get("urls") or stream.get("urlList") or stream.get("url")
        if not urls:
            continue
        height = str(stream.get("height") or stream.get("quality") or "").removesuffix("p")
        if height.isdigit() and int(height) > 0:
            result.add(int(height))
    return sorted(result, reverse=True)


class Anime365:
    def __init__(self, client, base):
        self.client = client
        self.base = base.rstrip("/") + "/"

    async def get(self, path, token=None, **params):
        if token:
            params["access_token"] = token
        for attempt in range(3):
            try:
                # Redirects deliberately disabled: access_token must not leak to another host.
                response = await self.client.get(urljoin(self.base, path), params=params, timeout=20)
            except NetworkError:
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
                    continue
                raise APIError("Anime365 недоступен. Попробуй позже.") from None
            if response.status_code in (401, 403):
                raise APIError("Anime365 отклонил доступ. Проверь токен (/auth) и подписку.", response.status_code)
            if response.status_code == 404:
                raise APIError("На Anime365 этот материал больше не найден.", 404)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    await asyncio.sleep(attempt + 1)
                    continue
                raise APIError("Anime365 занят. Попробуй немного позже.", response.status_code)
            if not response.is_success:
                raise APIError("Не удалось получить данные Anime365.", response.status_code)
            try:
                payload = response.json()
                if not isinstance(payload, dict) or "data" not in payload:
                    raise ValueError
                return payload["data"]
            except ValueError:
                raise APIError("Anime365 вернул неизвестный ответ.") from None

    async def validate(self, token):
        data = await self.get("me", token)
        if not isinstance(data, dict) or data.get("isLogined") is not True:
            raise APIError("Токен Anime365 не прошёл проверку. Отправь другой токен.", 401)

    async def listing(self, path, **params):
        result = []
        # Paginate rather than silently cutting off long series/catalog results.
        for offset in range(0, 10000, 100):
            data = await self.get(path, limit=100, offset=offset, **params)
            if not isinstance(data, list) or any(not isinstance(x, dict) or "id" not in x for x in data):
                raise APIError("Anime365 вернул неизвестный формат списка.")
            result.extend(data)
            if len(data) < 100:
                return result
        raise APIError("Слишком много результатов. Уточни название.")

    async def search(self, query):
        rows = await self.listing("series", query=query, fields="id,titles,type,typeTitle,year")
        needle = query.casefold()

        def rank(row):
            names = [str(v).casefold() for v in (row.get("titles") or {}).values()]
            return (0 if needle in names else 1 if any(needle in name for name in names) else 2,
                    -number(row.get("year")))

        return sorted(rows, key=rank)

    async def episodes(self, series_id):
        rows = await self.listing("episodes", seriesId=series_id, isActive=1,
                                  fields="id,episodeFull,episodeInt,episodeTitle,episodeType")
        return sorted(rows, key=lambda x: (str(x.get("episodeType") or "tv"), number(x.get("episodeInt"))))

    async def translations(self, episode_id):
        rows = await self.listing("translations", episodeId=episode_id, isActive=1,
                                  fields="id,title,type,typeKind,typeLang,authorsSummary,priority,height")
        return sorted((x for x in rows if subtitle_translation(x)),
                      key=lambda x: (str(x.get("typeLang") or "").lower() != "ru", -number(x.get("priority"))))

    async def available_qualities(self, translation_id, token):
        return qualities(await self.get(f"translations/embed/{int(translation_id)}", token))
