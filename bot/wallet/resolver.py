"""Wallet mode resolver.

Given the process environment, returns the correct :class:`Signer`.

Priority (first match wins):

1. ``WALLET_MODE=rotation|strategy-assigned``          → MultiWalletSigner
2. ``WALLET_MODE=hardware``                            → HardwareWalletSigner
3. ``WALLET_MODE=cloud_kms`` + ``KMS_KEY_ID``          → CloudKMSSigner
4. A key exists in the OS keyring (Tier 1)             → SecureKey
5. Fallback to ``POLY_PRIVATE_KEY`` env                → PlainEnvKey
6. Paper mode → a dummy signer that refuses real signing

The password for Tier 1 / Tier 3 comes from:
- ``WALLET_PASSWORD`` env var (convenient for VPS / PM2); or
- interactive ``getpass`` when stdin is a TTY; or
- :class:`SignerError` if neither is available.
"""

from __future__ import annotations

import getpass
import os
import sys
from typing import Optional

from bot.logger import get_logger
from bot.wallet.base import Signer, SignerError
from bot.wallet.plain_env import PlainEnvKey
from bot.wallet.secure_key import SecureKey

log = get_logger("wallet.resolver")


def _resolve_password() -> str:
    pw = os.getenv("WALLET_PASSWORD")
    if pw:
        return pw
    if not sys.stdin.isatty():
        raise SignerError(
            "Wallet password required but no TTY and WALLET_PASSWORD is unset. "
            "Either export WALLET_PASSWORD or run the bot interactively."
        )
    return getpass.getpass("🛡️  Wallet password: ")


def current_mode() -> str:
    return (os.getenv("WALLET_MODE") or "auto").lower()


def resolve_signer(*, paper: bool = False) -> Optional[Signer]:
    """Return the configured Signer, or None for paper/no-op.

    Paper mode never needs a real signer: the executor short-circuits
    ``_execute_paper`` without touching py-clob-client-v2. We still try
    Tier 1 first so a paper run against real config surfaces wallet
    problems early.
    """
    mode = current_mode()

    # Tier 3
    if mode in {"rotation", "strategy-assigned", "strategy_assigned"}:
        from bot.wallet.multi_wallet import MultiWalletSigner
        password = _resolve_password()
        signer = MultiWalletSigner.from_env(password)
        log.info(f"[green]Wallet:[/] {signer.describe()}")
        return signer

    # Tier 2
    if mode == "hardware":
        from bot.wallet.hardware_wallet import HardwareWalletSigner
        signer = HardwareWalletSigner()
        log.info(f"[yellow]Wallet (stub):[/] {signer.describe()}")
        return signer

    # Tier 4
    if mode in {"cloud_kms", "kms"}:
        from bot.wallet.cloud_kms import CloudKMSSigner
        key_id = os.getenv("KMS_KEY_ID", "")
        if not key_id:
            raise SignerError("WALLET_MODE=cloud_kms requires KMS_KEY_ID.")
        signer = CloudKMSSigner(key_id=key_id)
        log.info(f"[yellow]Wallet (stub):[/] {signer.tier}")
        return signer

    # Tier 1 — preferred default
    stored_addr = SecureKey.stored_address()
    if stored_addr:
        try:
            password = _resolve_password()
            signer = SecureKey.load(password)
            log.info(f"[green]Wallet:[/] secure_key -> {signer.address()}")
            return signer
        except SignerError as e:
            if paper:
                log.warning(f"Wallet load failed ({e}); continuing in paper mode.")
            else:
                raise

    # Tier 0 — plain env fallback
    if os.getenv("POLY_PRIVATE_KEY"):
        try:
            signer = PlainEnvKey()
            log.info(f"[yellow]Wallet:[/] plain_env -> {signer.address()}")
            return signer
        except SignerError as e:
            if paper:
                log.warning(f"Plain env wallet failed ({e}); paper mode OK.")
            else:
                raise

    if paper:
        log.info("[grey]No wallet configured; paper mode will not sign anything.[/]")
        return None

    raise SignerError(
        "No wallet configured. Run `python main.py --setup-wallet` to create a "
        "Tier 1 encrypted wallet, or set POLY_PRIVATE_KEY in .env."
    )
