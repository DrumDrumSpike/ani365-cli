"""Ephemeral media download and stream-copy MKV assembly."""
import asyncio
import gzip
import io
import logging
import os
import re
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


MAX_TELEGRAM_FILE = 1_990_000_000
MAX_SUBTITLE_FILE = 64 * 1024 * 1024
LOG = logging.getLogger(__name__)
SUBTITLE_SUFFIXES = {".ass", ".ssa", ".srt", ".vtt"}


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


def _unpack_subtitle(data, content_encoding):
    """Return a bounded subtitle payload and a safe compression label."""
    compression = "none"
    try:
        if data.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                files = [item for item in archive.infolist()
                         if not item.is_dir() and Path(item.filename).suffix.lower() in SUBTITLE_SUFFIXES]
                if not files:
                    raise MediaError("Архив не содержит поддерживаемых субтитров.")
                item = files[0]
                if item.file_size > MAX_SUBTITLE_FILE:
                    raise MediaError("Файл субтитров оказался слишком большим.")
                with archive.open(item) as source:
                    data = source.read(MAX_SUBTITLE_FILE + 1)
                compression = "zip"
        elif data.startswith(b"\x1f\x8b") or "gzip" in content_encoding.lower():
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as source:
                data = source.read(MAX_SUBTITLE_FILE + 1)
            compression = "gzip"
    except (gzip.BadGzipFile, EOFError, OSError, zipfile.BadZipFile, RuntimeError):
        raise MediaError("Anime365 вернул повреждённый архив субтитров.") from None
    if len(data) > MAX_SUBTITLE_FILE:
        raise MediaError("Файл субтитров оказался слишком большим.")
    return data, compression


def _normalize_subtitle(data, content_type="", content_encoding=""):
    """Unpack, decode and identify the common text subtitle formats."""
    data, compression = _unpack_subtitle(data, content_encoding)
    encoding = "utf-8"
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        candidates = ("utf-32",)
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",)
    else:
        candidates = ("utf-8-sig", "cp1251")
    text = None
    for candidate in candidates:
        try:
            text = data.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if text is None or "\x00" in text:
        raise MediaError("Скачанный файл субтитров имеет неизвестную кодировку.")

    stripped = text.lstrip("\ufeff \t\r\n")
    lowered = stripped[:4096].lower()
    if "[script info]" in lowered and "[events]" in text.lower():
        suffix, subtitle_format = ".ass", "ass"
    elif stripped.upper().startswith("WEBVTT"):
        suffix, subtitle_format = ".vtt", "webvtt"
    elif re.search(r"(?m)^\s*\d+\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s+-->", stripped):
        suffix, subtitle_format = ".srt", "srt"
    else:
        content_name = content_type.split(";", 1)[0].strip().lower() or "unknown"
        magic = data[:8].hex() or "empty"
        LOG.warning("Subtitle format is unknown (content-type=%s, bytes=%s, magic=%s, "
                    "compression=%s)", content_name, len(data), magic, compression)
        if ("html" in content_name or "json" in content_name
                or lowered.startswith(("<!doctype html", "<html", "{", "[{"))):
            raise MediaError("Anime365 вернул служебную страницу вместо субтитров.")
        raise MediaError("Скачанный файл субтитров имеет неизвестный формат.")

    LOG.info("Subtitle normalized (format=%s, encoding=%s, compression=%s, bytes=%s)",
             subtitle_format, encoding, compression, len(data))
    return text.encode("utf-8"), suffix


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
        # Local Bot API resolves a file URI in its own container. Keep the on-disk
        # basename ASCII-only so URI decoding cannot change the filesystem path.
        value = f"{title} {episode} {quality}p"
        parts = re.findall(r"[A-Za-z0-9]+", value)
        suffix = "-".join(parts[-10:]).lower()
        return f"anime-{suffix or str(quality) + 'p'}.mkv"

    @staticmethod
    def _download_subtitle(url, directory):
        if not _safe_url(url):
            raise MediaError("Anime365 вернул некорректную ссылку на субтитры.")
        request = Request(url, headers={"User-Agent": "ani365-bot/0.1"})
        try:
            with urlopen(request, timeout=60) as response:
                chunks = []
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_SUBTITLE_FILE:
                        raise MediaError("Файл субтитров оказался слишком большим.")
                    chunks.append(chunk)
                if total == 0:
                    raise MediaError("Anime365 вернул пустой файл субтитров.")
                content_type = str(response.headers.get("Content-Type", "")).lower()
                content_encoding = str(response.headers.get("Content-Encoding", ""))
            normalized, suffix = _normalize_subtitle(
                b"".join(chunks), content_type, content_encoding)
            path = directory / f"subtitles{suffix}"
            path.write_bytes(normalized)
            return path
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
                subtitle = await asyncio.to_thread(
                    self._download_subtitle, source.subtitle_url, directory)
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
