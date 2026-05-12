"""Tier 4 — Cloud KMS / Vault signer.

Status: **stub**. Production integrations vary by provider (AWS KMS, GCP
Cloud KMS, HashiCorp Vault Transit). Common pattern:

1. The raw private key NEVER leaves the HSM / KMS.
2. The bot prepares a transaction hash.
3. The bot calls the KMS sign API with the hash.
4. The KMS returns the signature (r, s, v) without exposing the key.

To enable this in production, implement the provider-specific client in
a subclass and wire it via WALLET_MODE=cloud_kms. For AWS, this is
``boto3.client('kms').sign(KeyId=..., Message=..., MessageType='DIGEST',
SigningAlgorithm='ECDSA_SHA_256')``.

This module is a scaffold; the rest of the bot works without it.
"""

from __future__ import annotations

from bot.logger import get_logger
from bot.wallet.base import Signer, SignerError

log = get_logger("wallet.kms")


class CloudKMSSigner(Signer):
    tier = "cloud_kms"

    def __init__(self, *, key_id: str, provider: str = "aws") -> None:
        self._key_id = key_id
        self._provider = provider
        log.warning(
            "Tier 4 Cloud KMS signer is a stub. "
            "Implement provider-specific signing before use."
        )

    def supports_raw_key(self) -> bool:
        return False

    def address(self) -> str:
        raise SignerError(
            "CloudKMSSigner.address needs the KMS public key endpoint. "
            "Implement in a provider-specific subclass."
        )

    def private_key(self) -> str:
        raise SignerError(
            "Cloud KMS never exposes the private key. Call sign_hash() instead."
        )

    async def sign_hash(self, digest: bytes) -> bytes:
        raise SignerError(
            "CloudKMSSigner.sign_hash is not implemented. See comments in "
            "bot/wallet/cloud_kms.py for the integration outline."
        )
