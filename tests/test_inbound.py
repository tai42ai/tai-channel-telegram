"""The inbound webhook door: verification, bounds, scoping, and forward policy."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.inbound import inbound
from tai42_channel_telegram.settings import TelegramSettings
from tests.conftest import make_inbound_request

_CALLBACK = "https://example.test/api/interactions/callback/tkt"
_VALID_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "s3cret_token"}


def _reply_update(
    chat_id: int = 777,
    replied_message_id: Any = 42,
    text: Any = "the blue one",
    username: str | None = None,
) -> dict[str, Any]:
    """A Telegram update carrying a ForceReply answer to a delivered question."""
    chat: dict[str, Any] = {"id": chat_id}
    if username is not None:
        chat["username"] = username
    message: dict[str, Any] = {
        "message_id": 1001,
        "chat": chat,
        "reply_to_message": {"message_id": replied_message_id},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": 5, "message": message}


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def test_route_metadata(stub_app):
    sys.modules.pop("tai42_channel_telegram.inbound", None)
    importlib.import_module("tai42_channel_telegram.inbound")
    routes = [r for r in stub_app.http.routes if r.path == "/api/channels/telegram/inbound"]
    assert routes
    route = routes[-1]
    assert route.methods == ["POST"]
    assert route.authed is False
    assert route.tags == ["channels"]
    assert route.summary == "Telegram channel inbound webhook"


async def test_valid_reply_forwards_answer_and_clears_mapping(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(http_recorder.requests) == 1
    forward = http_recorder.requests[0]
    assert str(forward.url) == _CALLBACK
    assert json.loads(forward.content) == {"answer": "the blue one"}
    assert fake_redis.data == {}


async def test_missing_wrong_and_wrong_length_secret_all_deny_identically(http_recorder, fake_redis):
    responses = [
        await inbound(make_inbound_request(_reply_update())),
        await inbound(make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-tok"})),
        # A different LENGTH must not short-circuit the compare: both sides are
        # sha256-hashed to 32 bytes before compare_digest.
        await inbound(
            make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret_token_longer"})
        ),
    ]
    assert [r.status_code for r in responses] == [401, 401, 401]
    assert len({bytes(r.body) for r in responses}) == 1
    assert http_recorder.requests == []


async def test_empty_env_secret_fails_closed(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_WEBHOOK_SECRET", "")
    reset_all_settings()
    response = await inbound(
        make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": ""}),
    )
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}
    assert http_recorder.requests == []


async def test_empty_configured_secret_never_verifies_even_matching(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # A set-but-empty SecretStr (constructed directly — the env layer drops
    # empty vars) must fail CLOSED even when the header matches it byte-for-byte.
    settings = TelegramSettings(webhook_secret=SecretStr(""))
    # Patch the handler's own globals: `inbound` here is the function object,
    # and a re-imported module elsewhere must not divert the patch target.
    monkeypatch.setitem(inbound.__globals__, "telegram_settings", lambda: settings)
    response = await inbound(
        make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": ""}),
    )
    assert response.status_code == 500
    assert http_recorder.requests == []


async def test_unset_secret_fails_closed(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_TELEGRAM_WEBHOOK_SECRET")
    reset_all_settings()
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}


async def test_no_recipients_configured_fails_closed(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    monkeypatch.delenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")
    reset_all_settings()
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}


async def test_reply_from_allowlisted_chat_forwards(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(_reply_update(chat_id=888), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(http_recorder.requests) == 1


async def test_reply_from_chat_allowlisted_only_by_username_forwards(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # The allowlist names the chat by @username alone; the update's numeric
    # chat id appears nowhere in the configuration, yet the reply matches on
    # "@" + chat.username and is forwarded.
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", "@ops_bot")
    reset_all_settings()
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    update = _reply_update(chat_id=424242, username="ops_bot")
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(http_recorder.requests) == 1
    assert json.loads(http_recorder.requests[0].content) == {"answer": "the blue one"}
    assert fake_redis.data == {}


async def test_oversized_body_413_bounded_while_streaming(http_recorder, fake_redis):
    request = make_inbound_request(
        headers=_VALID_HEADERS,
        chunks=[b"x" * 600_000, b"x" * 600_000, b"tail"],
    )
    response = await inbound(request)
    assert response.status_code == 413
    assert _body(response) == {"error": "payload too large"}
    # The cap fired mid-stream: the trailing chunk was never pulled.
    assert request.scope["_pending_body_messages"]
    assert http_recorder.requests == []


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2, 3]"])
async def test_non_object_body_400(http_recorder, fake_redis, raw: bytes):
    response = await inbound(make_inbound_request(raw=raw, headers=_VALID_HEADERS))
    assert response.status_code == 400
    assert _body(response) == {"error": "body must be a JSON object"}
    assert http_recorder.requests == []


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": 5},  # no message at all
        _reply_update(chat_id=778),  # chat not a recipient by id, no username
        _reply_update(chat_id=778, username="someone_else"),  # neither id nor @username listed
        {"update_id": 5, "message": {"message_id": 1, "chat": "778", "text": "hi"}},  # chat is not an object
        {"update_id": 5, "message": {"message_id": 1, "chat": {"id": 777}, "text": "hi"}},  # not a reply
        _reply_update(text=None),  # reply without text (e.g. a photo)
    ],
)
async def test_out_of_scope_updates_are_acked_and_ignored(http_recorder, fake_redis, update: dict[str, Any]):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert http_recorder.requests == []
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


async def test_unknown_correlation_is_acked_and_ignored(http_recorder, fake_redis):
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert http_recorder.requests == []


async def test_callback_404_is_terminal_mapping_dropped(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    http_recorder.responder = lambda request: httpx.Response(404)
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "stale"}}
    assert fake_redis.data == {}


async def test_callback_400_keeps_mapping_for_a_retyped_answer(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    http_recorder.responder = lambda request: httpx.Response(400)
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "rejected"}}
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


async def test_callback_500_raises_so_telegram_redelivers(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    http_recorder.responder = lambda request: httpx.Response(500)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


async def test_callback_transport_error_propagates(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("door unreachable")

    http_recorder.responder = responder
    with pytest.raises(httpx.ConnectError, match="door unreachable"):
        await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}
