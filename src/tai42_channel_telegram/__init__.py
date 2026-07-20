"""tai42-channel-telegram — the Telegram channel plugin for the TAI ecosystem.

A :class:`~tai42_contract.channels.Channel` that delivers ``ask_user`` questions
to a configured Telegram chat (``sendMessage`` — ForceReply for typed
text/select answers, a tappable URL button opening the callback door for
confirm/external) and bridges the human's typed reply back to the interaction
callback door through its own verified webhook route. The runtime discovers it
through the manifest's ``channel_modules`` — it imports every module under the
package, and :mod:`tai42_channel_telegram.register` fires the registrations as a
side-effect (the ``"telegram"`` channel name, the inbound route, the setWebhook
startup hook). Importing this ``__init__`` alone does NOT register (library use).
"""

from tai42_channel_telegram.channel import TelegramChannel
from tai42_channel_telegram.settings import TelegramSettings, telegram_settings

__all__ = ["TelegramChannel", "TelegramSettings", "telegram_settings"]
