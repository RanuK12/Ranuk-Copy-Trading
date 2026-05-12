"""Tier 2 — Hardware wallet signer (Ledger / Trezor).

Status: **stub**. A hardware wallet requires manual confirmation of each
transaction on the device, which is incompatible with arbitrage or
micro-spread strategies that fire orders in milliseconds.

This module is kept as a scaffold so you can wire it up for low-frequency
strategies (tail-end, smart-copy) if you decide to custody > $5k with a
Ledger. See `docs/WALLET_SECURITY.md` for the full threat model.

To enable real signing, implement :meth:`sign_transaction` against
``ledgereth``/``ledgerblue`` or ``trezor.client``. The bot will NEVER
see the raw key — signatures come back from the device as (r, s, v).
"""

from __future__ import annotations

from bot.logger import get_logger
from bot.wallet.base import Signer, SignerError

log = get_logger("wallet.hardware")


class HardwareWalletSigner(Signer):
    tier = "hardware"

    def __init__(self, *, derivation_path: str = "m/44'/60'/0'/0/0") -> None:
        self._path = derivation_path
        # Deliberately not importing the heavy hardware libs at module import
        # time — fail loudly only when someone actually tries to use this.
        self._address: str | None = None
        log.warning(
            "Tier 2 hardware-wallet signer is a stub. "
            "See docs/WALLET_SECURITY.md for wiring instructions."
        )

    def supports_raw_key(self) -> bool:
        return False  # Hardware wallets never expose the key

    def address(self) -> str:
        if self._address is None:
            raise SignerError(
                "HardwareWalletSigner is a stub. Implement device enumeration "
                "and address derivation before use."
            )
        return self._address

    def private_key(self) -> str:
        raise SignerError(
            "Hardware wallets do not expose the raw private key. "
            "Use sign_transaction / sign_typed_data on the device instead."
        )

    async def sign_transaction(self, tx: dict) -> bytes:
        """Placeholder — replace with ledgereth / trezor-ethereum call."""
        raise SignerError(
            "HardwareWalletSigner.sign_transaction is not implemented. "
            "See bot/wallet/hardware_wallet.py for the integration plan."
        )
