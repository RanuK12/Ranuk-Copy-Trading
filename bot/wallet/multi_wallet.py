"""Tier 3 — Multi-wallet routing.

Two modes:

* ``rotation`` — round-robin across N secure keys for each new order.
* ``strategy_assigned`` — specific strategies bind to specific keyring
  slots via env vars (``WALLET_ARBITRAGE``, ``WALLET_TAIL_END``, etc.).

Use case: if one wallet gets flagged or has a partial-fill situation you
don't want to compound, other wallets keep operating. Also useful to
separate hot capital (arbitrage) from cold capital (tail-end).

Each "slot" is an independent :class:`SecureKey` stored under a distinct
keyring username. The wizard (--setup-wallet) has a "+ add another" flow
that writes new slots.
"""

from __future__ import annotations

import itertools
import os
from typing import Iterable, Optional

from bot.logger import get_logger
from bot.wallet.base import Signer, SignerError
from bot.wallet.secure_key import _SERVICE, SecureKey, _default_keyring

log = get_logger("wallet.multi")


def _slot_username(slot_id: str) -> str:
    return f"private_key_slot__{slot_id}"


def store_slot(slot_id: str, private_key: str, password: str) -> str:
    """Persist a keyring slot. Returns the derived address."""
    from cryptography.fernet import Fernet  # local import for test isolation
    # Reuse SecureKey.setup by temporarily swapping the username.
    return _SlotBackedSecureKey.setup(slot_id, private_key, password)


def list_slots() -> list[str]:
    """Return the slot ids currently present in the keyring (best-effort)."""
    # keyring lacks an enumeration API; we track slots in a tiny manifest.
    kr = _default_keyring()
    raw = kr.get_password(_SERVICE, "slot_manifest")
    if not raw:
        return []
    return [s for s in raw.split(",") if s]


def _add_slot_to_manifest(slot_id: str) -> None:
    kr = _default_keyring()
    existing = set(list_slots())
    existing.add(slot_id)
    kr.set_password(_SERVICE, "slot_manifest", ",".join(sorted(existing)))


class _SlotBackedSecureKey(SecureKey):
    """A SecureKey that uses a slot-specific keyring username."""

    @classmethod
    def setup(cls, slot_id: str, private_key: str, password: str) -> str:  # type: ignore[override]
        kr = _default_keyring()
        # Cheap dispatch: temporarily patch _USERNAME on the parent by
        # writing through a proxy wrapper.
        from bot.wallet import secure_key as sk_mod

        original = sk_mod._USERNAME
        try:
            sk_mod._USERNAME = _slot_username(slot_id)
            address = SecureKey.setup(private_key, password, keyring_mod=kr)
        finally:
            sk_mod._USERNAME = original
        _add_slot_to_manifest(slot_id)
        log.info(f"Slot '{slot_id}' stored -> {address}")
        return address

    @classmethod
    def load(cls, slot_id: str, password: str) -> "_SlotBackedSecureKey":  # type: ignore[override]
        from bot.wallet import secure_key as sk_mod

        original = sk_mod._USERNAME
        try:
            sk_mod._USERNAME = _slot_username(slot_id)
            sk = SecureKey.load(password)
        finally:
            sk_mod._USERNAME = original
        return _SlotBackedSecureKey(sk.private_key(), sk.address())


class MultiWalletSigner(Signer):
    """Rotate or route orders across multiple Tier-1 wallets."""

    tier = "multi_wallet"

    def __init__(
        self,
        slots: dict[str, SecureKey],
        *,
        mode: str = "rotation",
        strategy_map: Optional[dict[str, str]] = None,
    ) -> None:
        if not slots:
            raise SignerError("MultiWalletSigner requires at least one slot.")
        self._slots = slots
        self._mode = mode
        self._strategy_map = strategy_map or {}
        self._rotation = itertools.cycle(slots.keys())

    # -- Signer interface (defaults to first slot when asked generically) --
    def address(self) -> str:
        return next(iter(self._slots.values())).address()

    def private_key(self) -> str:
        return next(iter(self._slots.values())).private_key()

    # -- Multi-wallet specific ------------------------------------------
    def pick_for_strategy(self, strategy: str) -> SecureKey:
        if self._mode == "strategy_assigned":
            slot_id = self._strategy_map.get(strategy)
            if not slot_id or slot_id not in self._slots:
                raise SignerError(
                    f"No slot mapped for strategy '{strategy}'. "
                    f"Set WALLET_{strategy.upper()} to one of {list(self._slots)}."
                )
            return self._slots[slot_id]
        # rotation
        slot_id = next(self._rotation)
        return self._slots[slot_id]

    def describe(self) -> str:
        return (
            f"multi_wallet[{self._mode}] slots={list(self._slots)} "
            f"strategy_map={self._strategy_map}"
        )

    # -- Construction helpers -------------------------------------------
    @classmethod
    def from_env(cls, password: str) -> "MultiWalletSigner":
        mode = os.getenv("WALLET_MODE", "single").lower()
        if mode not in {"rotation", "strategy-assigned", "strategy_assigned"}:
            raise SignerError(
                f"WALLET_MODE={mode!r} is not a multi-wallet mode. "
                "Use 'rotation' or 'strategy-assigned'."
            )

        strategy_map: dict[str, str] = {}
        if mode.replace("-", "_") == "strategy_assigned":
            # Collect WALLET_<STRATEGY>=slot_id pairs from env
            for key, value in os.environ.items():
                if key.startswith("WALLET_") and key not in {
                    "WALLET_MODE",
                    "WALLET_ROTATION_KEYS",
                }:
                    strat_name = key[len("WALLET_") :].lower()
                    strategy_map[strat_name] = value.strip()
            slot_ids: Iterable[str] = set(strategy_map.values())
        else:  # rotation
            raw = os.getenv("WALLET_ROTATION_KEYS", "")
            slot_ids = [s.strip() for s in raw.split(",") if s.strip()]

        if not slot_ids:
            raise SignerError(
                "No slots configured. Populate WALLET_ROTATION_KEYS or "
                "WALLET_<STRATEGY> variables."
            )

        slots: dict[str, SecureKey] = {}
        for slot_id in slot_ids:
            slots[slot_id] = _SlotBackedSecureKey.load(slot_id, password)

        normalized_mode = "strategy_assigned" if "strategy" in mode else "rotation"
        return cls(slots, mode=normalized_mode, strategy_map=strategy_map)
