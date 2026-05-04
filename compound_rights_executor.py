"""
compound_rights_executor.py — Executes Compound v3 absorptions for registered positions.

Reads compound_scanner_state.json to find positions where:
  - We hold active rights (LiquidationRightsRegistryV2)
  - isLiquidatable() == True (position is absorb-ready)

When both conditions are true:
  1. Calls Comet.absorb(owner, [borrower]) — writes off bad debt; seized collateral
     enters the protocol's own reserve (available for purchase via buyCollateral()).
  2. On success, calls LiquidationRightsRegistry.recordExecution() to reclaim stake.

Unlike Morpho, Compound v3 absorptions require zero capital from the caller.
Seized collateral can later be purchased at a discount via Comet.buyCollateral().

Comet markets on Base:
  cUSDbCv3  0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf
  cWETHv3   0x46e6b214b524310239732D51387075E0e70970bf

Usage:
    venv/bin/python3 compound_rights_executor.py
    venv/bin/python3 compound_rights_executor.py --dry-run
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from log_rotation import rotating_file_handler
import liquidation_rights_client as lrc

# ── Bootstrap ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

EXEC_LOCK      = LOGS_DIR / "compound_rights_executor.lock"
STATE_FILE     = LOGS_DIR / "compound_scanner_state.json"
POSITIONS_FILE = LOGS_DIR / "compound_positions.json"
SIGNER_LOCK    = LOGS_DIR / "base_8453_signer.lock"

log = logging.getLogger("compound_exec")
log.setLevel(logging.DEBUG)
log.addHandler(rotating_file_handler(LOGS_DIR / "compound_rights_executor.log"))
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Config ────────────────────────────────────────────────────────────────────

RPC_URL       = os.environ.get("BASE_MAINNET_RPC_URL", "https://mainnet.base.org")
PRIV_KEY      = os.environ.get("VAULT_PRIVATE_KEY", "")
POLL_INTERVAL = int(os.environ.get("COMPOUND_EXEC_POLL_INTERVAL", "15"))
MAX_BATCH     = int(os.environ.get("COMPOUND_EXEC_MAX_BATCH", "5"))  # borrowers per absorb() call

# ── ABIs ──────────────────────────────────────────────────────────────────────

_COMET_ABI = [
    {
        "name": "absorb", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "absorber",  "type": "address"},
            {"name": "accounts",  "type": "address[]"},
        ],
        "outputs": [],
    },
    {
        "name": "isLiquidatable", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "borrowBalanceOf", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    # buyCollateral — for future profit sweep
    {
        "name": "buyCollateral", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset",     "type": "address"},
            {"name": "minAmount", "type": "uint256"},
            {"name": "baseAmount","type": "uint256"},
            {"name": "recipient", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "quoteCollateral", "type": "function", "stateMutability": "view",
        "inputs": [
            {"name": "asset",      "type": "address"},
            {"name": "baseAmount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ── Singleton lock ─────────────────────────────────────────────────────────────

def _acquire_lock() -> None:
    _lf = open(EXEC_LOCK, "w")
    try:
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another compound_rights_executor is running ({EXEC_LOCK}). Exiting.")

_acquire_lock()

# ── Args ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser()
_parser.add_argument("--dry-run", action="store_true")
_args = _parser.parse_args()
DRY_RUN = _args.dry_run or os.environ.get("COMPOUND_EXEC_DRY_RUN", "").lower() in ("1", "true")

if DRY_RUN:
    log.info("[DRY-RUN] mode active — absorb txs will be simulated only")

# ── Web3 setup ────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not w3.is_connected():
    sys.exit("Cannot connect to Base RPC")

account = w3.eth.account.from_key(PRIV_KEY)
OWNER   = account.address

rights = lrc.get_client()

# Comet contract instances keyed by checksum address
_comet_contracts: dict[str, object] = {}

def _get_comet(addr: str):
    cs = Web3.to_checksum_address(addr)
    if cs not in _comet_contracts:
        _comet_contracts[cs] = w3.eth.contract(address=cs, abi=_COMET_ABI)
    return _comet_contracts[cs]


log.info("=" * 60)
log.info("compound_rights_executor starting")
log.info("owner      : %s", OWNER)
log.info("dry_run    : %s", DRY_RUN)
log.info("=" * 60)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_scanner_state() -> list[dict]:
    """Return watchlist entries from compound_scanner_state.json."""
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text()).get("watchlist", [])
    except Exception:
        return []


def _is_liquidatable_live(comet_addr: str, borrower: str) -> bool:
    """Re-check isLiquidatable() on-chain before committing gas."""
    try:
        return _get_comet(comet_addr).functions.isLiquidatable(
            Web3.to_checksum_address(borrower)
        ).call()
    except Exception as exc:
        log.debug("isLiquidatable check failed %s: %s", borrower[:12], exc)
        return False


def _absorb_batch(comet_addr: str, borrowers: list[str]) -> Optional[str]:
    """
    Call Comet.absorb(owner, borrowers).
    Returns tx hash hex on success, None on failure.
    """
    comet = _get_comet(comet_addr)
    cs_borrowers = [Web3.to_checksum_address(b) for b in borrowers]

    if DRY_RUN:
        log.info("[DRY-RUN] would absorb  comet=%s  accounts=%s",
                 comet_addr[:12], [b[:12] for b in cs_borrowers])
        return "0xdryrun"

    try:
        with open(SIGNER_LOCK, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                base_fee  = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
                tip       = max(base_fee * 2, 100_000_000)
                gas_est   = comet.functions.absorb(OWNER, cs_borrowers).estimate_gas({"from": OWNER})
                gas_limit = int(gas_est * 1.30)

                tx = comet.functions.absorb(OWNER, cs_borrowers).build_transaction({
                    "chainId":              8453,
                    "gas":                  gas_limit,
                    "maxFeePerGas":         base_fee * 2 + tip,
                    "maxPriorityFeePerGas": tip,
                    "nonce":                w3.eth.get_transaction_count(OWNER, "pending"),
                    "from":                 OWNER,
                })
                signed  = account.sign_transaction(tx)
                raw     = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
                tx_hash = w3.eth.send_raw_transaction(raw)
            finally:
                fcntl.flock(_lf, fcntl.LOCK_UN)

        tx_hex = w3.to_hex(tx_hash)
        log.info("absorb tx sent  comet=%s  accounts=%d  tx=%s",
                 comet_addr[:12], len(cs_borrowers), tx_hex)

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt["status"] != 1:
            log.warning("absorb REVERTED  comet=%s  tx=%s", comet_addr[:12], tx_hex)
            return None

        log.info("absorb SUCCESS  comet=%s  accounts=%d  gas=%d  block=%d",
                 comet_addr[:12], len(cs_borrowers), receipt["gasUsed"], receipt["blockNumber"])
        return tx_hex

    except Exception as exc:
        log.warning("absorb failed  comet=%s: %s", comet_addr[:12], exc)
        return None


# ── Main scan loop ─────────────────────────────────────────────────────────────

def _scan_and_execute() -> None:
    """One scan cycle: find registered+liquidatable positions and absorb them."""
    watchlist = _read_scanner_state()
    if not watchlist:
        return

    # Group candidates by comet address for batching
    by_comet: dict[str, list[dict]] = {}
    for entry in watchlist:
        if not entry.get("is_registered"):
            continue
        borrower = entry.get("borrower", "")
        comet    = entry.get("comet_address", "")
        if not borrower or not comet:
            continue

        if not rights.we_hold_rights(borrower):
            continue

        if not entry.get("is_liquidatable"):
            continue

        by_comet.setdefault(comet.lower(), []).append(entry)

    if not by_comet:
        return

    executed_total = 0

    for comet_lower, candidates in by_comet.items():
        comet_addr = Web3.to_checksum_address(comet_lower)

        # Live-filter: only absorb those still liquidatable on-chain
        ready: list[dict] = []
        for entry in candidates:
            if _is_liquidatable_live(comet_addr, entry["borrower"]):
                ready.append(entry)
            else:
                log.info("position recovered before execution  borrower=%s",
                         entry["borrower"][:12])

        if not ready:
            continue

        log.info("execution candidates  comet=%s  count=%d", comet_addr[:12], len(ready))

        # Process in batches of MAX_BATCH
        for i in range(0, len(ready), MAX_BATCH):
            batch  = ready[i : i + MAX_BATCH]
            addrs  = [e["borrower"] for e in batch]

            tx_hex = _absorb_batch(comet_addr, addrs)
            if not tx_hex:
                continue

            # Record execution for each successfully absorbed borrower
            for entry in batch:
                borrower = entry["borrower"]
                record_tx = rights.record_execution(borrower)
                if record_tx:
                    log.info("recordExecution  borrower=%s  tx=%s", borrower[:12], record_tx)
                else:
                    log.warning("recordExecution failed  borrower=%s", borrower[:12])
                executed_total += 1

            time.sleep(2)  # brief pause between batches

    if executed_total > 0:
        log.info("cycle complete  executed=%d", executed_total)


def main() -> None:
    while True:
        try:
            _scan_and_execute()
        except Exception as exc:
            log.error("scan cycle error: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
