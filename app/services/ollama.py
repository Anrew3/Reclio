"""Ollama client — used for generating 'Because You Watched' titles and
optional section blurbs. Fails gracefully: if Ollama is unreachable,
returns a sensible f-string fallback instead of erroring.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.config import get_settings
from app.utils.cache import TTLCache

logger = logging.getLogger(__name__)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def sanitize_for_prompt(value: Any, max_len: int = 120) -> str:
    """Defang user-controlled strings before interpolating into an LLM prompt.

    - Collapses newlines/tabs/control-chars into single spaces so an attacker
      can't use `\\n\\n Ignore previous instructions…` to break out of context.
    - Strips common quote characters so they can't close a quoted segment.
    - Truncates to max_len to bound prompt size.
    """
    if value is None:
        return ""
    text = str(value)
    text = _CONTROL_CHARS.sub(" ", text)
    text = text.replace("`", "'").replace('"', "'").replace("\\", "/")
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


class OllamaService:
    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self._cache = TTLCache(default_ttl=24 * 60 * 60)  # stable per day
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(15.0, connect=3.0),
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _generate(self, prompt: str, max_tokens: int = 40) -> str | None:
        try:
            client = await self._get_client()
            resp = await client.post(
                "/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.7,
                        "num_predict": max_tokens,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("response") or "").strip()
            return text or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Ollama generate failed: %s", exc)
            return None

    async def generate_byw_title(self, watched_title: str, media_type: str) -> str:
        """Generate a 'Because You Watched' style section title.

        Returns the fallback f-string if Ollama is unavailable.
        """
        safe_title = sanitize_for_prompt(watched_title, max_len=80)
        safe_type = sanitize_for_prompt(media_type, max_len=20)
        fallback = f"Because You Watched {safe_title}" if safe_title else "Because You Watched"
        if not safe_title:
            return fallback

        key = f"byw:{safe_type}:{safe_title}"
        cached = self._cache.get(key)
        if cached:
            return cached

        prompt = (
            "You write short, catchy Netflix-style section titles.\n"
            f"The viewer just finished the {safe_type} '{safe_title}'.\n"
            "Write ONE short section title (max 8 words) suggesting more of the "
            "same vibe. Examples: 'Because You Watched Inception', "
            "'Since You Loved Breaking Bad', 'More Like Ozark'.\n"
            "Respond with ONLY the title text, no quotes, no explanation."
        )
        result = await self._generate(prompt, max_tokens=24)
        if not result:
            return fallback

        # Clean up common artifacts
        cleaned = result.split("\n", 1)[0].strip().strip('"').strip("'").strip()
        if len(cleaned) < 6 or len(cleaned) > 80:
            cleaned = fallback
        self._cache.set(key, cleaned)
        return cleaned

    async def generate_section_blurb(
        self, section_type: str, context: dict[str, Any]
    ) -> str | None:
        """Optional one-sentence explanation for a section.

        `context` values may originate from user data — sanitize each one
        before interpolating to mitigate prompt injection.
        """
        safe_type = sanitize_for_prompt(section_type, max_len=40)
        safe_ctx = ", ".join(
            f"{sanitize_for_prompt(k, max_len=30)}={sanitize_for_prompt(v, max_len=60)}"
            for k, v in context.items()
        )
        prompt = (
            "Write ONE short sentence (max 15 words) explaining why this "
            "recommendation section was shown to the viewer. Be casual and warm.\n"
            f"Section: {safe_type}\nContext: {safe_ctx}\n"
            "Respond with ONLY the sentence."
        )
        result = await self._generate(prompt, max_tokens=40)
        if not result:
            return None
        return result.split("\n", 1)[0].strip().strip('"').strip("'")

    async def ensure_model_pulled(self) -> None:
        """Pull the configured model if not already cached. Safe to call repeatedly."""
        try:
            client = await self._get_client()
            resp = await client.post("/api/pull", json={"name": self.model, "stream": False}, timeout=600.0)
            if resp.status_code >= 400:
                logger.warning("Ollama pull returned %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ollama pull failed (non-fatal): %s", exc)


_ollama: OllamaService | None = None


def get_ollama() -> OllamaService:
    global _ollama
    if _ollama is None:
        _ollama = OllamaService()
    return _ollama
