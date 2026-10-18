"""Configurable Jev decisions API client (weibo tool edition).

Behavior mirrors tools/jev/jev_client.py: 429/529 retried with exponential backoff
up to 3 times, timeouts retried, 401/422 fail fast with readable messages.
Differences: endpoint, model and key come from a config dict instead of hardcoded
constants, and the OpenRouter endpoint path follows the app's convention
(openrouter base -> /alpha/decisions, others -> /v1/systemone).
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

MAX_RETRIES = 3


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def judge_url(base_url: str) -> str:
    """Derive the judge endpoint from a config base_url.

    OpenRouter preset (https://openrouter.ai/api) -> /alpha/decisions,
    TypeSafe-compatible presets (bocha/typesafe/vercel/zen) -> /v1/systemone.
    """
    base = base_url.rstrip("/")
    if "openrouter" in base:
        return base + "/alpha/decisions"
    return base + "/v1/systemone"


def replies_url(base_url: str) -> str:
    """Derive the chat-completions endpoint from a config base_url."""
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def redact_secrets(text: str, secrets: list[str]) -> str:
    """Strip configured keys/cookies from any string before print or disk write."""
    if not isinstance(text, str):
        text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class JevJudge:
    """Calls the Jev decisions endpoint with a fixed state + questions payload."""

    def __init__(self, base_url: str, model: str, api_key: str,
                 secrets: list[str] | None = None, timeout: float = 20) -> None:
        self.url = judge_url(base_url)
        self.model = model
        self.key = (api_key or "").strip()
        self.secrets = secrets or []
        self.timeout = timeout
        if not self.key:
            raise JevError(
                "no API key configured: set judge.api_key in tools/weibo/config.json "
                "or export OPENROUTER_API_KEY"
            )

    def _error_body(self, exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return redact_secrets(raw, self.secrets)[:800]

    def ask(self, state: dict, questions: dict) -> dict:
        """POST state+questions to Jev. Returns the parsed JSON body."""
        payload = json.dumps(
            {"model": self.model, "state": state, "questions": questions},
            ensure_ascii=False,
        ).encode("utf-8")

        last_status: int | None = None
        last_body = ""
        for attempt in range(MAX_RETRIES + 1):
            headers = {
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            }
            if "openrouter" in self.url:
                headers["HTTP-Referer"] = "https://jev-assistant.local"
                headers["X-Title"] = "Weibo Analyzer"
            req = urllib.request.Request(self.url, data=payload, method="POST",
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                last_body = self._error_body(exc)
                if last_status in (429, 529) and attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                readable = {
                    401: "Jev HTTP 401: API key rejected. Check config api_key.",
                    422: f"Jev HTTP 422: request body rejected. {last_body}",
                    429: f"Jev HTTP 429: rate limited after {MAX_RETRIES} retries. {last_body}",
                    529: f"Jev HTTP 529: provider overloaded after {MAX_RETRIES} retries. {last_body}",
                }.get(last_status, f"Jev HTTP {last_status}: {last_body}")
                raise JevError(readable, last_status) from None
            except (TimeoutError, socket.timeout) as exc:
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise JevError(f"Jev request timed out after {self.timeout}s") from exc
            except urllib.error.URLError as exc:
                reason = redact_secrets(getattr(exc, "reason", exc), self.secrets)
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
                raise JevError(f"Jev request failed: {reason}") from None

        raise JevError(f"Jev HTTP {last_status}: exhausted retries. {last_body}",
                       last_status)