"""CLOB V2 credential helper.

``py-clob-client-v2`` has a bug creating API keys for ``POLY_1271`` (deposit-wallet)
users — the L1 header it builds is rejected with *"Could not create api key"*.
This module replicates the correct Polynode signing flow so the bot can
authenticate on V2 regardless of wallet type.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import requests

from bot.config import CFG
from bot.logger import get_logger

log = get_logger("clob_v2_auth")

_CLOB_AUTH_TYPES = {
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "uint256"},
        {"name": "message", "type": "string"},
    ],
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
}

_CLOB_AUTH_DOMAIN = {
    "name": "ClobAuthDomain",
    "version": "1",
    "chainId": 137,
}

_CLOB_AUTH_MSG = "This message attests that I control the given wallet"


def _sign_l1(private_key: str, timestamp: int, nonce: int = 0) -> tuple[str, str]:
    """Return (eoa_address, signature) for the ClobAuth EIP-712 message."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    account = Account.from_key(private_key if private_key.startswith("0x") else f"0x{private_key}")
    full_message = {
        "domain": _CLOB_AUTH_DOMAIN,
        "types": _CLOB_AUTH_TYPES,
        "primaryType": "ClobAuth",
        "message": {
            "address": account.address,
            "timestamp": str(timestamp),
            "nonce": nonce,
            "message": _CLOB_AUTH_MSG,
        },
    }
    signed = account.sign_message(encode_typed_data(full_message=full_message))
    return account.address, signed.signature.hex()


def _l1_headers(private_key: str, timestamp: Optional[int] = None) -> dict[str, str]:
    ts = timestamp if timestamp is not None else int(time.time())
    address, signature = _sign_l1(private_key, ts)
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(ts),
        "POLY_NONCE": "0",
    }


def _credential_path() -> Path:
    return Path(CFG.state_file).parent / "clob_credentials.json"


def load_credentials() -> Optional[dict[str, str]]:
    """Load cached CLOB credentials from disk."""
    path = _credential_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if all(k in data for k in ("api_key", "api_secret", "api_passphrase")):
            return data
    except Exception:
        pass
    return None


def save_credentials(creds: dict[str, str]) -> None:
    """Persist CLOB credentials to disk."""
    _credential_path().write_text(json.dumps(creds, indent=2))


def create_or_derive_credentials(
    private_key: str,
    host: str = "https://clob.polymarket.com",
) -> dict[str, str]:
    """Create (or derive) CLOB API credentials using the Polynode L1 flow.

    Falls back to deriving an existing key if creation fails.
    """
    headers = {
        **_l1_headers(private_key),
        "Accept": "*/*",
        "Content-Type": "application/json",
    }

    # Try create first
    try:
        resp = requests.post(f"{host}/auth/api-key", headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            creds = {
                "api_key": data.get("apiKey") or data.get("key", ""),
                "api_secret": data["secret"],
                "api_passphrase": data["passphrase"],
            }
            save_credentials(creds)
            log.info("CLOB API credentials created.")
            return creds
    except Exception as e:
        log.debug(f"Create API key failed: {e}")

    # Fallback: derive existing
    headers = {
        **_l1_headers(private_key),
        "Accept": "*/*",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.get(f"{host}/auth/derive-api-key", headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        creds = {
            "api_key": data.get("apiKey") or data.get("key", ""),
            "api_secret": data["secret"],
            "api_passphrase": data["passphrase"],
        }
        save_credentials(creds)
        log.info("CLOB API credentials derived.")
        return creds
    except Exception as e:
        raise RuntimeError(f"Failed to create/derive CLOB credentials: {e}")


def get_or_create_credentials(
    private_key: str,
    host: str = "https://clob.polymarket.com",
) -> dict[str, str]:
    """Return cached credentials if valid, otherwise create new ones."""
    cached = load_credentials()
    if cached:
        return cached
    return create_or_derive_credentials(private_key, host)
