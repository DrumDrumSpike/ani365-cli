"""Durable, shared polling of Anime365 watch-list updates.

The watcher deliberately knows nothing about Telegram markup or Anime365 tokens.
``episodes`` and ``translations`` are public Anime365 endpoints, so one request can
serve every subscribed user.  The Store owns the durable baseline, candidate and
outbox state; the injected sender turns a ready outbox row into a Telegram message.

Store contract used here (all methods are synchronous SQLite operations):

* ``unique_notification_series() -> iterable[int]``
* ``observe_episodes(series_id, episodes)`` records the shared episode snapshot
  and creates candidates only for subscriptions that are past their baseline.
* ``notification_candidates(series_id, now=None) -> iterable[Mapping]`` returns
  unresolved rows with ``user_id``, ``series_id``, ``episode_id`` and ``mode``.
* ``queue_notification(user_id, series_id, episode_id, mode)`` atomically turns a
  matching candidate into a durable outbox row.
* ``pending_notifications(now=None) -> iterable[Mapping]`` returns due outbox
  rows; ``mark_notification_sent`` and ``retry_notification`` settle them.

``defer_notification_candidate(..., delay, now=None)`` is optional for stores
that do not yet keep a per-candidate retry time.  Its presence prevents a
permanently untranslated episode from causing a translations request on every
short polling cycle.
"""
import asyncio
import logging
import time
from collections import defaultdict

from .api import APIError
from .translations import viewing_type


LOG = logging.getLogger(__name__)

NOTIFICATION_ANY = "any"
NOTIFICATION_SUBTITLES = "subtitles"
NOTIFICATION_VOICE = "voice"
NOTIFICATION_MODES = frozenset((NOTIFICATION_ANY, NOTIFICATION_SUBTITLES, NOTIFICATION_VOICE))


def notification_mode(mode):
    """Return the canonical persisted mode, accepting the earlier short aliases."""
    value = str(mode or "").strip().lower()
    return {"sub_ru": NOTIFICATION_SUBTITLES,
            "voice_ru": NOTIFICATION_VOICE}.get(value, value)


def available_notification_modes(translations):
    """Derive Russian subtitle/voice availability from active translation metadata."""
    result = set()
    for item in translations:
        if not isinstance(item, dict):
            continue
        kind, language = viewing_type(item)
        if language != "ru":
            continue
        if kind == "sub":
            result.add(NOTIFICATION_SUBTITLES)
        elif kind == "voice":
            result.add(NOTIFICATION_VOICE)
    return result


def retry_delay(attempt, retry_after=0, ceiling=300):
    """Bounded retry delay suitable for remote API and Telegram failures."""
    try:
        requested = max(0, int(retry_after or 0))
    except (TypeError, ValueError):
        requested = 0
    try:
        number = max(1, int(attempt))
    except (TypeError, ValueError):
        number = 1
    return max(requested, min(ceiling, 5 * (2 ** min(number - 1, 6))))


