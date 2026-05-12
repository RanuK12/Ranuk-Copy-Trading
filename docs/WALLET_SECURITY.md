# Wallet Security Guide

Four tiers, one interface. Pick the tier that matches your capital and
your threat model, then wire it with `python main.py --setup-wallet`
(or the env vars described below). The bot's executor only sees a
`Signer` abstraction — it doesn't care whether the key came from disk,
the OS keychain, a Ledger, or AWS KMS.

---

## TL;DR — which tier should I use?

| Capital           | Recommended tier                 | Why |
|-------------------|----------------------------------|-----|
| $0 – $500         | **Tier 1** (keyring + Fernet)    | Simple, safe enough, works on VPS and laptops. |
| $500 – $5,000     | **Tier 1** with a strong password | Raise the password strength rather than the tier. |
| $5,000 – $50,000  | **Tier 2** (hardware wallet) for cold strategies + **Tier 1** hot wallet for arbitrage | Mixes manual-confirm custody with fast signing. |
| $50,000+          | **Tier 4** (Cloud KMS) or multi-sig | Production-grade HSM custody, requires ops setup. |

You can mix tiers via Tier 3 "strategy-assigned" mode: arbitrage uses
the hot Tier 1 key, tail-end uses a cold key, smart-copy uses a third.

---

## Why not just .env?

The `POLY_PRIVATE_KEY=...` pattern the v2 release used is still
supported for backward compatibility, but it has three problems:

1. **Plaintext at rest.** Anyone with shell access or a backup of
   `/projects/` can grab the key.
2. **Leaks into logs.** Shell rcfiles, docker history, `env | grep`,
   `ps -eaf` all expose it. One accidental `git add .env` and it's on
   GitHub forever.
3. **No password layer.** If the disk is stolen, there's nothing
   between the attacker and your funds.

Tier 1 solves all three: the key lives in the OS keyring (encrypted
by the OS) **and** is re-wrapped with Fernet using a password you
provide at startup. Even a full disk image doesn't give the attacker
the key without the password.

---

## Tier 1 — Encrypted key in the OS keyring (recommended default)

**How it works**

1. The setup wizard asks for your private key and a password.
2. The key is wrapped with `Fernet` using a 390,000-iteration PBKDF2
   derived from the password + a random 16-byte salt.
3. The ciphertext + salt + derived address are stored in the OS keyring
   under service `polymarket-bot`:
     * macOS → Keychain
     * Windows → Credential Manager
     * Linux desktop → Secret Service (libsecret)
     * Linux headless → falls back to `~/.polymarket-bot-keyring.cfg`
       via `keyrings.alt.EncryptedKeyring`

At startup, `bot/wallet/resolver.py` looks in the keyring first; if a
key is present, it asks for the password (via `WALLET_PASSWORD` or
interactive `getpass`), decrypts, and uses the key for the session.

**Setup**

```bash
python main.py --setup-wallet
# choose option 1 (Tier 1)
# paste your EOA private key (hidden input)
# create a password (>=8 chars, confirmed)
# -> "✅ Key encrypted and stored in the OS keyring."
```

**Run**

```bash
# Interactive
python main.py

# Non-interactive (systemd, PM2, docker)
WALLET_PASSWORD=my-strong-password python main.py
```

Remove `POLY_PRIVATE_KEY=` from `.env` after setup.

**Threats it mitigates**

* Casual file snooping (`.env` leak).
* Laptop theft without OS password.
* Accidental commit of `.env`.
* `ps`/`env` grepping by other shell users.

**Threats it does NOT mitigate**

* Full OS compromise (root on your machine).
* Malware running as your user that can wait for you to type the password.
* Supply-chain attacks on the `keyring` or `cryptography` libraries.

If those are in your threat model, move to Tier 2 or Tier 4.

---

## Tier 2 — Hardware wallet (Ledger / Trezor)

**Status:** scaffold (`bot/wallet/hardware_wallet.py`). Wired so you
can drop in provider-specific signing; not implemented out of the box.

**When to use**

* You hold **>$5,000** in Polymarket at any time.
* You run strategies that don't need sub-second signing
  (tail-end, smart-copy, sniper). Hardware wallets require you to
  **physically confirm each transaction** on the device — that's
  incompatible with arbitrage and micro-spread.

**Recommended pattern**

1. Use Tier 2 for `tail_end`, `smart_copy`.
2. Use Tier 1 for `arbitrage`, `micro_spread`, `dip_arb`.
3. Wire them together via Tier 3 strategy-assigned mode (see below).

**Wiring (you implement)**

`bot/wallet/hardware_wallet.py` has the stub class. Replace the body of
`sign_transaction` with a call to `ledgereth.transactions.sign_transaction`
(Ledger) or `trezorlib.ethereum.sign_tx` (Trezor). The bot never sees
the raw key — the device signs the transaction hash and returns `(r, s, v)`.

---

## Tier 3 — Multi-wallet rotation

**When to use**

* You want to separate **hot capital** (arbitrage, dip_arb, micro_spread)
  from **cold capital** (tail_end, smart_copy) so a compromise in one
  wallet doesn't drain the other.
* You run volume high enough that Polymarket flags one wallet and you
  need the others to keep operating.

**Modes**

