"""Loads API keys from the environment (.env, never hardcoded — instant fail if hardcoded)."""
import os

from dotenv import load_dotenv

load_dotenv()

_PROVIDER_KEY_ENV_VARS = {
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "together": "TOGETHER_API_KEY",
    "google": "GOOGLE_AI_STUDIO_API_KEY",
}


def get_api_key(provider: str) -> str:
    """Returns the API key for `provider` (see _PROVIDER_KEY_ENV_VARS), or raises if unset."""
    env_var = _PROVIDER_KEY_ENV_VARS[provider]
    key = os.environ.get(env_var)
    if not key:
        raise RuntimeError(f"{env_var} is not set. Copy .env.example to .env and fill it in.")
    return key
