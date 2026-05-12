"""Prometheus metrics endpoint.

Starts an HTTP server (default :9090) exposing ``/metrics`` in Prometheus
text format. Wire it via the ``--metrics`` flag in main.py; off by default.

Minimal set:
* ``bot_trades_total{strategy,status}``   counter
* ``bot_scan_duration_seconds``           histogram
* ``bot_equity_usdc``                     gauge
* ``bot_queue_depth``                     gauge
* ``bot_api_errors_total``                counter

Strategies/executor call ``record_*`` helpers; if prometheus_client is
missing, all helpers are no-ops and the server silently does nothing.
"""

from __future__ import annotations

from typing import Optional

from bot.logger import get_logger

log = get_logger("metrics")

try:
    from prometheus_client import (  # type: ignore
        Counter,
        Gauge,
        Histogram,
        start_http_server,
    )
    _AVAILABLE = True
except Exception as e:  # noqa: BLE001
    log.debug(f"prometheus_client unavailable ({e}); metrics disabled.")
    _AVAILABLE = False


if _AVAILABLE:
    TRADES_TOTAL = Counter(
        "bot_trades_total",
        "Total trades processed",
        ["strategy", "status"],
    )
    SCAN_DURATION = Histogram(
        "bot_scan_duration_seconds",
        "Duration of scanner cycles",
        buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    )
    EQUITY = Gauge("bot_equity_usdc", "Current equity in USDC")
    QUEUE_DEPTH = Gauge("bot_queue_depth", "Pending opportunities in the queue")
    API_ERRORS = Counter(
        "bot_api_errors_total",
        "Total API errors by source",
        ["source"],
    )
else:
    class _Noop:
        def labels(self, *_, **__):
            return self
        def inc(self, *_):
            pass
        def set(self, *_):
            pass
        def observe(self, *_):
            pass

    TRADES_TOTAL = _Noop()  # type: ignore
    SCAN_DURATION = _Noop()  # type: ignore
    EQUITY = _Noop()  # type: ignore
    QUEUE_DEPTH = _Noop()  # type: ignore
    API_ERRORS = _Noop()  # type: ignore


_STARTED = False


def start(port: int = 9090) -> bool:
    """Start the /metrics HTTP endpoint. Returns True if running."""
    global _STARTED
    if not _AVAILABLE:
        log.info("[grey]Prometheus metrics disabled (prometheus_client missing).[/]")
        return False
    if _STARTED:
        return True
    try:
        start_http_server(port)
        _STARTED = True
        log.info(f"[green]Prometheus metrics on[/] http://localhost:{port}/metrics")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(f"Prometheus metrics failed to start: {e}")
        return False


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------
def record_trade(strategy: str, status: str) -> None:
    TRADES_TOTAL.labels(strategy=strategy, status=status).inc()


def record_scan_duration(seconds: float) -> None:
    SCAN_DURATION.observe(seconds)


def set_equity(value: float) -> None:
    EQUITY.set(value)


def set_queue_depth(value: int) -> None:
    QUEUE_DEPTH.set(value)


def record_api_error(source: str = "unknown") -> None:
    API_ERRORS.labels(source=source).inc()
