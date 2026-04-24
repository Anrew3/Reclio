"""Backwards-compat shim. The real implementation moved to
`app.services.llm` when multi-provider support (Ollama / Claude /
OpenAI / none) was added.

Existing imports keep working — `get_ollama()` now returns the
configured `LLMService`, which is not necessarily Ollama. New code
should prefer `from app.services.llm import get_llm`.
"""

from __future__ import annotations

from app.services.llm import LLMService, get_llm, sanitize_for_prompt

__all__ = ["get_ollama", "get_llm", "LLMService", "sanitize_for_prompt"]


def get_ollama() -> LLMService:
    """Deprecated alias — returns whichever LLM provider is configured."""
    return get_llm()
