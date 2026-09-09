import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ani365_bot.api import MediaSource
from ani365_bot.media import MediaError, MediaProcessor


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_muxes_subtitle_and_always_removes_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            processor = MediaProcessor(temporary)
            calls = []

            async def run(*args):
                calls.append(args)
                if args[0] == "yt-dlp":
                    output = Path(args[args.index("-o") + 1].replace("%(ext)s", "mp4"))
                    output.write_bytes(b"video")
                    return str(output)
                output = Path(args[-1])
                output.write_bytes(b"mkv")
                return ""

            def subtitle(url, path):
                path.write_text("[Script Info]\n")

            async def to_thread(function, *args):
                return function(*args)

            source = MediaSource(("https://cdn.example/video.m3u8",), "https://cdn.example/sub.ass")
            with patch("ani365_bot.media._run", side_effect=run), \
                    patch("ani365_bot.media.asyncio.to_thread", side_effect=to_thread), \
                    patch.object(processor, "_download_subtitle", side_effect=subtitle):
                with self.assertRaises(RuntimeError):
                    async with processor.prepare(source, "episode.mkv", True, "ru") as result:
                        self.assertTrue(result.exists())
                        job = result.parent
                        ffmpeg = calls[-1]
                        self.assertIn("-c:s", ffmpeg)
                        self.assertIn("language=ru", ffmpeg)
                        raise RuntimeError("simulated upload failure")
            self.assertFalse(job.exists())

    async def test_missing_required_subtitle_cleans_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            processor = MediaProcessor(temporary)

            async def run(*args):
                output = Path(args[args.index("-o") + 1].replace("%(ext)s", "mp4"))
                output.write_bytes(b"video")
                return str(output)

            with patch("ani365_bot.media._run", side_effect=run):
                with self.assertRaises(MediaError):
                    async with processor.prepare(MediaSource(("https://example.org/v",), None),
                                                 "episode.mkv", True, "ru"):
                        pass
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_stale_crash_directory_is_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            stale = Path(temporary) / "job-stale"
            stale.mkdir()
            old = time.time() - 7200
            stale.touch()
            import os
            os.utime(stale, (old, old))
            MediaProcessor(temporary).cleanup_stale()
            self.assertFalse(stale.exists())

    def test_filename_is_safe_for_unicode_filesystems(self):
        name = MediaProcessor.filename("Очень длинное название 🎬" * 30, "tv · 12", 1080)
        self.assertLessEqual(len(name.encode("utf-8")), 224)
        self.assertTrue(name.endswith(".mkv"))
        self.assertNotIn("/", name)


if __name__ == "__main__":
    unittest.main()
