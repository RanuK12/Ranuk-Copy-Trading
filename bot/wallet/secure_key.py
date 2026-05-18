"""Tier 1 — Encrypted private key stored in the OS keyring.

Design
------
* The plaintext key is NEVER persisted to disk.
* It lives in the OS keychain (macOS Keychain, Windows Credential Manager,
  Linux Secret Service / libsecret), encrypted at rest by the OS.
* On top of that we wrap it with :class:`cryptography.fernet.Fernet`,
  deriving the Fernet key from a password the user types at startup.
  This means: even if an attacker dumps the keyring, they still need the
  password to decrypt.
* The salt is stored alongside the ciphertext so the same password
  reproduces the same Fernet key.

Usage
-----
>>> from bot.wallet.secure_key import SecureKey
>>> SecureKey.setup("0xabc...", "my-password")     # one-time
>>> sk = SecureKey.load("my-password")             # runtime
>>> sk.address()
'0xAddr...'
>>> sk.private_key()
'0xabc...'
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from bot.logger import get_logger
from bot.wallet.base import Signer, SignerError

log = get_logger("wallet.secure")

_SERVICE = "polymarket-bot"
_USERNAME = "private_key_v1"
_KDF_ITERATIONS = 390_000  # OWASP 2023 recommendation for SHA-256


def _derive_fernet_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _address_from_private_key(private_key: str) -> str:
    """Derive the checksum address from a hex private key.

    Uses eth_account which is already a transitive dep via web3/py-clob-client-v2.
    """
    try:
        from eth_account import Account  # type: ignore
    except Exception as e:  # noqa: BLE001
        raise SignerError(
            "eth_account is required to derive wallet addresses. "
            f"Install web3/py-clob-client-v2 to get it. ({e})"
        )
    pk = private_key.strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    return Account.from_key(pk).address


@dataclass
class _Payload:
    """JSON-encoded blob that lives in the keyring."""

    ciphertext: str  # Fernet token
    salt: str  # base64 of the KDF salt
    address: str  # address (for display without decrypting)
    version: int = 1

    def to_json(self) -> str:
        return json.dumps(self.__dict__, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "_Payload":
        d = json.loads(raw)
        return cls(**d)


class SecureKey(Signer):
    """Encrypted-at-rest private key held in the OS keyring."""

    tier = "secure_key"

    def __init__(self, private_key_hex: str, address: str) -> None:
        self._private_key = private_key_hex
        self._address = address

    # -- Accessors -------------------------------------------------------
    def address(self) -> str:
        return self._address

    def private_key(self) -> str:
        return self._private_key

    # -- One-time setup --------------------------------------------------
    @staticmethod
    def setup(private_key: str, password: str, *, keyring_mod=None) -> str:
        """Encrypt a key with ``password`` and persist to the keyring.

        Returns the derived wallet address. Raises :class:`SignerError`
        if the key is not a valid 32-byte hex string.
        """
        if not password or len(password) < 8:
            raise SignerError("Password must be at least 8 characters.")

        pk = private_key.strip()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        # Basic sanity: 32 bytes = 66 chars including '0x'
        if len(pk) != 66:
            raise SignerError(
                f"Private key length {len(pk) - 2} hex chars is not 64 (32 bytes)."
            )

        # Derive the address up-front so we fail fast on a bad key
        address = _address_from_private_key(pk)

        salt = secrets.token_bytes(16)
        fkey = _derive_fernet_key(password, salt)
        token = Fernet(fkey).encrypt(pk.encode("utf-8"))

        payload = _Payload(
            ciphertext=token.decode("utf-8"),
            salt=base64.b64encode(salt).decode("utf-8"),
            address=address,
        )
        kr = keyring_mod or _default_keyring()
        kr.set_password(_SERVICE, _USERNAME, payload.to_json())
        log.info(f"[green]Secure key stored[/] for address {address}")
        return address

    # -- Load at runtime --------------------------------------------------
    @staticmethod
    def load(password: str, *, keyring_mod=None) -> "SecureKey":
        kr = keyring_mod or _default_keyring()
        raw = kr.get_password(_SERVICE, _USERNAME)
        if not raw:
            raise SignerError(
                "No encrypted key in the keyring. Run: "
                "python main.py --setup-wallet"
            )
        payload = _Payload.from_json(raw)
        salt = base64.b64decode(payload.salt.encode("utf-8"))
        fkey = _derive_fernet_key(password, salt)
        try:
            plain = Fernet(fkey).decrypt(payload.ciphertext.encode("utf-8"))
        except InvalidToken:
            raise SignerError("Wrong password — decryption failed.")
        return SecureKey(plain.decode("utf-8"), payload.address)

    # -- Inspection without decrypting -----------------------------------
    @staticmethod
    def stored_address(*, keyring_mod=None) -> Optional[str]:
        """Return the address of the stored key without needing the password."""
        kr = keyring_mod or _default_keyring()
        raw = kr.get_password(_SERVICE, _USERNAME)
        if not raw:
            return None
        try:
            return _Payload.from_json(raw).address
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def delete(*, keyring_mod=None) -> bool:
        kr = keyring_mod or _default_keyring()
        try:
            kr.delete_password(_SERVICE, _USERNAME)
            return True
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Keyring backend resolution
# ---------------------------------------------------------------------------
def _default_keyring():
    """Return the best-available keyring backend.

    Headless Linux servers often lack Secret Service; we fall back to a
    file-based encrypted backend so the CLI still works on a VPS.
    """
    try:
        import keyring  # type: ignore
        backend = keyring.get_keyring()
        log.debug(f"keyring backend: {backend.__class__.__name__}")
        # On headless Linux, detect the null/fail backend and opt into
        # keyrings.alt's EncryptedFile if available.
        if "fail" in backend.__class__.__name__.lower() or "null" in backend.__class__.__name__.lower():
            try:
                from keyrings.alt.file import EncryptedKeyring  # type: ignore
                ek = EncryptedKeyring()
                # Key file in user home so root/user separation matters.
                ek.file_path = os.path.expanduser("~/.polymarket-bot-keyring.cfg")
                keyring.set_keyring(ek)
                log.info("Using EncryptedKeyring file backend (headless fallback).")
            except Exception:  # noqa: BLE001
                log.warning(
                    "No usable keyring backend. Install `keyrings.alt` or run on a "
                    "system with Keychain/Secret Service for secure key storage."
                )
        return keyring
    except ImportError:
        raise SignerError("`keyring` package is not installed. pip install keyring")
