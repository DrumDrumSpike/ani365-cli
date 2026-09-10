import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from ani365_bot.api import MediaSource
from ani365_bot.config import Config
from ani365_bot.downloads import DownloadManager
from ani365_bot.store import Store


class FakeMedia:
    def __init__(self, root, gate):
        self.root, self.gate = Path(root), gate
        self.started = 0
        self.all_started = asyncio.Event()

    def cleanup_stale(self, age=0):
        return None

    @staticmethod
    def filename(title, episode, quality):
        return f"episode-{quality}.mkv"

    @asynccontextmanager
    async def prepare(self, source, filename, require_subtitle, language):
        self.started += 1
        if self.started >= 2:
            self.all_started.set()
        directory = self.root / f"working-{self.started}"
        directory.mkdir(parents=True)
        output = directory / filename
        output.write_bytes(b"mkv")
        await self.gate.wait()
        try:
            yield output
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)


class DownloadManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = Store(root / "data")
        self.config = Config("123:token", 1, data_dir=root / "data", media_dir=root / "jobs",
                             download_workers=2, download_ttl=60)
        self.store.add_watchlist(1, 10, "Title")
        self.store.save_token(1, "encrypted-token")
        self.gate = asyncio.Event()
        self.media = FakeMedia(root / "jobs", self.gate)
        self.anime = type("Anime", (), {})()
        self.anime.media_source = AsyncMock(return_value=MediaSource(("https://cdn.example/video",), None))
        self.anime.translations = AsyncMock(return_value=[{"id": 20, "type": "raw"}])
        self.manager = DownloadManager(self.config, self.store, self.anime, media=self.media)

    async def asyncTearDown(self):
        await self.manager.stop()
        self.store.close()
        self.temp.cleanup()

    def job(self, job_id):
        return self.store.create_download_job(job_id, 1, 10, 11, "1", 20, 720, "browser")

    async def test_worker_limit_and_ready_file_ttl_metadata(self):
        for job_id in ("a" * 16, "b" * 16, "c" * 16):
            self.job(job_id)
            self.manager.enqueue(job_id)
        await asyncio.wait_for(self.media.all_started.wait(), 1)
        self.assertEqual(self.media.started, 2)
        self.gate.set()
        await asyncio.gather(*list(self.manager._tasks.values()), return_exceptions=True)
        self.assertEqual(self.media.started, 3)
        self.assertEqual([job["status"] for job in self.store.list_download_jobs(1)],
                         ["ready", "ready", "ready"])
        self.assertTrue(all(job["expires_at"] for job in self.store.list_download_jobs(1)))

    async def test_cancel_queued_job_never_starts(self):
        self.manager._semaphore = asyncio.Semaphore(1)
        first, second = "d" * 16, "e" * 16
        self.job(first)
        self.job(second)
        self.manager.enqueue(first)
        self.manager.enqueue(second)
        for _ in range(20):
            if self.media.started:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.media.started, 1)
        self.assertTrue(await self.manager.cancel(1, second))
        self.gate.set()
        await asyncio.gather(*list(self.manager._tasks.values()), return_exceptions=True)
        self.assertEqual(self.media.started, 1)
        self.assertEqual(self.store.download_job(1, second)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
