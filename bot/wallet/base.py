"""Wallet signer interface shared by all tiers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SignerError(RuntimeError):
    """Raised when a signer cannot produce a signature (bad key, cancelled, etc.)."""


class Signer(ABC):
    """Abstraction over anything that can provide the raw EOA private key."""

    #: Short id for the signer tier ("secure_key", "hardware", etc.)
    tier: str = "base"

    @abstractmethod
    def address(self) -> str:
        """Return the Ethereum address (checksummed, ``0x...``) of this signer."""

    @abstractmethod
    def private_key(self) -> str:
        """Return the raw hex private key (``0x...``).

        Tier 2 / Tier 4 signers that do NOT expose raw keys should raise
        :class:`SignerError` with a message pointing the caller to the
        async ``sign_typed_data`` / ``sign_transaction`` methods.
        """

    # Optional hooks -----------------------------------------------------
    def supports_raw_key(self) -> bool:
        """True for tiers where :meth:`private_key` is valid."""
        return True

    def describe(self) -> str:
        return f"{self.tier}:{self.address()}"
