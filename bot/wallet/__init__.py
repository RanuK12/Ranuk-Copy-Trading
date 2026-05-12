"""Wallet security subsystem (v3).

Four tiers, each exposing the same :class:`Signer` interface so the rest
of the bot doesn't care where the key lives:

    Tier 1 — SecureKey (keyring + Fernet).            Default, < $5k.
    Tier 2 — HardwareWalletSigner (Ledger/Trezor).    > $5k, low-freq strats only.
    Tier 3 — MultiWalletSigner (rotation or by-strategy).
    Tier 4 — CloudKMSSigner (AWS KMS / HashiCorp Vault). VPS deployments.

:func:`resolve_signer` reads the env and picks the right backend.
"""

from bot.wallet.base import Signer, SignerError  # noqa: F401
from bot.wallet.resolver import resolve_signer, current_mode  # noqa: F401
from bot.wallet.secure_key import SecureKey  # noqa: F401
