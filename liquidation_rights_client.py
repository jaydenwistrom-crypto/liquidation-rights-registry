"""
liquidation_rights_client.py — Python client for LiquidationRightsRegistry.

Used by JIT executors to:
  - register rights on a borrower before firing
  - record execution after a successful liquidation
  - check whether another party holds active rights on a target

The client is non-blocking: all failures are logged but never raise, so a
registry RPC issue never stops an actual liquidation from firing.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("rights_client")

# ── Config ────────────────────────────────────────────────────────────────────

REGISTRY_ADDR = os.environ.get("LIQUIDATION_RIGHTS_REGISTRY", "")
RPC_URL       = os.environ.get("BASE_MAINNET_RPC_URL", "http://127.0.0.1:8545")
PRIV_KEY      = os.environ.get("VAULT_PRIVATE_KEY", "")
LOGS_DIR      = BASE_DIR / "logs"
SIGNER_LOCK   = LOGS_DIR / "base_8453_signer.lock"

MIN_STAKE_WEI = int(0.005e18)   # matches contract MIN_STAKE

# ── ABI ───────────────────────────────────────────────────────────────────────

_REGISTRY_ABI = [
    {
        "name": "register",
        "type": "function",
        "stateMutability": "payable",
        "inputs":  [{"name": "borrower", "type": "address"}],
        "outputs": [],
    },
    {
        "name": "recordExecution",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs":  [{"name": "borrower", "type": "address"}],
        "outputs": [],
    },
    {
        "name": "hasActiveRights",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [
            {"name": "borrower",    "type": "address"},
            {"name": "liquidator",  "type": "address"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "getRights",
        "type": "function",
        "stateMutability": "view",
        "inputs":  [{"name": "borrower", "type": "address"}],
        "outputs": [
            {"name": "liquidator", "type": "address"},
            {"name": "stake",      "type": "uint256"},
            {"name": "expiresAt",  "type": "uint256"},
            {"name": "executed",   "type": "bool"},
            {"name": "active",     "type": "bool"},
        ],
    },
]

# ── Client ────────────────────────────────────────────────────────────────────

class LiquidationRightsClient:
    """
    Thin wrapper around LiquidationRightsRegistry.

    All write methods are no-ops when LIQUIDATION_RIGHTS_REGISTRY is unset
    (contract not yet deployed), so executors work normally before deployment.
    """

    def __init__(self) -> None:
        self._enabled = bool(REGISTRY_ADDR and PRIV_KEY)
        if not self._enabled:
            log.debug("LiquidationRightsClient disabled (LIQUIDATION_RIGHTS_REGISTRY not set)")
            return

        self._w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 10}))
        self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._account = self._w3.eth.account.from_key(PRIV_KEY)
        self._owner   = self._account.address
        self._contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(REGISTRY_ADDR),
            abi=_REGISTRY_ABI,
        )

    # ── Read ──────────────────────────────────────────────────────────────────

    def other_holds_rights(self, borrower: str) -> bool:
        """
        Returns True if another liquidator has active rights on this borrower.
        We should back off if so (respect coordination).
        Returns False on any RPC error (fail open — never block a liquidation
        due to registry unavailability).
        """
        if not self._enabled:
            return False
        try:
            liq, _, _, _, active = self._contract.functions.getRights(
                Web3.to_checksum_address(borrower)
            ).call()
            return active and liq.lower() != self._owner.lower()
        except Exception as exc:
            log.warning(f"rights check failed for {borrower[:10]}…: {exc}")
            return False

    def we_hold_rights(self, borrower: str) -> bool:
        """Returns True if we hold active rights on this borrower."""
        if not self._enabled:
            return False
        try:
            return self._contract.functions.hasActiveRights(
                Web3.to_checksum_address(borrower),
                self._owner,
            ).call()
        except Exception as exc:
            log.warning(f"hasActiveRights failed for {borrower[:10]}…: {exc}")
            return False

    # ── Write ─────────────────────────────────────────────────────────────────

    def register(self, borrower: str) -> Optional[str]:
        """
        Stake MIN_STAKE_WEI ETH to claim rights on `borrower`.
        Returns tx hash or None on failure.
        """
        if not self._enabled:
            return None
        try:
            borrower_addr = Web3.to_checksum_address(borrower)
            liq, stake, _, _, active = self._contract.functions.getRights(
                borrower_addr
            ).call()
            if active:
                if liq.lower() == self._owner.lower():
                    log.info(f"rights already active  borrower={borrower[:10]}…  stake={stake}")
                    return "already-active"
                log.info(f"rights held by another liquidator  borrower={borrower[:10]}…")
                return None

            with open(SIGNER_LOCK, "w") as _lf:
                fcntl.flock(_lf, fcntl.LOCK_EX)
                try:
                    base_fee = self._w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
                    tx = self._contract.functions.register(
                        borrower_addr
                    ).build_transaction({
                        "chainId":              8453,
                        "gas":                  80_000,
                        "maxFeePerGas":         base_fee * 2 + 50_000_000,
                        "maxPriorityFeePerGas": 50_000_000,  # flat 0.05 gwei for registry tx
                        "nonce":                self._w3.eth.get_transaction_count(self._owner, "pending"),
                        "from":                 self._owner,
                        "value":                MIN_STAKE_WEI,
                    })
                    signed  = self._account.sign_transaction(tx)
                    raw     = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
                    tx_hash = self._w3.eth.send_raw_transaction(raw)
                    h = self._w3.to_hex(tx_hash)
                finally:
                    fcntl.flock(_lf, fcntl.LOCK_UN)

            log.info(f"registered rights  borrower={borrower[:10]}…  tx={h}")
            return h
        except Exception as exc:
            log.warning(f"register failed for {borrower[:10]}…: {exc}")
            return None

    def record_execution(self, borrower: str, max_attempts: int = 3) -> Optional[str]:
        """
        Record a successful liquidation and reclaim the 0.005 ETH stake.
        Waits for receipt and retries up to max_attempts times on revert.
        Checks on-chain state before each attempt to skip if already recorded.
        Returns confirmed tx hash or None if all attempts failed.
        """
        if not self._enabled:
            return None

        borrower_addr = Web3.to_checksum_address(borrower)

        for attempt in range(1, max_attempts + 1):
            # Pre-check: skip if already executed or no active rights (nothing to reclaim)
            try:
                _, _, _, executed, active = self._contract.functions.getRights(borrower_addr).call()
                if executed:
                    log.info(f"recordExecution already confirmed on-chain  borrower={borrower[:10]}…")
                    return "already-executed"
                if not active:
                    log.warning(f"recordExecution: no active rights  borrower={borrower[:10]}… — stake unrecoverable")
                    return None
            except Exception as exc:
                log.warning(f"getRights pre-check failed (attempt {attempt})  borrower={borrower[:10]}…: {exc}")

            try:
                with open(SIGNER_LOCK, "w") as _lf:
                    fcntl.flock(_lf, fcntl.LOCK_EX)
                    try:
                        base_fee = self._w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
                        tip      = max(base_fee, 50_000_000)  # at least 0.05 gwei
                        tx = self._contract.functions.recordExecution(borrower_addr).build_transaction({
                            "chainId":              8453,
                            "gas":                  80_000,
                            "maxFeePerGas":         base_fee * 2 + tip,
                            "maxPriorityFeePerGas": tip,
                            "nonce":                self._w3.eth.get_transaction_count(self._owner, "pending"),
                            "from":                 self._owner,
                        })
                        signed  = self._account.sign_transaction(tx)
                        raw     = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
                        tx_hash = self._w3.eth.send_raw_transaction(raw)
                        h = self._w3.to_hex(tx_hash)
                    finally:
                        fcntl.flock(_lf, fcntl.LOCK_UN)

                receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
                if receipt["status"] == 1:
                    log.info(f"recordExecution confirmed  borrower={borrower[:10]}…  tx={h}  gas={receipt['gasUsed']}")
                    return h
                else:
                    log.warning(f"recordExecution REVERTED  attempt={attempt}/{max_attempts}  borrower={borrower[:10]}…  tx={h}")

            except Exception as exc:
                log.warning(f"recordExecution error  attempt={attempt}/{max_attempts}  borrower={borrower[:10]}…: {exc}")

            if attempt < max_attempts:
                import time as _time
                _time.sleep(3)

        log.error(f"recordExecution FAILED all {max_attempts} attempts  borrower={borrower[:10]}… — STAKE AT RISK")
        return None


# ── Module-level singleton ────────────────────────────────────────────────────

_client: Optional[LiquidationRightsClient] = None


def get_client() -> LiquidationRightsClient:
    global _client
    if _client is None:
        _client = LiquidationRightsClient()
    return _client
