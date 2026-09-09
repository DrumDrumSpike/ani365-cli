import gzip
import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ani365_bot.api import MediaSource
from ani365_bot.media import MediaError, MediaProcessor, _normalize_subtitle, _run, _safe_diagnostic


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_tool_logs_stage_and_code_but_scrubs_signed_url(self):
        process = AsyncMock()
        process.returncode = 7
        process.communicate.return_value = (
            b"", b"download failed for https://cdn.example/video?token=very-secret\nsecond line")
        with patch("ani365_bot.media.asyncio.create_subprocess_exec", return_value=process):
            with self.assertLogs("ani365_bot.media", level="WARNING") as logs:
                with self.assertRaises(MediaError):
                    await _run("yt-dlp", "https://cdn.example/video?token=very-secret")
        message = " ".join(logs.output)
        self.assertIn("yt-dlp failed (exit 7)", message)
        self.assertIn("<url>", message)
        self.assertNotIn("very-secret", message)

    def test_diagnostic_keeps_input_basename_without_job_path(self):
        value = _safe_diagnostic("/jobs/job-12-34/subtitles.ass: Invalid data")
        self.assertEqual(value, "<media>/subtitles.ass: Invalid data")

    def test_normalizes_gzip_utf16_ass(self):
        source = "[Script Info]\r\nTitle: Тест\r\n[Events]\r\n"
        data, suffix = _normalize_subtitle(gzip.compress(source.encode("utf-16")))
        self.assertEqual(suffix, ".ass")
        self.assertEqual(data.decode(), source)

    def test_normalizes_cp1251_srt(self):
        source = "1\r\n00:00:01,000 --> 00:00:02,000\r\nПривет\r\n"
        data, suffix = _normalize_subtitle(source.encode("cp1251"))
        self.assertEqual(suffix, ".srt")
        self.assertIn("Привет", data.decode())

    def test_extracts_webvtt_from_zip(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr("episode.vtt", "WEBVTT\n\n00:01.000 --> 00:02.000\nText\n")
        data, suffix = _normalize_subtitle(archive.getvalue())
        self.assertEqual(suffix, ".vtt")
        self.assertTrue(data.startswith(b"WEBVTT"))

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
                if args[0] == "ffprobe":
                    return "matroska"
                output = Path(args[-1])
                output.write_bytes(b"mkv")
                return ""

            def subtitle(url, directory):
                path = directory / "subtitles.ass"
                path.write_text("[Script Info]\n")
                return path

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
                if args[0] == "ffprobe":
                    return "mov,mp4"
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

    def test_stale_cleanup_keeps_an_active_concurrent_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            processor = MediaProcessor(temporary)
            active = Path(temporary) / "job-active"
            active.mkdir()
            old = time.time() - 7200
            import os
            os.utime(active, (old, old))
            processor._active_jobs.add(active)
            processor.cleanup_stale()
            self.assertTrue(active.exists())
            processor._active_jobs.remove(active)
            processor.cleanup_stale()
            self.assertFalse(active.exists())

    def test_filename_is_safe_for_unicode_filesystems(self):
        name = MediaProcessor.filename("Очень длинное название 🎬" * 30, "tv · 12", 1080)
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith(".mkv"))
        self.assertTrue(name.isascii())
        self.assertIn("tv-12-1080p", name)


if __name__ == "__main__":
    unittest.main()
