"""LLM abstraction: pick between Ollama (self-hosted), Claude, or OpenAI
behind one interface. Set `LLM_PROVIDER` in .env to switch at runtime —
no code changes required.

All providers implement a tiny common surface:

    await provider.generate(prompt, max_tokens=..., temperature=...)

Failure is signalled by returning None (never raising). That keeps
callsites like the feed builder — which need to degrade gracefully —
trivial.

Domain prompts (Because-You-Watched titles, section blurbs) live on
`LLMService`, which wraps any provider and adds caching + the
defang-before-interpolate sanitizer that blocks prompt injection via
Trakt/TMDB metadata.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.config import get_settings
from app.utils.cache import TTLCache

logger = logging.getLogger(__name__)


# ------------------------------ Sanitiser ------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def sanitize_for_prompt(value: Any, max_len: int = 120) -> str:
    """Defang user-controlled strings before interpolating into an LLM prompt.

    Collapses newlines/tabs/control-chars into single spaces, strips common
    quote characters, and truncates to max_len to bound the prompt.
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


# ------------------------------ Interface ------------------------------


class LLMProvider(ABC):
    """Minimal async text-generation interface. Never raises."""

    name: str = "base"

    @abstractmethod
    async def generate(
        self, prompt: str, *, max_tokens: int = 60, temperature: float = 0.7
    ) -> str | None:
        """Return completion text, or None if the provider is unavailable."""

    async def warmup(self) -> None:
        """Optional: pre-load models / open clients. Safe no-op default."""
        return None

    async def close(self) -> None:
        return None


# ------------------------------ Null provider -------------------------


class NullProvider(LLMProvider):
    """Used when llm_provider='none'. Returns None for every call so the
    callsite falls back to its non-LLM defaults. Cheap + deterministic."""

    name = "none"

    async def generate(
        self, prompt: str, *, max_tokens: int = 60, temperature: float = 0.7
    ) -> str | None:
        return None


# ------------------------------ Ollama --------------------------------


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.model = model or settings.ollama_model
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(15.0, connect=3.0),
            )
        return self._client

    async def generate(
        self, prompt: str, *, max_tokens: int = 60, temperature: float = 0.7
    ) -> str | None:
        try:
            client = await self._get_client()
            resp = await client.post(
                "/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
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

    async def warmup(self) -> None:
        """Pull the model on startup. Safe to call repeatedly."""
        try:
            client = await self._get_client()
            resp = await client.post(
                "/api/pull",
                json={"name": self.model, "stream": False},
                timeout=600.0,
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Ollama pull returned %s: %s", resp.status_code, resp.text[:200]
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ollama pull failed (non-fatal): %s", exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ------------------------------ Claude --------------------------------


class ClaudeProvider(LLMProvider):
    """Anthropic Messages API. Requires ANTHROPIC_API_KEY."""

    name = "claude"
    _API = "https://api.anthropic.com/v1/messages"
    _VERSION = "2023-06-01"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.anthropic_api_key
        self.model = model or settings.claude_model
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self._VERSION,
                    "content-type": "application/json",
                },
            )
        return self._client

    async def generate(
        self, prompt: str, *, max_tokens: int = 60, temperature: float = 0.7
    ) -> str | None:
        if not self.api_key:
            logger.debug("Claude: no ANTHROPIC_API_KEY set; returning None")
            return None
        try:
            client = await self._get_client()
            resp = await client.post(
                self._API,
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            blocks = data.get("content") or []
            for block in blocks:
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    return text or None
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("Claude generate failed: %s", exc)
            return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ------------------------------ OpenAI --------------------------------


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions. Requires OPENAI_API_KEY."""

    name = "openai"
    _API = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_model
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
            )
        return self._client

    async def generate(
        self, prompt: str, *, max_tokens: int = 60, temperature: float = 0.7
    ) -> str | None:
        if not self.api_key:
            logger.debug("OpenAI: no OPENAI_API_KEY set; returning None")
            return None
        try:
            client = await self._get_client()
            resp = await client.post(
                self._API,
                json={
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if choices:
                msg = (choices[0].get("message") or {}).get("content") or ""
                text = msg.strip()
                return text or None
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("OpenAI generate failed: %s", exc)
            return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ------------------------------ Service layer -------------------------


class LLMService:
    """Adds caching + domain-specific prompts on top of any Provider.

    The sanitizer runs on every user-sourced string before it hits a
    provider — keeps all three backends safe from prompt injection.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self._cache = TTLCache(default_ttl=24 * 60 * 60)  # stable per day

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def enabled(self) -> bool:
        return not isinstance(self.provider, NullProvider)

    async def warmup(self) -> None:
        await self.provider.warmup()

    async def close(self) -> None:
        await self.provider.close()

    async def generate_byw_title(self, watched_title: str, media_type: str) -> str:
        """'Because You Watched'-style Netflix section title.

        Always returns a usable string — falls back to a plain f-string
        if the provider is unreachable or returns garbage.
        """
        safe_title = sanitize_for_prompt(watched_title, max_len=80)
        safe_type = sanitize_for_prompt(media_type, max_len=20)
        fallback = f"Because You Watched {safe_title}" if safe_title else "Because You Watched"
        if not safe_title or not self.enabled:
            return fallback

        key = f"byw:{self.name}:{safe_type}:{safe_title}"
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
        result = await self.provider.generate(prompt, max_tokens=24)
        if not result:
            return fallback

        cleaned = result.split("\n", 1)[0].strip().strip('"').strip("'").strip()
        if len(cleaned) < 6 or len(cleaned) > 80:
            cleaned = fallback
        self._cache.set(key, cleaned)
        return cleaned

    async def generate_section_blurb(
        self, section_type: str, context: dict[str, Any]
    ) -> str | None:
        """Optional one-sentence explanation for a section. Returns None if
        the provider is unavailable — callers should handle None."""
        if not self.enabled:
            return None
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
        result = await self.provider.generate(prompt, max_tokens=40)
        if not result:
            return None
        return result.split("\n", 1)[0].strip().strip('"').strip("'")


# ------------------------------ Factory -------------------------------


_service: LLMService | None = None


def _build_provider() -> LLMProvider:
    settings = get_settings()
    provider = settings.llm_provider
    if provider == "none":
        return NullProvider()
    if provider == "claude":
        if not settings.anthropic_api_key:
            logger.warning(
                "LLM_PROVIDER=claude but ANTHROPIC_API_KEY is empty; "
                "falling back to NullProvider. Set the key or switch providers."
            )
            return NullProvider()
        return ClaudeProvider()
    if provider == "openai":
        if not settings.openai_api_key:
            logger.warning(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is empty; "
                "falling back to NullProvider."
            )
            return NullProvider()
        return OpenAIProvider()
    # default
    return OllamaProvider()


def get_llm() -> LLMService:
    """Process-wide LLMService singleton."""
    global _service
    if _service is None:
        _service = LLMService(_build_provider())
    return _service
