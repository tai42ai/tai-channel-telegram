"""The Telegram :class:`~tai_contract.channels.Channel`.

``deliver`` first validates the operator config — the bot token
(``CHANNEL_TELEGRAM_BOT_TOKEN``) must be set before any other work — then
resolves the recipient chat. EVERY failure on the deliver/notify path raises
:class:`~tai_contract.channels.ChannelDeliveryError`, operator
misconfiguration (missing bot token, missing default recipient) included — a
question that cannot leave is a delivery failure whatever the cause. A
caller-supplied
``delivery.recipient`` must be on the operator allowlist
(``CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS``) or the delivery is refused — fail
closed, nothing is sent; no caller recipient means the operator-configured
``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT``. The question then goes to that chat as
ONE ``sendMessage`` call — the Bot API has no idempotency key, so a blind
retry could duplicate
the question; a failed send raises instead (single-attempt policy). For a
typed-reply format (``text``/``select``) the message carries
``reply_markup: {force_reply: true}``,
so the human's next message arrives with ``reply_to_message`` pointing at the
question — the purpose-built Bot API correlation primitive. The sent message's
``message_id -> callback_url`` mapping is stored before ``deliver`` returns,
so by the time the helper unblocks the inbound door can route the answer. (A
reply landing in the sub-second window between Telegram's send ack and the
correlation write finds no mapping and is acked-and-ignored with a logged
reason — see the inbound door.)

A ``confirm`` or ``external`` question is delivered with a tappable URL
button opening the callback door instead (no correlation state, no inbound
involvement): the door records a confirm answer as a bool from the tap and
an external answer as a structured POST — neither is a value a human can
type into a chat, so a typed reply could never validate.

``notify`` is the fire-and-forget sibling: the same token check and recipient
resolution, then ONE plain ``sendMessage`` carrying only ``chat_id`` and
``text`` — no
reply markup, no deadline, no correlation state; nothing travels back.

Error text never includes the request URL — the bot token is embedded in every
Bot API URL and must stay out of logs and exception messages.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr
from tai_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification

from tai_channel_telegram.client import telegram_http
from tai_channel_telegram.correlation import store_correlation
from tai_channel_telegram.settings import require, require_secret, telegram_settings

# Tier-1 formats are answered at the callback door itself — a confirm answer is
# recorded as a bool from the tap, an external answer is a structured POST — so
# the question ships a tappable url button and no chat reply is expected.
# text/select are Tier-2: ForceReply + correlation, answered by typing.
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _question_text(delivery: ChannelDelivery) -> str:
    """Render the question for a plain-text chat, format-aware.

    A select question lists its options as guided text (validation stays
    server-side at the callback door); a Tier-1 question (confirm/external)
    is just the question — the url button below it is the answer path. The
    deadline is surfaced so the human knows the ask expires.
    """
    lines = [delivery.question]
    if delivery.answer_format == "select" and delivery.options:
        lines.append("")
        lines.extend(f"- {option}" for option in delivery.options)
        lines.append("")
        lines.append("Reply with one of the options above.")
    lines.append(f"(Answer before {delivery.timeout_at.strftime('%Y-%m-%d %H:%M %Z')}.)")
    return "\n".join(lines)


def _require_delivery[T](value: T | None, env_name: str) -> T:
    """Return the configured value, or raise :class:`ChannelDeliveryError`
    naming the missing env var.

    On the deliver/notify path EVERY failure — operator misconfiguration
    included — is a delivery failure, so the generic :func:`require` check is
    retyped here as :class:`~tai_contract.channels.ChannelDeliveryError` with
    the same message.
    """
    try:
        return require(value, env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """Return the secret's plaintext, or raise :class:`ChannelDeliveryError`
    on unset/EMPTY — fail CLOSED.

    The deliver/notify retyping of :func:`require_secret`: same checks, same
    message (which names the env var and never the secret), delivery error
    type.
    """
    try:
        return require_secret(value, env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _resolve_target(recipient: str | None) -> str:
    """Resolve the target chat id, fail closed against the operator allowlist.

    The allowlist gates ONLY caller-supplied values; the operator-set default
    is implicitly trusted. A caller-supplied ``recipient`` must be on
    ``CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS`` or a
    :class:`~tai_contract.channels.ChannelDeliveryError` refuses the send —
    nothing goes out. ``None`` means the operator-configured
    ``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT``, which must be set — a missing
    default is a delivery failure and raises
    :class:`~tai_contract.channels.ChannelDeliveryError` naming the env var.
    """
    settings = telegram_settings()
    if recipient is None:
        return _require_delivery(settings.default_recipient, "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    if recipient not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {recipient!r} is not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS; refusing to send"
        )
    return recipient


async def _send_message(token: str, payload: dict[str, Any], context: str) -> dict[str, Any]:
    """POST ``payload`` as one Bot API ``sendMessage`` and validate the response.

    ``token`` is the bot credential, validated by the caller before any work
    happens. Returns the decoded ``ok: true`` body. A transport error, a
    non-200 status, a non-JSON body, or an ``ok: false`` result each raises
    :class:`~tai_contract.channels.ChannelDeliveryError` naming ``context``
    (e.g. ``"interaction <id>"`` or ``"notification"``). The request URL
    embeds the bot token and never appears in error text.
    """
    try:
        async with telegram_http() as client:
            response = await client.post(f"{telegram_settings().api_base_url}/bot{token}/sendMessage", json=payload)
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"telegram sendMessage failed for {context}: {type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise ChannelDeliveryError(
            f"telegram sendMessage returned HTTP {response.status_code} for {context}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError(f"telegram sendMessage returned a non-JSON body for {context}") from exc
    if not data.get("ok"):
        raise ChannelDeliveryError(
            f"telegram sendMessage rejected {context}: "
            f"error_code={data.get('error_code')} description={data.get('description')!r}"
        )
    return data


class TelegramChannel:
    """Satisfies the :class:`~tai_contract.channels.Channel` protocol.

    Stateless — configuration is read from the cached settings at each send
    so a live-reload picks up rotated credentials immediately (no
    per-instance snapshot to go stale).
    """

    async def deliver(self, delivery: ChannelDelivery) -> None:
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        target = _resolve_target(delivery.recipient)

        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        payload: dict[str, Any] = {"chat_id": target, "text": _question_text(delivery)}
        if delivery.answer_format in _TIER1_FORMATS:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Answer", "url": delivery.callback_url}]]}
        else:
            payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Reply to answer"}

        data = await _send_message(token, payload, f"interaction {delivery.interaction_id}")

        if delivery.answer_format in _TIER1_FORMATS:
            return

        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise ChannelDeliveryError(
                f"telegram sendMessage ok response for interaction {delivery.interaction_id} "
                f"carried no result.message_id"
            )

        # Remaining budget measured AFTER the send returned, so the Redis key
        # expires at the question's deadline rather than the deadline plus the
        # send duration. A budget spent mid-send makes ``store_correlation``
        # reject the non-positive TTL, surfaced through the wrap below.
        ttl_seconds = math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds())
        try:
            await store_correlation(message_id, delivery.callback_url, ttl_seconds)
        except Exception as exc:
            raise ChannelDeliveryError(
                f"question {delivery.interaction_id} was sent (message_id={message_id}) but its "
                f"correlation could not be stored; the reply cannot be routed"
            ) from exc

    async def notify(self, notification: ChannelNotification) -> None:
        """Send ``notification.message`` as ONE plain ``sendMessage`` — fire-and-forget.

        The recipient resolves through the same allowlist gate as ``deliver``.
        The payload carries only ``chat_id`` and ``text``: no reply markup, no
        deadline, no correlation state — nothing travels back. Any failure
        raises :class:`~tai_contract.channels.ChannelDeliveryError`; a plain
        return means the Bot API accepted the message.
        """
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        target = _resolve_target(notification.recipient)
        await _send_message(token, {"chat_id": target, "text": notification.message}, "notification")
