from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration loaded at startup.

    Attributes:
        message: The greeting text served on ``GET /``.
        source:  Where the message came from — ``"configmap"`` when loaded
                 from the ``MESSAGE`` env var, or ``"fallback"`` for local dev.
    """

    message: str
    source: str


def parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    """Load application config from the environment.

    Resolution order:
    1. ``MESSAGE`` env var (set by ConfigMap ``envFrom``).
    2. Hard-coded fallback if ``ALLOW_MESSAGE_FALLBACK=true`` (local dev only).
    3. Raises :class:`ConfigError` — prevents silent startup with no message.
    """
    values = env if env is not None else os.environ

    message = values.get("MESSAGE")
    if message:
        return AppConfig(message=message, source="configmap")

    if parse_bool(values.get("ALLOW_MESSAGE_FALLBACK")):
        return AppConfig(message="hello from local dev", source="fallback")

    raise ConfigError(
        "MESSAGE is not set. Provide it via ConfigMap or set "
        "ALLOW_MESSAGE_FALLBACK=true for local development."
    )
