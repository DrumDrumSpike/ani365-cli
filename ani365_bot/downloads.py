"""Bounded, cancellable offline preparation using the existing MediaProcessor."""
import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from .api import APIError, Telegram
from .http import HTTPClient
from .media import MediaError, MediaProcessor
from .translations import viewing_type


LOG = logging.getLogger(__name__)


class DownloadManager:
    def __init__(self, config, store, anime, media=None, telegram=None, hentai=None, source_for_series=None):
        self.config, self.store, self.anime = config, store, anime
        self.hentai = hentai
        self.source_for_series = source_for_series or (lambda _series_id: (anime, config.anime_token))
        self.media = media or MediaProcessor(config.media_dir)
        self.telegram = telegram or Telegram(HTTPClient(), config.bot_token, config.telegram_url)
        self._semaphore = asyncio.Semaphore(config.download_workers)
        self._tasks = {}

    async def start(self):
        self.store.reset_interrupted_downloads()
        self.media.cleanup_stale(age=0)
        await self.cleanup()
        for job_id in self.store.queued_download_job_ids():
            self.enqueue(job_id)

    async def stop(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def enqueue(self, job_id):
        if job_id in self._tasks:
            return
        task = asyncio.create_task(self._run(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))

    async def cancel(self, user_id, job_id):
        if not self.store.cancel_download_job(user_id, job_id):
            return False
        task = self._tasks.get(job_id)
        if task:
            task.cancel()
        self._remove_ready(job_id)
        return True

    def _ready_dir(self, job_id):
        return Path(self.config.media_dir) / f"ready-{job_id}"

    def _remove_ready(self, job_id):
        shutil.rmtree(self._ready_dir(job_id), ignore_errors=True)

    async def cleanup(self):
        for job_id, _filename in self.store.expire_download_jobs():
            self._remove_ready(job_id)

    async def _run(self, job_id):
        async with self._semaphore:
            job = self.store.claim_download_job(job_id)
            if not job:
                return
            try:
                source_client, token = self.source_for_series(job["series_id"])
                if not token:
                    raise APIError("Источник видео не настроен на сервере.")
                source = await source_client.media_source(job["translation_id"], job["quality"], token)
                translations = await source_client.translations(job["episode_id"])
                translation = next((row for row in translations
                                    if int(row.get("id", 0)) == job["translation_id"]), {})
                kind, language = viewing_type(translation)
                watch = self.store.get_watchlist(job["user_id"], job["series_id"])
                if not watch:
                    raise MediaError("Аниме больше нет в вашем списке.")
                filename = self.media.filename(watch["title"], f"tv {job['episode_number']}", job["quality"])
                async with self.media.prepare(source, filename, kind == "sub", language) as output:
                    current = self.store.download_job(job["user_id"], job_id)
                    if not current or current["status"] != "preparing":
                        return
                    if job["delivery"] == "telegram":
                        await self.telegram.call("sendDocument", chat_id=job["user_id"], document=output.as_uri(),
                                                 caption=f"{watch['title']} · серия {job['episode_number']} · {job['quality']}p")
                        self.store.update_progress(job["user_id"], job["series_id"], job["episode_id"],
                                                   job["episode_number"])
                        self.store.finish_download_job(job_id, "sent")
                    else:
                        ready = self._ready_dir(job_id)
                        ready.mkdir(mode=0o700, parents=True, exist_ok=True)
                        final = ready / filename
                        os.replace(output, final)
                        final.chmod(0o600)
                        self.store.finish_download_job(job_id, "ready", filename=filename,
                                                       expires_at=time.time() + self.config.download_ttl)
            except asyncio.CancelledError:
                # The Store state was changed by cancel(); MediaProcessor kills
                # yt-dlp/ffmpeg and removes its temporary directory on cancellation.
                raise
            except (APIError, MediaError) as exc:
                LOG.warning("Offline job failed (job=%s, category=%s)", job_id, type(exc).__name__)
                self.store.finish_download_job(job_id, "failed", error_code=type(exc).__name__)
            except Exception as exc:
                LOG.error("Offline job failed unexpectedly (job=%s, type=%s)", job_id, type(exc).__name__)
                self.store.finish_download_job(job_id, "failed", error_code="internal")
            finally:
                if self.store.download_job(job["user_id"], job_id) and \
                        self.store.download_job(job["user_id"], job_id)["status"] == "cancelled":
                    self._remove_ready(job_id)
