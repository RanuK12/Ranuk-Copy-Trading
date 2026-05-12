"""Async Telegram alerter + remote kill-switch listener.

Two independent coroutines:

* :meth:`send` / :meth:`send_markdown`    — outbound notifications
* :meth:`listen_for_commands`             — polls ``getUpdates`` for
  ``/emergencystop`` + ``/resume`` + ``/status`` and calls registered
  callbacks. No webhook required; runs fine on a Mac Mini behind NAT.

If TELEGRAM_BOT_TOKEN is unset the client becomes a silent no-op so
strategies don't need conditionals.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

import httpx

from bot.config import CFG
from bot.logger import get_logger

log = get_logger("telegram")

CommandHandler = Callable[[str], Awaitable[None]]


class TelegramClient:
    def __init__(self) -> None:
        self._token = CFG.telegram_token
        self._chat_id = CFG.telegram_chat_id
        self._enabled = bool(self._token and self._chat_id)
        self._handlers: dict[str, CommandHandler] = {}
        self._last_update_id = 0
        self._http = httpx.AsyncClient(timeout=15.0)

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    async def send(self, text: str, *, markdown: bool = False) -> None:
        if not self._enabled:
            log.debug(f"Telegram disabled; would have sent: {text[:80]}")
            return
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if markdown:
            payload["parse_mode"] = "Markdown"
        try:
            r = await self._http.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage", data=payload
            )
            if r.status_code != 200:
                log.warning(f"Telegram send {r.status_code}: {r.text[:120]}")
        except Exception as e:  # noqa: BLE001
            log.debug(f"Telegram send failed: {e}")

    async def send_markdown(self, text: str) -> None:
        await self.send(text, markdown=True)

    # ------------------------------------------------------------------
    # Command listener (long-poll /getUpdates)
    # ------------------------------------------------------------------
    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._handlers[name.lstrip("/").lower()] = handler

    async def listen_for_commands(self, poll_interval: float = 3.0) -> None:
        if not self._enabled:
            log.info("[grey]Telegram command listener disabled (no token).[/]")
            return
        log.info("[green]Telegram command listener started.[/]")
        while True:
            try:
                r = await self._http.get(
                    f"https://api.telegram.org/bot{self._token}/getUpdates",
                    params={"timeout": 25, "offset": self._last_update_id + 1},
                    timeout=30.0,
                )
                data = r.json()
                for upd in data.get("result", []):
                    self._last_update_id = upd["update_id"]
                    msg = upd.get("message") or upd.get("edited_message") or {}
                    text = (msg.get("text") or "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(self._chat_id):
                        continue  # ignore others
                    if not text.startswith("/"):
                        continue
                    cmd, *rest = text.split(maxsplit=1)
                    key = cmd.lstrip("/").split("@")[0].lower()
                    handler = self._handlers.get(key)
                    if handler:
                        arg = rest[0] if rest else ""
                        try:
                            await handler(arg)
                        except Exception as e:  # noqa: BLE001
                            log.exception(f"Handler for {cmd} crashed: {e}")
                    else:
                        await self.send(f"Unknown command: {cmd}")
            except Exception as e:  # noqa: BLE001
                log.debug(f"Telegram poll error: {e}; backing off.")
                await asyncio.sleep(poll_interval * 2)
                continue
            await asyncio.sleep(poll_interval)


TG: Optional[TelegramClient] = None


def get_telegram() -> TelegramClient:
    global TG
    if TG is None:
        TG = TelegramClient()
    return TG
