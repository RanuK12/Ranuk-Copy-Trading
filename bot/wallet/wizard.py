"""Interactive wallet setup wizard — invoked via `python main.py --setup-wallet`.

Covers:
* Tier 1 single-slot encrypted key (default).
* Tier 3 multi-slot setup (add/remove/list slots).
* Tier 2 / Tier 4 info prompts (not yet implemented — points at docs).
"""

from __future__ import annotations

import getpass
import sys
from typing import Optional

from bot.logger import CONSOLE, get_logger
from bot.wallet.base import SignerError
from bot.wallet.secure_key import SecureKey, _address_from_private_key
from bot.wallet.multi_wallet import _SlotBackedSecureKey, list_slots

log = get_logger("wallet.wizard")


def _prompt(text: str) -> str:
    CONSOLE.print(f"[cyan]{text}[/]", end="")
    return input(" ").strip()


def _prompt_password(confirm: bool = True) -> str:
    pw = getpass.getpass("🛡️  Create a password (min 8 chars): ")
    if len(pw) < 8:
        raise SystemExit("Password too short (min 8 chars).")
    if confirm:
        pw2 = getpass.getpass("🛡️  Confirm password: ")
        if pw != pw2:
            raise SystemExit("Passwords do not match.")
    return pw


def run(argv: Optional[list[str]] = None) -> int:
    """Entry point called from main.py."""
    CONSOLE.rule("[bold]Polymarket Bot — Wallet Setup Wizard[/]")
    CONSOLE.print(
        "\n  [bold]1[/] 💾 Local Tier 1: encrypted single-slot (recommended < $5k)\n"
        "  [bold]2[/] 🔁 Local Tier 3: multi-slot rotation / strategy-assigned\n"
        "  [bold]3[/] 🔐 Hardware wallet (stub — see docs/WALLET_SECURITY.md)\n"
        "  [bold]4[/] 🏢 Cloud KMS (stub)\n"
        "  [bold]5[/] 🗑️ Delete the currently stored key\n"
        "  [bold]q[/] Quit\n"
    )
    choice = _prompt("Choose an option (1-5, q):").lower() or "1"
    if choice == "q":
        return 0
    if choice == "1":
        return _setup_tier1()
    if choice == "2":
        return _setup_tier3()
    if choice in {"3", "4"}:
        CONSOLE.print(
            "[yellow]Tier 2 / Tier 4 are stubs in this release. See "
            "[bold]docs/WALLET_SECURITY.md[/] for the wiring plan.[/]"
        )
        return 0
    if choice == "5":
        return _delete()
    CONSOLE.print("[red]Unknown option.[/]")
    return 1


# ---------------------------------------------------------------------------
def _setup_tier1() -> int:
    existing = SecureKey.stored_address()
    if existing:
        CONSOLE.print(
            f"[yellow]A Tier 1 key is already stored for address {existing}. "
            "Continuing will overwrite it.[/]"
        )
        if _prompt("Overwrite? (y/N):").lower() != "y":
            return 0

    CONSOLE.print(
        "\n  Paste your Polymarket EOA private key. It will be encrypted "
        "before being stored in your OS keyring. Input is hidden."
    )
    pk = getpass.getpass("🔑 Private key (0x...): ").strip()
    if not pk:
        raise SystemExit("No key provided.")

    try:
        # Sanity-derive the address before asking for a password
        addr = _address_from_private_key(pk if pk.startswith("0x") else "0x" + pk)
    except SignerError as e:
        raise SystemExit(f"Invalid key: {e}")

    CONSOLE.print(f"[green]Derived address:[/] {addr}")
    password = _prompt_password()
    SecureKey.setup(pk, password)
    CONSOLE.print(
        "\n[green]✅ Key encrypted and stored in the OS keyring.[/]\n"
        "   Next steps:\n"
        "     • Do NOT keep POLY_PRIVATE_KEY in .env anymore — delete that line.\n"
        "     • At bot startup you'll be asked for the password (or set\n"
        "       WALLET_PASSWORD in your shell / systemd unit).\n"
    )
    return 0


def _setup_tier3() -> int:
    CONSOLE.print("\n[bold]Tier 3 — Multi-slot setup[/]")
    existing = list_slots()
    if existing:
        CONSOLE.print(f"  Existing slots: {', '.join(existing)}")
    password = _prompt_password(confirm=not existing)

    while True:
        slot_id = _prompt(
            "\n  Slot id (e.g. 'hot', 'cold', 'arb_fast'), empty to finish:"
        ).strip().lower()
        if not slot_id:
            break
        if not slot_id.replace("_", "").isalnum():
            CONSOLE.print("[red]Slot id must be alphanumeric/underscore.[/]")
            continue
        pk = getpass.getpass(f"🔑 Private key for slot {slot_id!r} (0x...): ")
        try:
            addr = _SlotBackedSecureKey.setup(slot_id, pk, password)
        except SignerError as e:
            CONSOLE.print(f"[red]Slot setup failed:[/] {e}")
            continue
        CONSOLE.print(f"  [green]✅ slot '{slot_id}' -> {addr}[/]")

    CONSOLE.print(
        "\n[green]Done.[/] Now set in .env:\n"
        "    WALLET_MODE=rotation\n"
        "    WALLET_ROTATION_KEYS=slot1,slot2,slot3\n"
        "  — or —\n"
        "    WALLET_MODE=strategy-assigned\n"
        "    WALLET_ARBITRAGE=hot\n"
        "    WALLET_TAIL_END=cold\n"
    )
    return 0


def _delete() -> int:
    addr = SecureKey.stored_address()
    if not addr:
        CONSOLE.print("[grey]No single-slot Tier 1 key stored.[/]")
    else:
        if _prompt(f"Delete stored key for {addr}? (y/N):").lower() == "y":
            SecureKey.delete()
            CONSOLE.print("[green]Deleted.[/]")

    slots = list_slots()
    if slots:
        CONSOLE.print(
            f"[yellow]Tier 3 slots still present:[/] {', '.join(slots)}\n"
            "  Use the keyring CLI of your OS to delete individual slots."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
