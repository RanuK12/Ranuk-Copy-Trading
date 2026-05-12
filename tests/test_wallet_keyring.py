"""Tests for Tier-1 encrypted-key wallet (SecureKey).

We use a fake in-memory keyring so the test never touches the real OS
keychain.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.wallet.base import SignerError
from bot.wallet.secure_key import SecureKey


class FakeKeyring:
    """In-memory substitute for the `keyring` module."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self._store.get((service, username))

    def set_password(self, service, username, value):
        self._store[(service, username)] = value

    def delete_password(self, service, username):
        self._store.pop((service, username), None)


# A deterministic 32-byte hex private key for tests.
_TEST_PK = "0x" + "11" * 32


@pytest.fixture
def fake_kr():
    return FakeKeyring()


def test_setup_then_load_roundtrip(fake_kr):
    SecureKey.setup(_TEST_PK, "correct-horse", keyring_mod=fake_kr)

    # Address is visible without the password.
    addr = SecureKey.stored_address(keyring_mod=fake_kr)
    assert addr is not None
    assert addr.startswith("0x")

    sk = SecureKey.load("correct-horse", keyring_mod=fake_kr)
    assert sk.private_key() == _TEST_PK
    assert sk.address() == addr


def test_load_with_wrong_password_raises(fake_kr):
    SecureKey.setup(_TEST_PK, "right-password", keyring_mod=fake_kr)
    with pytest.raises(SignerError, match="Wrong password"):
        SecureKey.load("nope-wrong", keyring_mod=fake_kr)


def test_load_without_stored_key_raises(fake_kr):
    with pytest.raises(SignerError, match="No encrypted key"):
        SecureKey.load("anything", keyring_mod=fake_kr)


def test_setup_rejects_short_password(fake_kr):
    with pytest.raises(SignerError, match="at least 8"):
        SecureKey.setup(_TEST_PK, "short", keyring_mod=fake_kr)


def test_setup_rejects_malformed_key(fake_kr):
    with pytest.raises(SignerError, match="is not 64"):
        SecureKey.setup("0xabc", "correct-horse", keyring_mod=fake_kr)


def test_delete_removes_key(fake_kr):
    SecureKey.setup(_TEST_PK, "correct-horse", keyring_mod=fake_kr)
    assert SecureKey.stored_address(keyring_mod=fake_kr) is not None

    SecureKey.delete(keyring_mod=fake_kr)
    assert SecureKey.stored_address(keyring_mod=fake_kr) is None


def test_different_passwords_produce_different_ciphertexts(fake_kr):
    kr1 = FakeKeyring()
    kr2 = FakeKeyring()
    SecureKey.setup(_TEST_PK, "password-one", keyring_mod=kr1)
    SecureKey.setup(_TEST_PK, "password-two", keyring_mod=kr2)
    raw1 = kr1.get_password("polymarket-bot", "private_key_v1")
    raw2 = kr2.get_password("polymarket-bot", "private_key_v1")
    assert raw1 != raw2  # different passwords -> different ciphertext
