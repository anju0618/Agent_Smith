"""Environment and provider configuration loading for Agent Smith.

Centralizes .env loading and the registry of known LLM providers so the rest
of the codebase never touches os.environ directly. This is where the General
Rules requirement ("no hardcoded API keys, everything from environment
variables / .env files") is actually enforced architecturally.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv

_ENV_LOADED = False


def load_env() -> None:
    """Load .env once (idempotent). Never overrides variables already set in the shell."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path, override=False)
    _ENV_LOADED = True


load_env()


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of one LLM provider (Section 5.6 - multi-provider support)."""

    name: str
    base_url: str
    api_key_env_prefix: str
    kind: str = "openai_compatible"  # or "gemini"

    def collect_api_keys(self) -> List[str]:
        """Collect every API key configured for this provider.

        Supports multi-token management (Section 5.6.1): OPENROUTER_API_KEY,
        OPENROUTER_API_KEY_2, OPENROUTER_API_KEY_3, ... are all picked up so the
        LLM client can rotate between them when one hits a rate limit.
        """
        keys = []
        primary = os.environ.get(self.api_key_env_prefix)
        if primary:
            keys.append(primary)
        index = 2
        while True:
            value = os.environ.get(f"{self.api_key_env_prefix}_{index}")
            if not value:
                break
            keys.append(value)
            index += 1
        return keys


# Known free-tier providers (Section 5.6.1 - illustrative and non-exhaustive).
# Add more ProviderSpec entries here to support additional providers.
KNOWN_PROVIDERS: List[ProviderSpec] = [
    ProviderSpec("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openai_compatible"),
    ProviderSpec("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai_compatible"),
    ProviderSpec("together", "https://api.together.xyz/v1", "TOGETHER_API_KEY", "openai_compatible"),
    ProviderSpec(
        "fireworks", "https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", "openai_compatible"
    ),
    ProviderSpec(
        "google_ai_studio",
        "https://generativelanguage.googleapis.com/v1beta",
        "GOOGLE_AI_STUDIO_API_KEY",
        "gemini",
    ),
]


def _env_var_from_url(base_url: str) -> str:
    """Best-effort guess of a <HOST>_API_KEY env var name for an unlisted provider."""
    host = re.sub(r"^https?://", "", base_url).split("/")[0]
    host = re.sub(r"[^a-zA-Z0-9]+", "_", host).strip("_").upper()
    return f"{host}_API_KEY"


def resolve_provider(base_url: str) -> ProviderSpec:
    """Match a --provider-url against the known registry, or synthesize a generic one.

    Keeps the system usable with "other providers ... as long as your system
    complies with the project requirements" (Section 5.6): any OpenAI-compatible
    base URL works out of the box, as long as its key is exported under the
    conventional <HOST>_API_KEY environment variable.
    """
    normalized = base_url.rstrip("/")
    for spec in KNOWN_PROVIDERS:
        spec_url = spec.base_url.rstrip("/")
        if normalized == spec_url or normalized.startswith(spec_url):
            return spec
    return ProviderSpec(
        name=normalized,
        base_url=base_url,
        api_key_env_prefix=_env_var_from_url(base_url),
        kind="openai_compatible",
    )
