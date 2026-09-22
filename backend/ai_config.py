"""AI provider configuration (Phase 9 increment 1).

Reads configuration from the environment only - never a hard-coded key,
never a key or example secret written to a tracked file, a log line, a
fixture, or a response body. `python-dotenv` (already a dependency) is
relied on only insofar as `main.py`/a developer's shell may load a local,
gitignored `.env` before this module reads `os.environ`; this module itself
never reads or writes any file.

Configuration is read lazily, inside `resolve_ai_config()`, never at import
time - importing this module (or any module that imports it) must never
require `OPENAI_API_KEY` to be set, so the test suite can run entirely
against a scripted fake model without ever touching a real provider or
needing a real key.
"""

import os

DEFAULT_MODEL = "gpt-4o-mini"


class AIConfigurationError(RuntimeError):
    """AI configuration is missing or invalid. Maps to HTTP 503 - a known,
    expected "not set up yet" condition, never a generic 500."""


class AIConfig:
    """Resolved, immutable configuration for one call to the model provider."""

    __slots__ = ("api_key", "model")

    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model

    def __repr__(self):  # pragma: no cover - convenience only
        # Never includes the key itself, even partially - this is the one
        # place a careless `%r`/log call elsewhere could otherwise leak it.
        return f"AIConfig(model={self.model!r})"


def resolve_ai_config():
    """Read and validate AI configuration from the environment.

    Raises `AIConfigurationError` if `OPENAI_API_KEY` is not set or is
    whitespace-only - a clear, immediate failure rather than a confusing
    provider-side authentication error later. `OPENAI_MODEL` is optional;
    `DEFAULT_MODEL` is used when it is unset or blank.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AIConfigurationError(
            "AI is not configured: the OPENAI_API_KEY environment variable is "
            "not set. Set it (for example in a local, gitignored .env file) "
            "before sending a message to the scheduling agent."
        )
    model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    return AIConfig(api_key=api_key, model=model)
