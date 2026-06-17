"""Telegram alerts. No-ops cleanly when not configured."""
from __future__ import annotations

import json
import logging
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("bot.notify")


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.enabled = bool(bot_token and chat_id)
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, message: str) -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled; message: %s", message)
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = urlencode({"chat_id": self.chat_id, "text": message}).encode()
        try:
            with urlopen(Request(url, data=data), timeout=10) as resp:  # noqa: S310
                return json.loads(resp.read()).get("ok", False)
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the bot
            logger.warning("Telegram send failed: %s", exc)
            return False
