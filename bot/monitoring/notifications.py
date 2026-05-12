"""Multi-channel notification router.

Events are published via :func:`notify` with a typed :class:`NotificationEvent`
and fan out to every enabled channel:

* **Desktop** (plyer)  — cross-platform native toast, always enabled by default.
* **Sound** (beepy)    — differentiated audio cues for profit / loss / critical.
* **Telegram**         — existing ``bot/clients/telegram.py`` integration.
* **Email** (smtplib)  — critical-only by default (e.g. kill switch).
* **Log fallback**     — if nothing is configured, log at the appropriate level.

All channels degrade gracefully: if a module is missing or a third-party
library is unavailable, the router logs a one-time warning and continues
with the channels that do work. Telegram is now fully optional.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import Enum
from typing import Any, Optional

from bot.logger import get_logger

log = get_logger("notify")


# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------
class Urgency(str, Enum):
    LOW = "low"          # arb detected, market event
    NORMAL = "normal"    # trade filled, bot started
    HIGH = "high"        # strategy paused, partial fill
    CRITICAL = "critical"  # kill switch, daily cap, bot crash


class EventKind(str, Enum):
    TRADE_PROFIT = "trade_profit"
    TRADE_LOSS = "trade_loss"
    TRADE_SIMULATED = "trade_simulated"
    ARB_DETECTED = "arb_detected"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_CAP = "daily_loss_cap"
    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"
    STRATEGY_PAUSED = "strategy_paused"
    PARTIAL_FILL = "partial_fill"
    CONFIG_RELOADED = "config_reloaded"
    CUSTOM = "custom"


# Maps event kind -> sound name (beepy vocabulary: "coin", "error",
# "ready", "ping", "success", "robot_error", "wilhelm"). Missing entries
# mean no beep.
_SOUND_MAP: dict[EventKind, str] = {
    EventKind.TRADE_PROFIT: "coin",
    EventKind.TRADE_LOSS: "error",
    EventKind.ARB_DETECTED: "ready",
    EventKind.KILL_SWITCH: "robot_error",
    EventKind.DAILY_LOSS_CAP: "robot_error",
    EventKind.PARTIAL_FILL: "ping",
    EventKind.BOT_STARTED: "ping",
}


@dataclass
class NotificationEvent:
    kind: EventKind
    title: str
    message: str
    urgency: Urgency = Urgency.NORMAL
    # Which channels to target. If empty -> "all enabled".
    channels: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    def should_send_to(self, channel: str) -> bool:
        return not self.channels or channel in self.channels


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------
class Channel:
    name: str = "base"

    def available(self) -> bool:  # pragma: no cover - trivial
        return True

    async def send(self, event: NotificationEvent) -> None:  # pragma: no cover
        raise NotImplementedError


class LogChannel(Channel):
    """Always-on fallback: writes every event to the shared logger."""

    name = "log"

    async def send(self, event: NotificationEvent) -> None:
        level = {
            Urgency.LOW: log.debug,
            Urgency.NORMAL: log.info,
            Urgency.HIGH: log.warning,
            Urgency.CRITICAL: log.error,
        }[event.urgency]
        level(f"[{event.kind.value}] {event.title} — {event.message}")


class DesktopChannel(Channel):
    name = "desktop"

    def __init__(self) -> None:
        try:
            from plyer import notification  # type: ignore
            self._notify = notification.notify
            self._available = True
        except Exception as e:  # noqa: BLE001
            log.debug(f"plyer unavailable, desktop notifications disabled: {e}")
            self._notify = None
            self._available = False

    def available(self) -> bool:
        return self._available

    async def send(self, event: NotificationEvent) -> None:
        if not self._available or self._notify is None:
            return
        # plyer's timeout=0 means sticky (critical events persist)
        timeout = 0 if event.urgency == Urgency.CRITICAL else 10
        try:
            await asyncio.to_thread(
                self._notify,
                title=f"Polymarket Bot — {event.title}",
                message=event.message[:240],
                app_name="Polymarket Bot",
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            log.debug(f"desktop notify failed: {e}")


class SoundChannel(Channel):
    name = "sound"

    def __init__(self) -> None:
        try:
            import beepy  # type: ignore
            self._beep = beepy.beep
            self._available = True
        except Exception as e:  # noqa: BLE001
            log.debug(f"beepy unavailable, sound alerts disabled: {e}")
            self._beep = None
            self._available = False

    def available(self) -> bool:
        return self._available

    async def send(self, event: NotificationEvent) -> None:
        if not self._available or self._beep is None:
            return
        sound = _SOUND_MAP.get(event.kind)
        if sound is None:
            return
        try:
            await asyncio.to_thread(self._beep, sound=sound)
        except Exception as e:  # noqa: BLE001
            log.debug(f"sound beep failed: {e}")


class TelegramChannel(Channel):
    """Thin wrapper around the existing telegram client."""

    name = "telegram"

    def __init__(self) -> None:
        try:
            from bot.clients.telegram import get_telegram
            self._tg = get_telegram()
            self._available = bool(self._tg.enabled)
        except Exception as e:  # noqa: BLE001
            log.debug(f"telegram client unavailable: {e}")
            self._tg = None
            self._available = False

    def available(self) -> bool:
        return self._available

    async def send(self, event: NotificationEvent) -> None:
        if not self._available or self._tg is None:
            return
        icon = {
            EventKind.TRADE_PROFIT: "💰",
            EventKind.TRADE_LOSS: "📉",
            EventKind.TRADE_SIMULATED: "💸",
            EventKind.ARB_DETECTED: "🔔",
            EventKind.KILL_SWITCH: "🛑",
            EventKind.DAILY_LOSS_CAP: "⚠️",
            EventKind.BOT_STARTED: "🤖",
            EventKind.BOT_STOPPED: "👋",
            EventKind.PARTIAL_FILL: "⚠️",
            EventKind.STRATEGY_PAUSED: "⏸️",
            EventKind.CONFIG_RELOADED: "🔄",
        }.get(event.kind, "")
        text = f"{icon} *{event.title}*\n{event.message}".strip()
        try:
            await self._tg.send_markdown(text)
        except Exception as e:  # noqa: BLE001
            log.debug(f"telegram send failed: {e}")


class EmailChannel(Channel):
    """Critical-only email alerter via SMTP.

    Env vars (all required if enabled):
        EMAIL_SMTP_HOST, EMAIL_SMTP_PORT (default 465),
        EMAIL_USERNAME, EMAIL_PASSWORD,
        EMAIL_FROM, EMAIL_TO
    """

    name = "email"

    def __init__(self) -> None:
        self.host = os.getenv("EMAIL_SMTP_HOST", "")
        self.port = int(os.getenv("EMAIL_SMTP_PORT", "465"))
        self.user = os.getenv("EMAIL_USERNAME", "")
        self.password = os.getenv("EMAIL_PASSWORD", "")
        self.from_addr = os.getenv("EMAIL_FROM", self.user)
        self.to_addr = os.getenv("EMAIL_TO", "")
        self._available = bool(self.host and self.user and self.password and self.to_addr)

    def available(self) -> bool:
        return self._available

    async def send(self, event: NotificationEvent) -> None:
        # Email is intentionally slow + synchronous. Only send on CRITICAL.
        if not self._available:
            return
        if event.urgency != Urgency.CRITICAL:
            return

        msg = EmailMessage()
        msg["Subject"] = f"[Polymarket Bot] {event.title}"
        msg["From"] = self.from_addr
        msg["To"] = self.to_addr
        msg.set_content(
            f"{event.title}\n\n{event.message}\n\n"
            f"Urgency: {event.urgency.value}\nKind: {event.kind.value}\n"
        )
        try:
            await asyncio.to_thread(self._send_smtp, msg)
        except Exception as e:  # noqa: BLE001
            log.warning(f"email send failed: {e}")

    def _send_smtp(self, msg: EmailMessage) -> None:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=20) as s:
            s.login(self.user, self.password)
            s.send_message(msg)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


@dataclass
class NotificationConfig:
    desktop: bool = True
    sound: bool = True
    telegram: bool = False
    email: bool = False

    @classmethod
    def from_env(cls) -> "NotificationConfig":
        return cls(
            desktop=_env_bool("NOTIFY_DESKTOP", True),
            sound=_env_bool("NOTIFY_SOUND", True),
            telegram=_env_bool("NOTIFY_TELEGRAM", False),
            email=_env_bool("NOTIFY_EMAIL", False),
        )


class NotificationRouter:
    def __init__(self, config: Optional[NotificationConfig] = None) -> None:
        self.config = config or NotificationConfig.from_env()
        self._channels: list[Channel] = []
        self._log = LogChannel()  # always on
        self._wire()

    def _wire(self) -> None:
        if self.config.desktop:
            ch = DesktopChannel()
            if ch.available():
                self._channels.append(ch)
            else:
                log.warning("Desktop notifications requested but plyer not available.")
        if self.config.sound:
            ch = SoundChannel()
            if ch.available():
                self._channels.append(ch)
            else:
                log.debug("Sound alerts disabled (beepy unavailable).")
        if self.config.telegram:
            ch = TelegramChannel()
            if ch.available():
                self._channels.append(ch)
            else:
                log.warning("NOTIFY_TELEGRAM=true but telegram client isn't configured.")
        if self.config.email:
            ch = EmailChannel()
            if ch.available():
                self._channels.append(ch)
            else:
                log.warning("NOTIFY_EMAIL=true but EMAIL_* env vars are incomplete.")

        if not self._channels:
            log.info(
                "[grey]Notifications: no external channels configured; "
                "log fallback only.[/]"
            )
        else:
            log.info(
                f"[green]Notifications wired:[/] {', '.join(c.name for c in self._channels)}"
            )

    async def notify(self, event: NotificationEvent) -> None:
        """Fan out an event to every enabled channel + the log."""
        # Log first so there's always a record regardless of what fails below.
        await self._log.send(event)
        for ch in self._channels:
            if not event.should_send_to(ch.name):
                continue
            try:
                await ch.send(event)
            except Exception as e:  # noqa: BLE001
                log.debug(f"channel {ch.name} raised: {e}")

    # Convenience helpers -------------------------------------------------
    async def trade_filled(
        self, strategy: str, market: str, pnl_usdc: float, paper: bool = False
    ) -> None:
        kind = (
            EventKind.TRADE_SIMULATED if paper
            else (EventKind.TRADE_PROFIT if pnl_usdc >= 0 else EventKind.TRADE_LOSS)
        )
        sign = "+" if pnl_usdc >= 0 else ""
        await self.notify(
            NotificationEvent(
                kind=kind,
                title=f"{strategy} {'(sim)' if paper else ''} {sign}{pnl_usdc:.2f}".strip(),
                message=f"Market: {market}",
                urgency=Urgency.NORMAL,
                metadata={"strategy": strategy, "pnl": pnl_usdc, "paper": paper},
            )
        )

    async def arb_detected(self, market: str, edge_pct: float) -> None:
        await self.notify(
            NotificationEvent(
                kind=EventKind.ARB_DETECTED,
                title="Arbitrage detected",
                message=f"{market} edge={edge_pct * 100:+.2f}%",
                urgency=Urgency.LOW,
            )
        )

    async def kill_switch(self, reason: str) -> None:
        await self.notify(
            NotificationEvent(
                kind=EventKind.KILL_SWITCH,
                title="KILL SWITCH ACTIVATED",
                message=f"Reason: {reason}. All new orders blocked.",
                urgency=Urgency.CRITICAL,
            )
        )

    async def daily_cap(self, pct_lost: float) -> None:
        await self.notify(
            NotificationEvent(
                kind=EventKind.DAILY_LOSS_CAP,
                title="Daily loss cap reached",
                message=f"Lost {pct_lost * 100:.2f}% of capital today. Kill switch engaged.",
                urgency=Urgency.CRITICAL,
            )
        )

    async def bot_started(self, mode: str, capital: float) -> None:
        await self.notify(
            NotificationEvent(
                kind=EventKind.BOT_STARTED,
                title=f"Bot online ({mode})",
                message=f"Capital ${capital:.2f}",
                urgency=Urgency.NORMAL,
            )
        )


# Global singleton accessor -------------------------------------------------
_ROUTER: Optional[NotificationRouter] = None


def get_router() -> NotificationRouter:
    global _ROUTER
    if _ROUTER is None:
        _ROUTER = NotificationRouter()
    return _ROUTER


async def notify(event: NotificationEvent) -> None:
    """Module-level shortcut."""
    await get_router().notify(event)