* `WALLET_MODE=rotation` — round-robin across all configured slots.
  Trades are distributed evenly.
* `WALLET_MODE=strategy-assigned` — each strategy pins to one slot.

**Setup**

```bash
python main.py --setup-wallet
# choose option 2 (Tier 3)
# enter password (shared across slots)
# add slots: "hot", "cold", "arb_fast" ... (empty to finish)
```

This writes each slot to the keyring under username
`private_key_slot__<slot_id>` and tracks them in a `slot_manifest` key.

**.env for rotation**

```
WALLET_MODE=rotation
WALLET_ROTATION_KEYS=hot,cold,arb_fast
WALLET_PASSWORD=shared-password-for-all-slots
```

**.env for strategy-assigned**

```
WALLET_MODE=strategy-assigned
WALLET_ARBITRAGE=hot
WALLET_TAIL_END=cold
WALLET_SMART_COPY=cold
```

Each strategy's `pick_for_strategy()` call picks the right slot at
order time.

---

## Tier 4 — Cloud KMS / HashiCorp Vault

**Status:** scaffold (`bot/wallet/cloud_kms.py`). Real provider
integration is provider-specific and out of scope for this release.

**When to use**

* You run the bot on cloud infrastructure (AWS, GCP) where you don't
  trust the disk to hold a private key even encrypted.
* You want auditability: every signature is logged in CloudTrail /
  the Vault audit backend.
* You need **key rotation** and **role-based access control** for
  different operators.

**Wiring (you implement)**

The pattern is the same for AWS KMS, GCP Cloud KMS, and HashiCorp
Vault Transit:

1. Create the key in the KMS/Vault with ECDSA secp256k1 (Ethereum curve).
2. Export the **public** key and derive the Ethereum address client-side.
3. For each order, hash the EIP-712 typed data locally, then call the
   KMS Sign API with the digest.
4. Recover the signature and submit.

The raw key never leaves the HSM.

**Concrete AWS KMS example**

```python
# bot/wallet/cloud_kms.py — fill in CloudKMSSigner.sign_hash
import boto3

client = boto3.client("kms")

def sign_hash(self, digest: bytes) -> bytes:
    resp = client.sign(
        KeyId=self._key_id,
        Message=digest,
        MessageType="DIGEST",
        SigningAlgorithm="ECDSA_SHA_256",
    )
    return resp["Signature"]
```

Parsing the DER-encoded signature and computing `v` is identical to
what `eth_account` does internally; see the AWS KMS Ethereum
integration pattern in the KMS documentation.

---

## Threat model cheat-sheet

| Threat                              | Tier 0 .env | Tier 1 keyring | Tier 2 HW | Tier 3 multi | Tier 4 KMS |
|-------------------------------------|:-----------:|:--------------:|:---------:|:------------:|:----------:|
| `.env` leak / git commit            | ❌          | ✅             | ✅        | ✅           | ✅         |
| Laptop stolen (disk encrypted)      | ❌          | ✅             | ✅        | ✅           | ✅         |
| Laptop stolen (disk plain)          | ❌          | ⚠️             | ✅        | ⚠️           | ✅         |
| Root on the server                  | ❌          | ❌             | ✅        | ❌           | ✅         |
| One wallet flagged / frozen         | ❌          | ❌             | ❌        | ✅           | ⚠️         |
| Malware intercepts password         | ❌          | ❌             | ✅        | ❌           | ✅         |
| Accidental wrong-network deposit    | ❌          | ❌             | ❌        | ❌           | ❌         |

(The last one is operator error and no tier prevents it. Always test
with $10 first.)

---

## Operational checklist

- [ ] `.env` is in `.gitignore` (`git status` must not show it).
- [ ] Backup of the Tier 1 password in a password manager (1Password,
      Bitwarden). Without it your key is permanently lost.
- [ ] The address in `POLY_FUNDER` is your **proxy** wallet, not the EOA.
      See `docs/CONECTAR_WALLET.md`.
- [ ] The signing wallet has 1-2 MATIC for gas.
- [ ] You've tested `--setup-wallet` on a throwaway key first.
- [ ] You've read `docs/LOW_BUDGET_GUIDE.md` if your capital is under $300.

---

## FAQ

**Can I migrate from Tier 0 to Tier 1 without losing state?**
Yes. Run `python main.py --setup-wallet`, choose Tier 1, paste the same
key that's currently in `POLY_PRIVATE_KEY`. Then delete that line from
`.env`. Your state, positions, and wallet history are unchanged.

**What happens if I forget the Tier 1 password?**
The bot can't recover it. Delete the stored key
(`python main.py --setup-wallet` → option 5) and re-import from your
offline backup of the raw private key.

**Can I use a hardware wallet for arbitrage?**
No — the device requires manual confirmation. Arbitrage opportunities
last a few seconds. Use Tier 1 for any strategy that needs speed.

**Does the bot ever send my private key over the network?**
No. All signing happens locally (Tier 0/1/3) or on the device (Tier 2/4).
Only the signed order payload leaves the process.

**Can I share the same encrypted key across machines?**
Yes — export it from the keyring of the first machine, transfer it
securely (e.g. over SSH to a password-manager-backed file), and import
via `--setup-wallet`. Each machine still needs the password.
