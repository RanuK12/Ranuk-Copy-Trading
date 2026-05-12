"""Fallback Tier 0 signer — reads POLY_PRIVATE_KEY from the environment.

Intentionally kept around for backward compatibility with v2 and for
paper-mode runs where no real signing happens. Logs a warning every
time it is instantiated.
"""

from __future__ import annotations

import os

from bot.logger import get_logger
from bot.wallet.base import Signer, SignerError
from bot.wallet.secure_key import _address_from_private_key

log = get_logger("wallet.env")


class PlainEnvKey(Signer):
    """Signer that reads the private key from ``POLY_PRIVATE_KEY`` in .env."""

    tier = "plain_env"

    def __init__(self) -> None:
        pk = os.getenv("POLY_PRIVATE_KEY", "").strip()
        if not pk:
            raise SignerError(
                "POLY_PRIVATE_KEY is empty. Set it in .env or run "
                "`python main.py --setup-wallet` for Tier 1 encrypted storage."
            )
        if not pk.startswith("0x"):
            pk = "0x" + pk
        self._private_key = pk
        try:
            self._address = _address_from_private_key(pk)
        except SignerError:
            # Allow a dummy/paper-mode placeholder; address becomes a sentinel
            self._address = "0x" + "0" * 40
        log.warning(
            "[yellow]Using PLAIN-TEXT private key from .env. "
            "Migrate to encrypted storage with --setup-wallet.[/]"
        )

    def address(self) -> str:
        return self._address

    def private_key(self) -> str:
        return self._private_key
