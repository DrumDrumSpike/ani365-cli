"""Background recommendation jobs. One source cannot block or erase the other."""
from __future__ import annotations

import asyncio
import logging
import time

from .candidates import select_candidates, user_franchises
from .catalog import CatalogError
from .continuations import find_continuations
from .llm import LLMError, user_message
from .parser import ParseError, parse_picks
from .prompt import SYSTEM_PROMPT, build_user_prompt
from .resolve import resolve_candidates
from .taste import taste_profile, top_genres


LOG = logging.getLogger(__name__)
SOURCES = ("openai", "xai")
SOURCE_LABELS = {"openai": "ChatGPT", "xai": "Grok"}
TASTE_METADATA_LIMIT = 250
CONTINUATION_SOURCE_LIMIT = 80
RESOLVE_POOL_LIMIT = 80
CONTINUATION_STALE_SECONDS = 12 * 60 * 60
WATCHED_STATUSES = frozenset({"completed", "watching", "rewatching"})


class RecommendationRequestError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class RecommendationService:
    def __init__(self, store, anime, catalog, clients, *, daily_limit=5, candidate_limit=36,
                 anime_url="https://smotret-anime.app/api", refresh_rates=None, now=None):
        self.store = store
        self.anime = anime
        self.catalog = catalog
        self.clients = clients
        self.daily_limit = max(1, int(daily_limit))
        self.candidate_limit = max(1, min(40, int(candidate_limit)))
        self.anime_url = anime_url
        self.refresh_rates = refresh_rates
        self._now = now or time.time

    def request(self, user_id, source):
        """Queue one job, or return the job already queued or running for this source."""
        self._source(source)
        if not self.store.external_account_status(user_id, "shikimori")["connected"]:
            raise RecommendationRequestError("Сначала подключите Shikimori.", 409)
        active = self.store.active_recommendation_job(user_id, source)
        if active:
            return active, False
        used = self.store.recommendation_api_calls_since(user_id, source, self._now() - 24 * 60 * 60)
        if used >= self.daily_limit:
            raise RecommendationRequestError(user_message(source, "daily_limit"), 429)
        return self.store.enqueue_recommendation_job(user_id, source, now=self._now())

    def recover(self):
        """Resume jobs that never called an API. Fail the rest without a second call."""
        resumed = []
        for job in self.store.unfinished_recommendation_jobs():
            if job["api_called"]:
                self.store.fail_recommendation_job(
                    job["id"], "interrupted", user_message(job["source"], "interrupted"), now=self._now())
            else:
                if job["state"] == "running":
                    self.store.requeue_recommendation_job(job["id"])
                resumed.append(job["id"])
        return resumed

    def continuations_stale(self, user_id):
        updated = self.store.recommendation_continuations_updated_at(user_id)
        return updated is None or self._now() - updated >= CONTINUATION_STALE_SECONDS

    async def execute(self, job_id):
        job = self.store.recommendation_job(job_id)
        if not job or job["state"] not in {"queued", "running"}:
            return
        if job["api_called"]:
            self.store.fail_recommendation_job(
                job_id, "interrupted", user_message(job["source"], "interrupted"), now=self._now())
            return
        if not self.store.mark_recommendation_job_running(job_id, now=self._now()):
            return
        source, user_id = job["source"], job["user_id"]
        try:
            client = self.clients[source]
            if not client.configured():
                self.store.fail_recommendation_job(
                    job_id, "missing_key", user_message(source, "missing_key"), now=self._now())
                return
            await self._refresh_rates(user_id)
            if not self.store.external_account_status(user_id, "shikimori")["connected"]:
                self.store.fail_recommendation_job(
                    job_id, "not_connected", "Сначала подключите Shikimori.", now=self._now())
                return
            rates = self.store.external_user_rates(user_id, "shikimori", limit=5000)
            profile, metadata = await self._profile(rates)
            try:
                await self._save_continuations(user_id, rates, metadata, load_related=True)
            except (CatalogError, OSError):
                LOG.info("Continuation refresh skipped (user=%s)", user_id)
            candidates = await self._candidates(user_id, rates, metadata, profile)
            if not candidates:
                self.store.complete_recommendation_job(job_id, user_id, source, [], now=self._now())
                return
            prompt = build_user_prompt(
                profile, self.store.recent_recommendation_feedback(user_id, limit=5),
                sorted((value for value in self.store.recommended_shikimori_ids(user_id)
                        if str(value).isdigit()), key=int), candidates)
            raw = await self._complete(job_id, client, prompt)
            try:
                picks = parse_picks(raw, [item["id"] for item in candidates])
            except ParseError:
                raw = await self._complete(job_id, client, prompt, retry=True)
                picks = parse_picks(raw, [item["id"] for item in candidates])
            by_id = {item["id"]: item for item in candidates}
            self.store.complete_recommendation_job(
                job_id, user_id, source, [{**by_id[pick["id"]], "reason": pick["reason"]} for pick in picks],
                now=self._now())
        except asyncio.CancelledError:
            raise
        except ParseError:
            self.store.fail_recommendation_job(
                job_id, "invalid_response", user_message(source, "invalid_response"), now=self._now())
        except LLMError as exc:
            self.store.fail_recommendation_job(job_id, exc.code, str(exc), now=self._now())
        except CatalogError:
            self.store.fail_recommendation_job(
                job_id, "shikimori_unavailable",
                "Shikimori временно недоступен. Прошлая подборка сохранена.", now=self._now())
        except Exception:
            LOG.exception("Recommendation job failed (user=%s source=%s)", user_id, source)
            called = bool((self.store.recommendation_job(job_id) or {}).get("api_called"))
            if called:
                code, message = "unavailable", user_message(source, "unavailable") + " Прошлая подборка сохранена."
            else:
                code, message = "profile", "Не удалось подготовить профиль. Прошлая подборка сохранена."
            self.store.fail_recommendation_job(job_id, code, message, now=self._now())

    async def refresh_continuations(self, user_id):
        if not self.store.external_account_status(user_id, "shikimori")["connected"]:
            return
        rates = self.store.external_user_rates(user_id, "shikimori", limit=5000)
        watched = [rate["external_anime_id"] for rate in rates if rate["status"] in WATCHED_STATUSES]
        metadata = {}
        if watched:
            metadata = await self.catalog.metadata(watched[:CONTINUATION_SOURCE_LIMIT], related=True)
        await self._save_continuations(user_id, rates, metadata, load_related=False)

    async def _refresh_rates(self, user_id):
        if self.refresh_rates is None:
            return
        try:
            await self.refresh_rates(user_id)
        except Exception:
            LOG.info("Recommendation rate refresh skipped (user=%s)", user_id)

    async def _profile(self, rates):
        identifiers = [rate["external_anime_id"] for rate in rates if rate["status"] in
                       {"completed", "watching", "rewatching", "on_hold", "dropped", "planned"}]
        metadata = await self.catalog.metadata(identifiers[:TASTE_METADATA_LIMIT], related=False) if identifiers else {}
        return taste_profile(rates, metadata), metadata

    async def _candidates(self, user_id, rates, metadata, profile):
        genres = top_genres(profile["genre_weights"], limit=4)
        if not genres:
            return []
        excluded = {str(rate.get("external_anime_id") or "").strip() for rate in rates
                    if isinstance(rate, dict)}
        excluded.discard("")
        franchises = user_franchises(rates, metadata)
        recommended = self.store.recommended_shikimori_ids(user_id)
        library = set((self.store.recommendation_profile(user_id) or {}).get("excluded_anime365_series_ids") or ())
        raw = await self.catalog.candidates(genres)
        narrowed = select_candidates(
            raw, excluded_shikimori_ids=excluded, excluded_series_ids=library,
            excluded_franchises=franchises, already_recommended_ids=recommended,
            limit=RESOLVE_POOL_LIMIT, require_resolved=False)
        resolved = await resolve_candidates(self.anime, narrowed, anime_url=self.anime_url, store=self.store)
        return select_candidates(
            resolved, excluded_shikimori_ids=excluded, excluded_series_ids=library,
            excluded_franchises=franchises, already_recommended_ids=recommended,
            limit=self.candidate_limit, require_resolved=True)

    async def _save_continuations(self, user_id, rates, metadata, *, load_related):
        if load_related:
            watched = [rate["external_anime_id"] for rate in rates if rate["status"] in WATCHED_STATUSES]
            if watched:
                metadata = {**metadata, **await self.catalog.metadata(watched[:CONTINUATION_SOURCE_LIMIT], related=True)}
        found = find_continuations(rates, metadata)
        resolved = await resolve_candidates(self.anime, [
            {"id": item["shikimori_anime_id"], "mal_id": item.get("mal_id"), "title": item["title"],
             "year": item.get("year"), "kind": item.get("kind")}
            for item in found
        ], anime_url=self.anime_url, store=self.store)
        by_id = {item["id"]: item for item in resolved}
        items = []
        for item in found:
            match = by_id.get(item["shikimori_anime_id"])
            if match:
                item = {**item, "anime365_series_id": match["anime365_series_id"],
                        "poster_url": match.get("poster_url"),
                        "title": match.get("title") or item["title"],
                        "year": match.get("year") or item.get("year")}
            items.append(item)
        self.store.replace_recommendation_continuations(user_id, items, now=self._now())

    async def _complete(self, job_id, client, prompt, *, retry=False):
        if retry:
            if not self.store.mark_recommendation_retry(job_id):
                raise ParseError("retry already used")
        elif not self.store.mark_recommendation_api_called(job_id):
            raise LLMError("interrupted", user_message(client.source, "interrupted"))
        return await client.complete(SYSTEM_PROMPT, prompt)

    @staticmethod
    def _source(source):
        if source not in SOURCES:
            raise RecommendationRequestError("Неизвестный источник рекомендаций.", 422)
        return source
