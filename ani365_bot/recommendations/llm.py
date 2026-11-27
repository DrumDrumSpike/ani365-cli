"""Server-side ChatGPT and Grok calls. Keys never leave this process."""
from __future__ import annotations

import httpx


PICK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "picks": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "reason"],
            },
        },
    },
    "required": ["picks"],
}

_MESSAGES = {
    "openai": {
        "missing_key": "Ключ OpenAI не настроен на сервере.",
        "unauthorized": "Ключ ChatGPT отклонён. Проверьте OPENAI_API_KEY на сервере.",
        "rate_limited": "У ChatGPT закончился лимит или баланс. Попробуйте позже.",
        "timeout": "ChatGPT не ответил вовремя.",
        "unavailable": "ChatGPT временно недоступен.",
        "invalid_response": "ChatGPT вернул неподходящий ответ.",
        "interrupted": "Подбор ChatGPT прервался при перезапуске. Повторный запрос не отправлялся.",
        "daily_limit": "На сегодня лимит подборок ChatGPT исчерпан.",
    },
    "xai": {
        "missing_key": "Ключ xAI не настроен на сервере.",
        "unauthorized": "Ключ Grok отклонён. Проверьте XAI_API_KEY на сервере.",
        "rate_limited": "У Grok закончился лимит или баланс. Попробуйте позже.",
        "timeout": "Grok не ответил вовремя.",
        "unavailable": "Grok временно недоступен.",
        "invalid_response": "Grok вернул неподходящий ответ.",
        "interrupted": "Подбор Grok прервался при перезапуске. Повторный запрос не отправлялся.",
        "daily_limit": "На сегодня лимит подборок Grok исчерпан.",
    },
}


class LLMError(Exception):
    """Safe Russian diagnostic. It never includes a key, prompt, or response body."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def user_message(source, code):
    table = _MESSAGES.get(source) or _MESSAGES["openai"]
    return table.get(code) or table["unavailable"]


class ChatCompletionsClient:
    """OpenAI-compatible chat completions with a strict JSON schema."""

    def __init__(self, *, source, api_key, model, url, reasoning_effort="none", timeout=45, transport=None):
        self.source = source
        self.api_key = api_key or ""
        self.model = model
        self.url = url
        self.reasoning_effort = reasoning_effort or ""
        self.timeout = timeout
        self.transport = transport

    def configured(self):
        return bool(self.api_key)

    async def complete(self, system, user):
        if not self.api_key:
            raise LLMError("missing_key", user_message(self.source, "missing_key"))
        body = {
            "model": self.model,
            "temperature": 0.2,
            "max_completion_tokens": 500,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "anime_picks", "strict": True, "schema": PICK_SCHEMA},
            },
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("timeout", user_message(self.source, "timeout")) from None
        except httpx.HTTPError:
            raise LLMError("unavailable", user_message(self.source, "unavailable")) from None
        self._raise_for_status(response)
        return self._content(response)

    def _raise_for_status(self, response):
        status = response.status_code
        if status < 400:
            return
        error_code = ""
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                error_code = str(error.get("code") or error.get("type") or "").casefold()
        except (TypeError, ValueError):
            error_code = ""
        if status in (401, 403):
            raise LLMError("unauthorized", user_message(self.source, "unauthorized"))
        if status in (402, 429) or any(token in error_code for token in ("insufficient", "quota", "billing", "credit")):
            raise LLMError("rate_limited", user_message(self.source, "rate_limited"))
        raise LLMError("unavailable", user_message(self.source, "unavailable"))

    def _content(self, response):
        try:
            payload = response.json()
        except ValueError:
            raise LLMError("invalid_response", user_message(self.source, "invalid_response")) from None
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        if not isinstance(message, dict) or message.get("refusal"):
            raise LLMError("invalid_response", user_message(self.source, "invalid_response"))
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise LLMError("invalid_response", user_message(self.source, "invalid_response"))
        return content
