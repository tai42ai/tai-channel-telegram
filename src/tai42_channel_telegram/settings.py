"""Telegram channel settings.

Two settings groups read the ``CHANNEL_TELEGRAM_`` environment prefix:
:class:`TelegramSettings` (bot credential, recipient allowlist and default
recipient, webhook secret, public base URL, HTTP budget) and
:class:`TelegramCorrelationSettings` (the Redis
connection the correlation store lives in, e.g. ``CHANNEL_TELEGRAM_REDIS_URL``).
Both are exposed through ``@settings_cache`` accessors so a live-reload soft
restart drops the singletons with every other settings group.

Every credential is a ``SecretStr`` read from the environment only — the LLM
never sees a token, and ``repr``/logs/``model_dump`` show it masked. Fields
default ``None`` so importing the package never demands configuration; the
``require`` / ``require_secret`` helpers raise loudly, naming the missing env
var, wherever a value is actually needed.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import NoDecode, SettingsConfigDict
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.settings import TaiBaseSettings, settings_cache


class TelegramSettings(TaiBaseSettings):
    model_config = SettingsConfigDict(env_prefix="CHANNEL_TELEGRAM_")

    # The bot credential (from BotFather). SecretStr keeps it out of any repr,
    # log line, or traceback; the plaintext is read only when composing the
    # Bot API URL (the token IS the auth, embedded in the URL path).
    bot_token: SecretStr | None = None
    # The operator whitelist of chats a caller-supplied recipient may name.
    # Each entry is a chat address as ``sendMessage`` accepts it: a numeric
    # chat id as a string (negative for groups/supergroups) or an ``@username``.
    # The env value is a comma-separated string or a JSON list; ``NoDecode``
    # hands the raw string to the before-validator below, which parses both.
    # A caller-requested recipient not on this list is refused (fail closed);
    # the list gates ONLY caller-supplied values, never ``default_recipient``.
    allowed_recipients: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # The chat questions are delivered to when the caller names no recipient.
    # Operator configuration, implicitly trusted — it is not checked against
    # ``allowed_recipients``.
    default_recipient: str | None = None
    # The ``setWebhook`` secret_token (1-256 chars of [A-Za-z0-9_-]). Every
    # genuine Telegram delivery echoes it in X-Telegram-Bot-Api-Secret-Token;
    # the inbound door compares it constant-time and fails CLOSED when unset.
    webhook_secret: SecretStr | None = None
    # This deployment's public base URL. The startup hook points the bot's
    # webhook at {public_base_url}/api/channels/telegram/inbound.
    public_base_url: str | None = None
    # Bot API origin. Overridable so a stub server can stand in for Telegram
    # (the e2e harness records against a local endpoint); production never
    # changes it.
    api_base_url: str = "https://api.telegram.org"
    # Wall-clock budget for one outbound HTTP call (sendMessage / setWebhook /
    # the callback forward). Must be positive.
    http_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("allowed_recipients", mode="before")
    @classmethod
    def _parse_allowed_recipients(cls, value: object) -> object:
        """Parse the allowlist from either accepted shape — anything else
        raises loudly.

        A string bracketed like JSON is decoded as a JSON list (a malformed
        value raises, never a silent comma-split into garbage entries); any
        other string is split on commas. Every entry — from either string form
        or a list passed directly — must itself be a string; entries are
        stripped and empties dropped. Any other input shape raises — never a
        silently coerced whitelist.
        """
        if isinstance(value, str):
            stripped = value.strip()
            # JSON text opening with "[" parses to a list or raises loudly.
            value = json.loads(stripped) if stripped.startswith("[") else stripped.split(",")
        if not isinstance(value, list):
            raise ValueError("allowed_recipients must be a comma-separated string or a list of chat addresses")
        entries: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("allowed_recipients entries must be strings")
            entry = item.strip()
            if entry:
                entries.append(entry)
        return entries


class TelegramCorrelationSettings(RedisConnectionSettings):
    """The Redis connection the correlation store uses.

    Field names come from :class:`RedisConnectionSettings` under this prefix:
    ``CHANNEL_TELEGRAM_REDIS_URL``, ``CHANNEL_TELEGRAM_REDIS_MAX_CONNECTIONS``,
    ``CHANNEL_TELEGRAM_SOCKET_TIMEOUT``, ... — the plugin owns its own keys and
    connection, independent of the skeleton's interactions store.
    """

    model_config = SettingsConfigDict(env_prefix="CHANNEL_TELEGRAM_")


@settings_cache
def telegram_settings() -> TelegramSettings:
    return TelegramSettings()


@settings_cache
def telegram_correlation_settings() -> TelegramCorrelationSettings:
    return TelegramCorrelationSettings()


def require[T](value: T | None, env_name: str) -> T:
    """Return the configured value, or raise naming the missing env var.

    A channel named in the manifest but missing its configuration is an
    operator error that must surface loudly at the point of use — never a
    silent no-op delivery.
    """
    if value is None:
        raise ValueError(f"the telegram channel is not configured: set {env_name}")
    return value


def require_secret(value: SecretStr | None, env_name: str) -> str:
    """Return the secret's plaintext, or raise on unset/EMPTY — fail CLOSED.

    An empty secret is as dangerous as a missing one (an empty webhook secret
    would let any internet client forge "the human answered"), so both raise.
    """
    secret = require(value, env_name).get_secret_value()
    if not secret:
        raise ValueError(f"{env_name} is set but empty")
    return secret
