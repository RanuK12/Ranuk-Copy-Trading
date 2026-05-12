"""Polygon RPC client with Alchemy <-> QuickNode failover.

Design
------
Two ``Web3`` instances are instantiated (HTTP; WSS support added via
websockets in ``subscribe_logs`` when needed). Every call goes through
:meth:`RPCManager.call` which transparently switches to the backup
provider on connection / timeout errors. The last-known good provider
is remembered so steady-state latency is single-provider.

The module-level singleton is accessed via :func:`get_rpc`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from web3 import Web3

from bot.config import CFG
from bot.logger import get_logger

log = get_logger("rpc")


@dataclass
class Endpoint:
    name: str
    http: str
    wss: str


class RPCManager:
    def __init__(self) -> None:
        endpoints: list[Endpoint] = []
        if CFG.alchemy_http:
            endpoints.append(Endpoint("alchemy", CFG.alchemy_http, CFG.alchemy_wss))
        if CFG.quicknode_http:
            endpoints.append(Endpoint("quicknode", CFG.quicknode_http, CFG.quicknode_wss))
        if not endpoints:
            log.warning(
                "[yellow]No RPC endpoints configured; falling back to public Polygon RPC "
                "(will be slow — set ALCHEMY_HTTP_URL / QUICKNODE_HTTP_URL).[/]"
            )
            endpoints.append(
                Endpoint("public", "https://polygon-rpc.com", "wss://polygon-rpc.com")
            )
        self._endpoints = endpoints
        self._w3s: dict[str, Web3] = {
            ep.name: Web3(Web3.HTTPProvider(ep.http, request_kwargs={"timeout": 10}))
            for ep in endpoints
        }
        self._active = endpoints[0].name
        self._last_ok: float = 0.0

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------
    @property
    def w3(self) -> Web3:
        return self._w3s[self._active]

    @property
    def active_endpoint(self) -> Endpoint:
        return next(e for e in self._endpoints if e.name == self._active)

    def call(self, fn: Callable[[Web3], Any]) -> Any:
        """Run ``fn(w3)`` with automatic failover on error.

        Example
        -------
        >>> rpc.call(lambda w3: w3.eth.block_number)
        """
        last_err: Optional[Exception] = None
        for ep in self._endpoints_rotated():
            try:
                result = fn(self._w3s[ep.name])
                if self._active != ep.name:
                    log.info(f"[green]RPC switched to {ep.name}[/]")
                    self._active = ep.name
                self._last_ok = time.time()
                return result
            except Exception as e:  # noqa: BLE001
                last_err = e
                log.warning(f"RPC call on {ep.name} failed: {e!r}; trying next endpoint.")
        raise RuntimeError(f"All RPC endpoints failed: {last_err!r}")

    def _endpoints_rotated(self) -> list[Endpoint]:
        """Active endpoint first, then the others, preserving order."""
        order = [e for e in self._endpoints if e.name == self._active]
        order += [e for e in self._endpoints if e.name != self._active]
        return order

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def block_number(self) -> int:
        return self.call(lambda w3: w3.eth.block_number)

    def is_connected(self) -> bool:
        try:
            return bool(self.call(lambda w3: w3.is_connected()))
        except Exception:  # noqa: BLE001
            return False


RPC: Optional[RPCManager] = None


def get_rpc() -> RPCManager:
    global RPC
    if RPC is None:
        RPC = RPCManager()
    return RPC
