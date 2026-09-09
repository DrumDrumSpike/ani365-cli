"""Ephemeral media download and stream-copy MKV assembly."""
import asyncio
import logging
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
LOG = logging.getLogger(__name__)


class MediaError(Exception):
    """Fixed, credential-free diagnostic safe for logs and chat."""

    code = 0


def _safe_url(value):
    parsed = urlsplit(value)
    return (parsed.scheme in ("http", "https") and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None)


def _safe_diagnostic(value):
    """Keep tool diagnostics useful without persisting signed media URLs."""
    value = re.sub(r"(?i)\b(?:https?|file)://[^\s'\"<>]+", "<url>", value)
    value = re.sub(r"/jobs/job-[^/\s:'\"]+/(input\.[a-zA-Z0-9]+|subtitles\.[a-zA-Z0-9]+)",
                   r"<media>/\1", value)
    value = re.sub(r"/jobs/job-[^\s:'\"]+", "<media>", value)
    value = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", value)
    return " | ".join(line.strip() for line in value.splitlines() if line.strip())[-2000:]


async def _run(*args):
    tool = Path(args[0]).name
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError:
        LOG.error("Media tool %s could not be started", tool)
        raise MediaError("На сервере не найдена программа для обработки видео.") from None
    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        diagnostic = _safe_diagnostic(stderr.decode(errors="replace"))
        if diagnostic:
            LOG.warning("Media tool %s failed (exit %s): %s", tool, process.returncode, diagnostic)
        else:
            LOG.warning("Media tool %s failed (exit %s) without diagnostics", tool, process.returncode)
        raise MediaError("Не удалось скачать или собрать видео. Попробуй другой перевод или качество.")
    return stdout.decode(errors="replace").strip()


async def _probe(path, label):
    try:
        await _run("ffprobe", "-v", "error", "-show_entries", "format=format_name",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path))
    except MediaError:
        LOG.warning("Downloaded %s is not recognized: %s", label, path.name)
        if label == "subtitle":
            raise MediaError("Скачанный файл субтитров имеет неизвестный формат.") from None
        raise MediaError("Скачанный видеофайл повреждён или имеет неизвестный формат.") from None


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
                if total == 0:
                    raise MediaError("Anime365 вернул пустой файл субтитров.")
                content_type = str(response.headers.get("Content-Type", "")).lower()
            prefix = path.read_bytes()[:256].lstrip().lower()
            if "text/html" in content_type or "application/json" in content_type \
                    or prefix.startswith((b"<!doctype html", b"<html", b"{")):
                LOG.warning("Subtitle URL returned non-subtitle content (%s, %s bytes)",
                            content_type.split(";", 1)[0] or "unknown", total)
                raise MediaError("Anime365 вернул служебную страницу вместо субтитров.")
        except MediaError:
            raise
        except HTTPError as exc:
            LOG.warning("Subtitle download failed (HTTP %s)", exc.code)
            raise MediaError("Не удалось скачать субтитры для выбранного перевода.") from None
        except (OSError, URLError) as exc:
            LOG.warning("Subtitle download failed (%s)", type(exc).__name__)
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
            await _probe(video, "video")
            if subtitle:
                await _probe(subtitle, "subtitle")
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
