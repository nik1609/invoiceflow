"""AI configuration, read from environment variables only (GROQ_API_KEY or ANTHROPIC_API_KEY, optional IF_LLM_MODEL)."""
from __future__ import annotations

import os

ENV = {"groq": "GROQ_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def key(provider: str) -> str | None:
    return os.getenv(ENV[provider]) or None


def model() -> str | None:
    return os.getenv("IF_LLM_MODEL") or None
