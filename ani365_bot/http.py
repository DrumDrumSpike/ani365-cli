"""Small async JSON HTTP adapter for this sequential, single-user bot."""
import asyncio
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NetworkError(Exception):
    pass


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
