"""The AI layer — the Myth Engine and a swappable LLM provider seam.

This is the *rare, high-value* tier of the game's brain (see the tick-vs-AI
split in the README). It is never called per tick — only at decision points
and for content, here: turning a player's logged deeds into a **distorted
legend** when their works are dug up generations later.

Providers are swappable behind one interface so the same code runs against:
  - DeepSeek (the cloud default; great quality, scales, works on the GPU-less VPS),
  - a local Ollama model (free, offline dev iteration on the workstation),
  - a deterministic stub (no key / offline / tests).
"""
from __future__ import annotations

from typing import Any

import httpx

from .settings import SETTINGS


# --------------------------------------------------------------------------
# Provider interface + implementations
# --------------------------------------------------------------------------
class LLMProvider:
    name = "base"

    async def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class StubProvider(LLMProvider):
    """Used when no AI is configured. Deterministic, offline, free."""

    name = "stub"

    async def complete(self, system: str, user: str) -> str:
        return (
            "The elders half-remember a founder whose name the wind has worn "
            "away — said to have raised the old stones with bare hands and "
            "spoken with the frost. Few details survive, and fewer are true."
        )


class DeepSeekProvider(LLMProvider):
    """DeepSeek's OpenAI-compatible chat completions endpoint."""

    name = "deepseek"

    def __init__(self, api_key: str, model: str, base_url: str):
        self._key = api_key
        self._model = model
        self._url = base_url.rstrip("/") + "/chat/completions"

    async def complete(self, system: str, user: str) -> str:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 1.3,  # high: we *want* colorful, varied distortion
            "max_tokens": 220,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._key}"},
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()


class OllamaProvider(LLMProvider):
    """Local Ollama model — the free, offline dev/escape hatch."""

    name = "ollama"

    def __init__(self, model: str, base_url: str):
        self._model = model
        self._url = base_url.rstrip("/") + "/api/chat"

    async def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "stream": False,
            "options": {"temperature": 1.2},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(self._url, json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"].strip()


def make_provider() -> LLMProvider:
    choice = SETTINGS.ai_provider.lower()
    if choice == "auto":
        choice = "deepseek" if SETTINGS.deepseek_api_key else "stub"
    if choice == "deepseek" and SETTINGS.deepseek_api_key:
        return DeepSeekProvider(
            SETTINGS.deepseek_api_key,
            SETTINGS.deepseek_model,
            SETTINGS.deepseek_base_url,
        )
    if choice == "ollama":
        return OllamaProvider(SETTINGS.ollama_model, SETTINGS.ollama_base_url)
    return StubProvider()


# --------------------------------------------------------------------------
# The Myth Engine
# --------------------------------------------------------------------------
_SYSTEM = (
    "You are the Historian, the keeper of oral tradition for an ancient "
    "people. You take the true deeds of a long-dead figure and retell them "
    "as your people would after countless generations: a LEGEND — exaggerated, "
    "mythologized, partly wrong, details garbled and grown grander with each "
    "telling. Reply with ONLY the legend itself: 2-3 vivid sentences, no "
    "preamble, no quotation marks."
)


def _deeds_summary(deeds: list[dict]) -> str:
    if not deeds:
        return "Little is known of what they truly did."
    counts: dict[str, int] = {}
    for ev in deeds:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
    parts = []
    if counts.get("build"):
        parts.append(f"raised {counts['build']} structure(s)")
    if counts.get("gather"):
        parts.append(f"gathered resources {counts['gather']} times")
    if counts.get("spawn"):
        parts.append("appeared in the world as a wanderer")
    return "; ".join(parts) or "lived an unremarkable life by the record."


class MythEngine:
    """Generates (and the caller caches) the legend for an excavated ruin."""

    def __init__(self, provider: LLMProvider):
        self.provider = provider

    async def generate(
        self,
        *,
        builder: str,
        structure_type: str,
        era_built: str,
        deeds: list[dict],
    ) -> str:
        user = (
            f"The figure was known as {builder}, who lived in the "
            f"{era_built.title()} Age. The true record of their deeds: "
            f"{_deeds_summary(deeds)}. Their most notable work, a "
            f"{structure_type.replace('_', ' ')}, has just been unearthed as a "
            f"ruin. Tell the legend your people now whisper about them."
        )
        try:
            text = await self.provider.complete(_SYSTEM, user)
            return text or "The legend has faded beyond recall."
        except Exception as exc:  # never let an AI hiccup break a dig
            return (
                f"The legend of {builder} is lost to a storm of ages "
                f"(the Historian fell silent: {type(exc).__name__})."
            )