class Watcher:
    """Check unique series IDs and deliver durable notification outbox rows.

    ``send_notification`` receives one row from ``Store.pending_notifications``.
    It must raise :class:`APIError` for a Telegram failure.  Keeping Telegram UI in
    Bot means this class cannot accidentally put signed media URLs or API payloads
    into persistence or logs.
    """

    def __init__(self, store, anime, send_notification, interval=600, *, is_allowed=None,
                 clock=time.time, sleep=asyncio.sleep, concurrency=2):
        self.store = store
        self.anime = anime
        self.send_notification = send_notification
        # The caller normally supplies ``lambda user_id:
        # store.is_allowed(user_id, config.owner_id)``.  Keeping the check here is
        # a second line of defence if an outbox row survived a revocation race.
        self.is_allowed = is_allowed or (lambda user_id: True)
        self.interval = max(1, int(interval))
        self.clock = clock
        self.sleep = sleep
        self.concurrency = max(1, int(concurrency))
        self._series_failures = {}
        self._series_retry_at = {}
        self._candidate_failures = {}
        self._delivery_failures = {}

    async def run(self):
        """Run until cancelled; cancellation is deliberately allowed to propagate."""
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except APIError as exc:
                # APIError intentionally has no response body, URL or credential.
                LOG.warning("Watch checker cycle failed (code=%s); retrying", exc.code)
                await self.sleep(retry_delay(1, exc.retry_after))
                continue
            except Exception as exc:
                # Do not let an individual background failure stop Telegram polling.
                LOG.error("Watch checker cycle failed (type=%s); retrying", type(exc).__name__)
                await self.sleep(retry_delay(1))
                continue
            await self.sleep(self._next_delay())

    def _next_delay(self):
        now = self.clock()
        retry_delays = [retry_at - now for retry_at in self._series_retry_at.values()
                        if retry_at > now]
        return max(1, min([self.interval, *retry_delays]))

    async def check_once(self):
        """Perform one shared observation pass, then independently drain the outbox."""
        now = self.clock()
        series_ids = tuple(dict.fromkeys(self.store.unique_notification_series()))
        if series_ids:
            semaphore = asyncio.Semaphore(self.concurrency)

            async def check(series_id):
                async with semaphore:
                    await self._check_series_safely(series_id, now)

            await asyncio.gather(*(check(series_id) for series_id in series_ids))
        await self._deliver_pending(now)

    async def _check_series_safely(self, series_id, now):
        if self._series_retry_at.get(series_id, 0) > now:
            return
        try:
            await self._check_series(series_id, now)
        except asyncio.CancelledError:
            raise
        except APIError as exc:
            self._record_series_failure(series_id, now, exc.retry_after, exc.code)
        except Exception as exc:
            self._record_series_failure(series_id, now, 0, None, type(exc).__name__)
        else:
            self._series_failures.pop(series_id, None)
            self._series_retry_at.pop(series_id, None)

    def _record_series_failure(self, series_id, now, retry_after=0, code=None, error_type=None):
        attempt = self._series_failures.get(series_id, 0) + 1
        self._series_failures[series_id] = attempt
        self._series_retry_at[series_id] = now + retry_delay(attempt, retry_after)
        if code is not None:
            LOG.warning("Watch check for one series failed (code=%s); retrying", code)
        else:
            LOG.error("Watch check for one series failed (type=%s); retrying", error_type)

    async def _check_series(self, series_id, now):
        # Exactly one episodes() request for this series in this check, irrespective
        # of how many users are watching it.
        episodes = await self.anime.episodes(series_id)
        self.store.observe_episodes(series_id, episodes, now=now)
        candidates = tuple(self.store.notification_candidates(series_id, now=now))
        if not candidates:
            return

        by_episode = defaultdict(list)
        for candidate in candidates:
            if not self.is_allowed(candidate["user_id"]):
                self._discard_candidate(candidate)
                continue
            mode = notification_mode(candidate.get("mode"))
            if mode not in NOTIFICATION_MODES:
                # Store validation should prevent this.  Defer defensively rather
                # than logging user data or allowing one corrupt row to halt a run.
                self._defer(candidate, now, attempt=1)
                continue
            by_episode[candidate.get("episode_id")].append((candidate, mode))

        for episode_id, rows in by_episode.items():
            any_rows = [candidate for candidate, mode in rows if mode == NOTIFICATION_ANY]
            for candidate in any_rows:
                self._queue(candidate, NOTIFICATION_ANY, now)

            translated = [(candidate, mode) for candidate, mode in rows
                          if mode != NOTIFICATION_ANY]
            if not translated:
                continue
            try:
                translations = await self.anime.translations(episode_id)
            except asyncio.CancelledError:
                raise
            except APIError as exc:
                for candidate, _ in translated:
                    self._defer(candidate, now, retry_after=exc.retry_after)
                LOG.warning("Watch translation check failed (code=%s); retrying", exc.code)
                continue
            available = available_notification_modes(translations)
            for candidate, mode in translated:
                if mode in available:
                    self._queue(candidate, mode, now)
                else:
                    self._defer(candidate, now)

    def _queue(self, candidate, mode, now):
        # The Store verifies current enabled/mode state as part of this atomic write.
        self.store.queue_notification(candidate["user_id"], candidate["series_id"],
                                      candidate["episode_id"], mode, now=now)
        # A false result means the atomic Store revalidation observed a disabled
        # or mode-changed subscription; either way this stale candidate is done.
        self._candidate_failures.pop(self._candidate_key(candidate), None)

    def _defer(self, candidate, now, attempt=None, retry_after=0):
        defer = getattr(self.store, "defer_notification_candidate", None)
        if defer is None:
            return
        key = self._candidate_key(candidate)
        try:
            persisted_attempts = max(0, int(candidate.get("attempts", 0) or 0))
        except (TypeError, ValueError):
            persisted_attempts = 0
        tries = attempt or max(persisted_attempts + 1, self._candidate_failures.get(key, 0) + 1)
        self._candidate_failures[key] = tries
        delay = retry_delay(tries, retry_after)
        deferred = defer(candidate["user_id"], candidate["series_id"], candidate["episode_id"],
                         notification_mode(candidate.get("mode")), delay, now=now)
        if not deferred:
            # A concurrent mode change/revocation removed the candidate.
            self._candidate_failures.pop(key, None)

    def _discard_candidate(self, candidate):
        """Best-effort cleanup for a subscription revoked after candidate creation."""
        discard = getattr(self.store, "discard_notification_candidate", None)
        if discard is not None:
            discard(candidate["user_id"], candidate["series_id"], candidate["episode_id"],
                    notification_mode(candidate.get("mode")))
        self._candidate_failures.pop(self._candidate_key(candidate), None)

    async def _deliver_pending(self, now):
        for notification in tuple(self.store.pending_notifications(now=now)):
            key = self._notification_key(notification)
            if not self.is_allowed(notification["user_id"]):
                self._discard_notification(*key)
                continue
            try:
                await self.send_notification(notification)
            except asyncio.CancelledError:
                raise
            except APIError as exc:
                attempt = self._delivery_failures.get(key, 0) + 1
                self._delivery_failures[key] = attempt
                self.store.retry_notification(*key, retry_delay(attempt, exc.retry_after), now=now)
                LOG.warning("Watch notification delivery failed (code=%s); retrying", exc.code)
            except Exception as exc:
                attempt = self._delivery_failures.get(key, 0) + 1
                self._delivery_failures[key] = attempt
                self.store.retry_notification(*key, retry_delay(attempt), now=now)
                LOG.error("Watch notification delivery failed (type=%s); retrying", type(exc).__name__)
            else:
                self.store.mark_notification_sent(*key, now=now)
                self._delivery_failures.pop(key, None)

    def _discard_notification(self, user_id, series_id, episode_id, mode):
        discard = getattr(self.store, "discard_notification", None)
        if discard is not None:
            discard(user_id, series_id, episode_id, mode)

    @staticmethod
    def _notification_key(notification):
        return (notification["user_id"], notification["series_id"],
                notification["episode_id"], notification_mode(notification["mode"]))

    _candidate_key = _notification_key
