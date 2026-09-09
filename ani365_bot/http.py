"""Small async JSON HTTP adapter for this sequential, single-user bot."""
import asyncio
import http.client
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


LOG = logging.getLogger(__name__)


class NetworkError(Exception):
    def __init__(self, message, stage="request", retry_safe=False):
        super().__init__(message)
        self.stage = stage
        self.retry_safe = retry_safe


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Response:
    status_code: int
    body: bytes

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return json.loads(self.body)


class HTTPClient:
    def _request(self, method, url, params, payload, timeout):
        if params:
            url += "?" + urlencode(params)
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"User-Agent": "ani365-bot/0.1", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            try:
                response = build_opener(NoRedirects()).open(request, timeout=timeout)
            except HTTPError as exc:
                response = exc
            with response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise NetworkError("Response exceeds the size limit")
                return Response(response.code, body)
        except (OSError, URLError, ValueError):
            raise NetworkError("HTTP request failed") from None

    async def get(self, url, params=None, timeout=20):
        return await asyncio.to_thread(self._request, "GET", url, params, None, timeout)

    async def post(self, url, json=None, timeout=40):
        return await asyncio.to_thread(self._request, "POST", url, None, json, timeout)

    def _post_file(self, url, fields, file_field, file_path, timeout):
        """Stream multipart data without holding the media file in memory."""
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.username or parsed.password:
            raise NetworkError("Invalid upload URL")
        path = Path(file_path)
        try:
            size = path.stat().st_size
        except OSError:
            raise NetworkError("Upload file is unavailable") from None

        boundary = "ani365-" + secrets.token_hex(16)
        chunks = []
        for name, value in fields.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif isinstance(value, bool):
                value = "true" if value else "false"
            header = (f"--{boundary}\r\n"
                      f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n").encode()
            chunks.append(header + str(value).encode("utf-8") + b"\r\n")
        file_header = (f"--{boundary}\r\n"
                       f"Content-Disposition: form-data; name=\"{file_field}\"; "
                       f"filename=\"{path.name}\"\r\n"
                       "Content-Type: video/x-matroska\r\n\r\n").encode("ascii")
        ending = f"\r\n--{boundary}--\r\n".encode()
        content_length = sum(map(len, chunks)) + len(file_header) + size + len(ending)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection_type = (http.client.HTTPSConnection if parsed.scheme == "https"
                           else http.client.HTTPConnection)
        connection = None
        stage, sent = "connect", 0
        try:
            connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
            connection.putrequest("POST", target)
            connection.putheader("User-Agent", "ani365-bot/0.1")
            connection.putheader("Accept", "application/json")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()
            stage = "upload"
            for chunk in chunks:
                connection.send(chunk)
                sent += len(chunk)
            connection.send(file_header)
            sent += len(file_header)
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    connection.send(chunk)
                    sent += len(chunk)
            connection.send(ending)
            sent += len(ending)
            stage = "response"
            response = connection.getresponse()
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise NetworkError("Response exceeds the size limit")
            return Response(response.status, body)
        except (OSError, http.client.HTTPException) as exc:
            LOG.warning("Telegram file transport failed (stage=%s, type=%s, sent=%s, total=%s)",
                        stage, type(exc).__name__, sent, content_length)
            raise NetworkError("File upload failed", stage, stage != "response") from None
        finally:
            if connection:
                connection.close()

    async def post_file(self, url, fields, file_field, file_path, timeout=40):
        for attempt in range(2):
            try:
                return await asyncio.to_thread(
                    self._post_file, url, fields, file_field, file_path, timeout)
            except NetworkError as exc:
                if attempt == 0 and exc.retry_safe:
                    LOG.warning("Retrying Telegram file upload before acceptance (stage=%s)", exc.stage)
                    await asyncio.sleep(1)
                    continue
                raise
