from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from app.core.config import Settings


class AiGateway:
    """OpenAI-compatible gateway for Bedrock Mantle and the legacy provider."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_content: str,
        timeout_seconds: float = 30.0,
        expected_keys: tuple[str, ...] = (),
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        if not self._settings.ai_api_key:
            raise ValueError("AI API key is not configured")

        payload: dict[str, Any] = {
            "model": self._settings.ai_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        if self._settings.ai_model.startswith("openai.gpt-oss"):
            payload["max_completion_tokens"] = 8192
            payload["reasoning_effort"] = "low"
        else:
            payload["max_tokens"] = 4096
        # Both current providers implement the OpenAI-compatible JSON mode.
        # If a future Mantle model rejects it, the retry below uses prompt-only
        # JSON enforcement and validates the result locally.
        payload["response_format"] = {"type": "json_object"}
        url = f"{self._settings.ai_base_url.rstrip('/')}/chat/completions"
        timeout = httpx.Timeout(timeout_seconds, connect=10.0)
        last_error: Exception | None = None

        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            for attempt in range(max_attempts):
                try:
                    response = await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {self._settings.ai_api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    if response.status_code == 400 and "response_format" in payload:
                        payload.pop("response_format", None)
                        # Some Mantle models support strict JSON through the
                        # prompt but reject OpenAI's response_format option.
                        # Retry immediately without consuming the caller's
                        # network retry budget (including max_attempts=1).
                        response = await client.post(
                            url,
                            headers={
                                "Authorization": f"Bearer {self._settings.ai_api_key}",
                                "Content-Type": "application/json",
                            },
                            json=payload,
                        )
                    response.raise_for_status()
                    content = response.json()["choices"][0]["message"]["content"]
                    parsed = self._parse_json_object(content)
                    missing = [key for key in expected_keys if key not in parsed]
                    if missing:
                        raise ValueError(f"AI JSON is missing required keys: {missing}")
                    return parsed
                except (
                    httpx.RequestError,
                    httpx.HTTPStatusError,
                    KeyError,
                    IndexError,
                    ValueError,
                ) as exc:
                    last_error = exc
                    retryable = not isinstance(
                        exc, httpx.HTTPStatusError
                    ) or exc.response.status_code in {408, 429, 500, 502, 503, 504}
                    if attempt + 1 < max_attempts and retryable:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    break

        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_json_object(content: object) -> dict[str, Any]:
        if not isinstance(content, str):
            raise ValueError("AI response content is not text")
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as original_error:
            # Some OpenAI-compatible reasoning models occasionally wrap an
            # otherwise valid object as `{ "{...}`. Scan each object boundary
            # and accept only a complete decoded dictionary.
            decoder = json.JSONDecoder()
            candidates: list[dict[str, Any]] = []
            for match in re.finditer(r"\{", text):
                try:
                    candidate, _ = decoder.raw_decode(text[match.start() :])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    candidates.append(candidate)
            if not candidates:
                raise original_error
            parsed = max(
                candidates,
                key=lambda item: len(json.dumps(item, ensure_ascii=False)),
            )
        if not isinstance(parsed, dict):
            raise ValueError("AI response JSON is not an object")
        return parsed
