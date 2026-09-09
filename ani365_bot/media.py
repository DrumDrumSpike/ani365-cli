"""Ephemeral media download and stream-copy MKV assembly."""
import asyncio
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_TELEGRAM_FILE = 1_990_000_000
MAX_SUBTITLE_FILE = 64 * 1024 * 1024


class MediaError(Exception):
    """Fixed, credential-free diagnostic safe for logs and chat."""

    code = 0


def _safe_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None)


async def _run(*args):
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        raise MediaError("На сервере не найдена программа для обработки видео.") from None
    try:
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise MediaError("Не удалось скачать или собрать видео. Попробуй другой перевод или качество.")
    return stdout.decode(errors="replace").strip()


class MediaProcessor:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def cleanup_stale(self, age=3600):
        cutoff = time.time() - age
        for path in self.root.glob("job-*"):
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path)
            except OSError:
                pass

    @staticmethod
    def filename(title, episode, quality):
        value = re.sub(r"[\\/\x00-\x1f:*?\"<>|]+", " ", f"{title} — {episode} — {quality}p")
        value = re.sub(r"\s+", " ", value).strip(" .")
        encoded = value.encode("utf-8")
        if len(encoded) > 220:
            value = (encoded[:150].decode("utf-8", errors="ignore").rstrip() + "…" +
                     encoded[-65:].decode("utf-8", errors="ignore").lstrip())
        return (value or "anime") + ".mkv"

    @staticmethod
    def _download_subtitle(url, path):
        if not _safe_url(url):
            raise MediaError("Anime365 вернул некорректную ссылку на субтитры.")
        request = Request(url, headers={"User-Agent": "ani365-bot/0.1"})
        try:
            with urlopen(request, timeout=60) as response, path.open("wb") as target:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_SUBTITLE_FILE:
                        raise MediaError("Файл субтитров оказался слишком большим.")
                    target.write(chunk)
        except MediaError:
            raise
        except (OSError, HTTPError, URLError):
            raise MediaError("Не удалось скачать субтитры для выбранного перевода.") from None

    async def _download_video(self, urls, directory):
        for url in urls:
            if not _safe_url(url):
                continue
            for old in directory.glob("input.*"):
                old.unlink(missing_ok=True)
            try:
                output = await _run(
                    "yt-dlp", "--no-playlist", "--quiet", "--no-warnings",
                    "--retries", "3", "--fragment-retries", "10",
                    "--concurrent-fragments", "4", "--no-part", "--max-filesize", "1990M",
                    "--print", "after_move:filepath", "-o", str(directory / "input.%(ext)s"), url)
                candidate = Path(output.splitlines()[-1]) if output else None
                if candidate and candidate.is_file() and candidate.parent == directory:
                    return candidate
                files = list(directory.glob("input.*"))
                if len(files) == 1 and files[0].is_file():
                    return files[0]
            except MediaError:
                continue
        raise MediaError("Не удалось скачать видеопоток. Возможно, ссылка уже устарела.")

    @asynccontextmanager
    async def prepare(self, source, filename, require_subtitle, language):
        self.cleanup_stale()
        directory = self.root / f"job-{os.getpid()}-{time.time_ns()}"
        directory.mkdir(mode=0o755)
        try:
            video = await self._download_video(source.urls, directory)
            if video.stat().st_size >= MAX_TELEGRAM_FILE:
                raise MediaError("Файл превышает лимит Telegram 2000 МБ. Выбери качество ниже.")
            subtitle = None
            if require_subtitle:
                if not source.subtitle_url:
                    raise MediaError("Anime365 не вернул субтитры для выбранного перевода.")
                subtitle = directory / "subtitles.ass"
                await asyncio.to_thread(self._download_subtitle, source.subtitle_url, subtitle)
            output = directory / filename
            command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video)]
            if subtitle:
                command += ["-i", str(subtitle), "-map", "0:v?", "-map", "0:a?", "-map", "1:0",
                            "-c:v", "copy", "-c:a", "copy", "-c:s", "ass",
                            "-metadata:s:s:0", f"language={language or 'rus'}",
                            "-metadata:s:s:0", "title=Subtitles", "-disposition:s:0", "default"]
            else:
                command += ["-map", "0", "-c", "copy"]
            command.append(str(output))
            await _run(*command)
            if not output.is_file() or output.stat().st_size == 0:
                raise MediaError("Не удалось создать итоговый MKV.")
            if output.stat().st_size >= MAX_TELEGRAM_FILE:
                raise MediaError("Готовый файл превышает лимит Telegram 2000 МБ. Выбери качество ниже.")
            output.chmod(0o644)
            yield output
        finally:
            shutil.rmtree(directory, ignore_errors=True)
