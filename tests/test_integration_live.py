"""Live integration tests against the real Telegram Bot API.

Run with ``pytest -m integration``. Credentials are read purely from the
``CHANNEL_TELEGRAM_BOT_TOKEN`` / ``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT``
environment; the
suite skips cleanly when either is unset. It never calls ``setWebhook`` — a
live bot may have a production webhook this suite must not repoint.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai_contract.channels import ChannelDelivery
from tai_kit.clients.impl.http import HttpxClient

from tai_channel_telegram import TelegramChannel, telegram_settings

pytestmark = pytest.mark.integration

_ENV_KEYS = ("CHANNEL_TELEGRAM_BOT_TOKEN", "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")


def _creds() -> dict[str, str]:
    creds = {key: os.environ.get(key, "") for key in _ENV_KEYS}
    if not all(creds.values()):
        pytest.skip("CHANNEL_TELEGRAM_* credentials not available")
    return creds


@pytest.fixture
async def real_http(stub_app):
    """A real-transport httpx client bound where the plugin expects its pool."""
    client = httpx.AsyncClient(trust_env=False)
    stub_app.clients.by_class[HttpxClient] = client
    try:
        yield client
    finally:
        stub_app.clients.by_class.pop(HttpxClient, None)
        await client.aclose()


async def test_get_me_authenticates(real_http):
    creds = _creds()
    settings = telegram_settings()
    response = await real_http.get(f"{settings.api_base_url}/bot{creds['CHANNEL_TELEGRAM_BOT_TOKEN']}/getMe")
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_deliver_sends_real_force_reply(real_http, fake_redis):
    _creds()
    delivery = ChannelDelivery(
        interaction_id="smoke-text",
        question="[tai-channel-telegram smoke] reply not required",
        answer_format="text",
        callback_url="https://example.org/smoke",
        timeout_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    await TelegramChannel().deliver(delivery)
    assert len(fake_redis.data) == 1
    assert all(ttl > 0 for ttl in fake_redis.ttls.values())


async def test_deliver_external_sends_url_button(real_http, fake_redis):
    _creds()
    delivery = ChannelDelivery(
        interaction_id="smoke-external",
        question="[tai-channel-telegram smoke] tap not required",
        answer_format="external",
        callback_url="https://example.org/smoke",
        timeout_at=datetime.now(UTC) + timedelta(seconds=120),
    )
    await TelegramChannel().deliver(delivery)
    assert fake_redis.data == {}
