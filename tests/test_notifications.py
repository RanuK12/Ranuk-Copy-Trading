"""Tests for the multi-channel notification router."""

from __future__ import annotations

import asyncio

import pytest

from bot.monitoring.notifications import (
    Channel,
    EventKind,
    NotificationConfig,
    NotificationEvent,
    NotificationRouter,
    Urgency,
)


class _RecorderChannel(Channel):
    def __init__(self, name: str, available: bool = True) -> None:
        self.name = name
        self._available = available
        self.received: list[NotificationEvent] = []

    def available(self) -> bool:
        return self._available

    async def send(self, event: NotificationEvent) -> None:
        self.received.append(event)


@pytest.fixture
def disabled_router() -> NotificationRouter:
    """A router with all channels turned off so we can control wiring ourselves."""
    cfg = NotificationConfig(desktop=False, sound=False, telegram=False, email=False)
    return NotificationRouter(config=cfg)


def test_router_starts_with_log_only_when_nothing_configured(disabled_router):
    # No channels wired means only the always-on LogChannel runs.
    assert disabled_router._channels == []


@pytest.mark.asyncio
async def test_router_fans_out_to_every_enabled_channel(disabled_router):
    ch_a = _RecorderChannel("a")
    ch_b = _RecorderChannel("b")
    disabled_router._channels.extend([ch_a, ch_b])

    event = NotificationEvent(
        kind=EventKind.TRADE_PROFIT,
        title="Test",
        message="hello",
        urgency=Urgency.NORMAL,
    )
    await disabled_router.notify(event)

    assert len(ch_a.received) == 1
    assert len(ch_b.received) == 1
    assert ch_a.received[0].title == "Test"


@pytest.mark.asyncio
async def test_channel_targeting_respects_whitelist(disabled_router):
    ch_a = _RecorderChannel("a")
    ch_b = _RecorderChannel("b")
    disabled_router._channels.extend([ch_a, ch_b])

    # Only target channel "a"
    event = NotificationEvent(
        kind=EventKind.CUSTOM,
        title="T",
        message="m",
        channels={"a"},
    )
    await disabled_router.notify(event)

    assert len(ch_a.received) == 1
    assert len(ch_b.received) == 0


@pytest.mark.asyncio
async def test_failing_channel_does_not_break_others(disabled_router):
    class _BoomChannel(Channel):
        name = "boom"
        async def send(self, event):  # noqa: D401
            raise RuntimeError("sim failure")

    good = _RecorderChannel("good")
    disabled_router._channels.extend([_BoomChannel(), good])

    await disabled_router.notify(
        NotificationEvent(kind=EventKind.TRADE_LOSS, title="x", message="y")
    )
    # The failing channel must not prevent delivery to the good one.
    assert len(good.received) == 1


@pytest.mark.asyncio
async def test_trade_filled_helper_picks_correct_event_kind(disabled_router):
    ch = _RecorderChannel("tap")
    disabled_router._channels.append(ch)

    await disabled_router.trade_filled("arbitrage", "market-a", +1.25, paper=False)
    await disabled_router.trade_filled("arbitrage", "market-b", -0.80, paper=False)
    await disabled_router.trade_filled("arbitrage", "market-c", 0.10, paper=True)

    kinds = [e.kind for e in ch.received]
    assert kinds == [
        EventKind.TRADE_PROFIT,
        EventKind.TRADE_LOSS,
        EventKind.TRADE_SIMULATED,
    ]


def test_notification_config_from_env(monkeypatch):
    monkeypatch.setenv("NOTIFY_DESKTOP", "false")
    monkeypatch.setenv("NOTIFY_SOUND", "true")
    monkeypatch.setenv("NOTIFY_TELEGRAM", "false")
    monkeypatch.setenv("NOTIFY_EMAIL", "false")
    cfg = NotificationConfig.from_env()
    assert cfg.desktop is False
    assert cfg.sound is True
    assert cfg.telegram is False
    assert cfg.email is False
