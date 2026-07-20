"""The public inbound door Telegram's webhook POSTs updates to.

``POST /api/channels/telegram/inbound`` (``authed=False``): verify the
``X-Telegram-Bot-Api-Secret-Token`` header against the configured webhook
secret (constant-time over sha256 digests of both sides; FAIL CLOSED on
missing configuration), extract the
ForceReply answer (``message.reply_to_message.message_id`` + ``text``), look
up the question's callback_url in the correlation store, and forward
``{"answer": <typed text>}`` to the interaction callback door.

Telegram redelivers an update until it gets a 2xx, so every branch chooses its
status deliberately: verification failures deny (401/500 — redelivering an
unauthentic update has no value), out-of-scope and stale updates ack (200 —
redelivery cannot make them relevant, the reason is logged), and a transient
forward failure raises (500 — Telegram's redelivery is the visible recovery
path, never a swallowed answer).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai_contract.app import tai_app

from tai_channel_telegram.client import telegram_http
from tai_channel_telegram.correlation import clear_correlation, lookup_callback_url
from tai_channel_telegram.settings import telegram_settings

logger = logging.getLogger(__name__)

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# A Telegram text-message update is a few KiB; an unauthenticated door still
# bounds what it reads into memory — loud 413, never a truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class _PayloadTooLarge(Exception):
    """The inbound body exceeded ``_MAX_BODY_BYTES`` -> 413."""


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the request body on ACTUAL bytes, never a client ``Content-Length``.

    Raise ``_PayloadTooLarge`` the moment the accumulated stream crosses ``cap``
    — the oversized remainder is never read into memory; loud, never truncated.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _PayloadTooLarge("request body exceeds the configured cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _misconfigured(env_name: str) -> JSONResponse:
    logger.error("telegram inbound: %s is unset or empty; failing closed", env_name)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


def _denied() -> JSONResponse:
    # One constant deny for every verification failure — no missing-vs-wrong oracle.
    return JSONResponse({"error": "verification failed"}, status_code=401)


def _ignored(reason: str) -> JSONResponse:
    logger.info("telegram inbound: update ignored: %s", reason)
    return JSONResponse({"data": {"status": "ignored"}}, status_code=200)


@tai_app.http.custom_route(
    "/api/channels/telegram/inbound",
    methods=["POST"],
    summary="Telegram channel inbound webhook",
    tags=["channels"],
    response_model=None,
    authed=False,
)
async def inbound(request: Request) -> Response:
    """Receive a Telegram webhook update and bridge a ForceReply answer to the
    interaction callback door.

    Only replies (``reply_to_message``) carrying text, from a configured
    recipient chat (the default recipient or an allowlisted one, matched by
    numeric chat id or ``@username``), are answers;
    everything else is acknowledged and ignored. The answer
    is forwarded as the JSON object ``{"answer": "<typed text>"}`` — the
    callback door validates the value against the question's stored
    answer_format and enforces single-use/idempotency.
    """
    settings = telegram_settings()
    configured = settings.webhook_secret.get_secret_value() if settings.webhook_secret else ""
    if not configured:
        return _misconfigured("CHANNEL_TELEGRAM_WEBHOOK_SECRET")
    # The chats questions can be delivered to — replies from anywhere else are
    # never answers. An entry matches an inbound reply by either address form:
    # the string form of the update's numeric chat id, or ``@username`` when
    # the update's chat carries a username.
    recipient_chats = set(settings.allowed_recipients)
    if settings.default_recipient is not None:
        recipient_chats.add(settings.default_recipient)
    if not recipient_chats:
        return _misconfigured("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT / CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")

    provided = request.headers.get(_SECRET_HEADER)
    # Hash BOTH sides before the constant-time compare: compare_digest over
    # unequal-length raw inputs returns early, so a raw compare would leak the
    # secret's length. sha256 fixes both sides at 32 bytes.
    if provided is None or not hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(configured.encode()).digest(),
    ):
        return _denied()

    try:
        body = await _read_bounded_body(request, _MAX_BODY_BYTES)
    except _PayloadTooLarge:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        update = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    if not isinstance(update, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    message = update.get("message")
    if not isinstance(message, dict):
        return _ignored("update carries no message")
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return _ignored("message is not from a configured recipient chat")
    # A chat is a configured recipient when either of its addresses is listed:
    # the string form of its numeric id, or "@" + its username (Telegram sets
    # ``chat.username`` on the update when the chat has one).
    username = chat.get("username")
    if str(chat.get("id")) not in recipient_chats and not (
        isinstance(username, str) and f"@{username}" in recipient_chats
    ):
        return _ignored("message is not from a configured recipient chat")
    reply_to = message.get("reply_to_message")
    if not isinstance(reply_to, dict) or not isinstance(reply_to.get("message_id"), int):
        return _ignored("message is not a reply to a question")
    text = message.get("text")
    if not isinstance(text, str):
        return _ignored("reply carries no text (a media reply is not an answer)")

    message_id = reply_to["message_id"]
    callback_url = await lookup_callback_url(message_id)
    if callback_url is None:
        logger.warning("telegram inbound: no pending question for message_id=%s (expired or unknown)", message_id)
        return _ignored("no pending question for this reply")

    # Forward the typed answer. Transport errors propagate (-> 500) so Telegram
    # redelivers the update — the retry is the recovery, never a lost answer.
    async with telegram_http() as client:
        forwarded = await client.post(callback_url, json={"answer": text})

    if forwarded.status_code == 200:
        await clear_correlation(message_id)
        return JSONResponse({"data": {"status": "forwarded"}}, status_code=200)
    # 404 is the ONE terminal callback status (the door emits only
    # 200/400/401/404/413/500): the ticket is gone — expired or unknown — and
    # retrying the SAME answer can never succeed. (A duplicate forward of an
    # already-recorded answer resolves idempotently to 200 at the door.)
    if forwarded.status_code == 404:
        logger.warning(
            "telegram inbound: callback door returned terminal HTTP 404 for message_id=%s; dropping correlation",
            message_id,
        )
        await clear_correlation(message_id)
        return JSONResponse({"data": {"status": "stale"}}, status_code=200)
    if forwarded.status_code == 400:
        logger.warning(
            "telegram inbound: callback door rejected the answer for message_id=%s (400); "
            "correlation kept so the human can reply again",
            message_id,
        )
        return JSONResponse({"data": {"status": "rejected"}}, status_code=200)
    raise RuntimeError(
        f"interaction callback returned HTTP {forwarded.status_code} for message_id={message_id}; "
        f"failing the webhook delivery so Telegram redelivers"
    )
