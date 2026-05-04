"""
morpho_rights_executor.py — Executes Morpho Blue liquidations for registered positions.

Reads morpho_scanner_state.json to find positions where:
  - We hold active rights (LiquidationRightsRegistryV2)
  - HF < 1.0 (position is liquidatable right now)

When both conditions are true:
  1. Calls MorphoBlueRightsLiquidator.executeLiquidation()
  2. On success, calls LiquidationRightsRegistry.recordExecution() to reclaim stake

Swap routing (collateral → loan token):
  All dominant Base markets route collateral → USDC or WETH via Aerodrome Slipstream.
  Known tick spacings are hardcoded; unknown markets are skipped (stake forfeited to slasher).

Usage:
    venv/bin/python3 morpho_rights_executor.py
    venv/bin/python3 morpho_rights_executor.py --dry-run
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

EXEC_LOCK      = LOGS_DIR / "morpho_rights_executor.lock"
STATE_FILE     = LOGS_DIR / "morpho_scanner_state.json"
POSITIONS_FILE = LOGS_DIR / "morpho_positions.json"
SIGNER_LOCK    = LOGS_DIR / "base_8453_signer.lock"

log = logging.getLogger("morpho_exec")
log.setLevel(logging.DEBUG)
log.addHandler(rotating_file_handler(LOGS_DIR / "morpho_rights_executor.log"))
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Config ────────────────────────────────────────────────────────────────────

RPC_URL      = os.environ.get("BASE_MAINNET_RPC_URL", "https://mainnet.base.org")
PRIV_KEY     = os.environ.get("VAULT_PRIVATE_KEY", "")
LIQ_ADDR     = os.environ.get("MORPHO_BLUE_RIGHTS_LIQUIDATOR", "")
MORPHO_ADDR  = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")

POLL_INTERVAL = int(os.environ.get("MORPHO_EXEC_POLL_INTERVAL", "15"))
HF_EXEC_THRESHOLD = float(os.environ.get("MORPHO_EXEC_HF_THRESHOLD", "0.99"))
MIN_PROFIT_WEI = int(os.environ.get("MORPHO_EXEC_MIN_PROFIT_WEI", str(int(0.002e18))))  # ~$5

# ── Swap route table ──────────────────────────────────────────────────────────
#
# Maps (loanToken_lower, collateralToken_lower) → (routerType, tickSpacing, minOutBps)
# routerType: 0 = Aerodrome Slipstream, 1 = UniV3/AeroV2
# tickSpacing: Aerodrome CL tick spacing (routerType 0) or fee tier (routerType 1)
# minOutBps:   minimum output as basis points of theoretical (e.g. 9900 = 99%)
#
# Dominant Base Morpho markets (from scanner analysis):
#   USDC/cbBTC  86% LLTV — cbBTC→USDC via Aerodrome CL tick=1 (cbBTC/USDC pool)
#   USDC/WETH   86% LLTV — WETH→USDC via Aerodrome CL tick=1 (WETH/USDC pool)
#   USDC/cbXRP  62% LLTV — cbXRP→USDC via Aerodrome CL tick=200
#   USDC/cbDOGE 62% LLTV — cbDOGE→USDC via Aerodrome CL tick=200

USDC  = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH  = "0x4200000000000000000000000000000000000006"
cbBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
cbXRP = "0xcbd06e5a2b0c65597161de254aa074e489deb510"   # cbDOGE/cbXRP (varies by market)
cbETH = "0x2ae3f1ec7f1f5012cfc3be3072f3527a7e9a4504"

_SWAP_ROUTES: dict[tuple[str, str], tuple[int, int, int]] = {
    # (loan_lower, collateral_lower): (routerType, tickSpacing, minOutBps)
    (USDC,  cbBTC):  (0, 1,   9800),   # cbBTC → USDC, Slipstream CL1
    (USDC,  WETH):   (0, 1,   9900),   # WETH  → USDC, Slipstream CL1
    (USDC,  cbETH):  (0, 1,   9800),   # cbETH → USDC, Slipstream CL1
    (USDC,  cbXRP):  (0, 200, 9500),   # cbXRP → USDC, Slipstream CL200
    (WETH,  cbETH):  (0, 1,   9900),   # cbETH → WETH, Slipstream CL1
}

# ── ABIs ──────────────────────────────────────────────────────────────────────

_LIQUIDATOR_ABI = [
    {
        "name": "executeLiquidation", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "marketParams", "type": "tuple",
             "components": [
                 {"name": "loanToken",       "type": "address"},
                 {"name": "collateralToken", "type": "address"},
                 {"name": "oracle",          "type": "address"},
                 {"name": "irm",             "type": "address"},
                 {"name": "lltv",            "type": "uint256"},
             ]},
            {"name": "borrower",      "type": "address"},
            {"name": "repaidShares",  "type": "uint256"},
            {"name": "swapData",      "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "sweep", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [],
    },
]

_MORPHO_ABI = [
    {
        "name": "position", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "id", "type": "bytes32"}, {"name": "user", "type": "address"}],
        "outputs": [
            {"name": "supplyShares", "type": "uint256"},
            {"name": "borrowShares", "type": "uint128"},
            {"name": "collateral",   "type": "uint128"},
        ],
    },
    {
        "name": "market", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "id", "type": "bytes32"}],
        "outputs": [
            {"name": "totalSupplyAssets",  "type": "uint128"},
            {"name": "totalSupplyShares",  "type": "uint128"},
            {"name": "totalBorrowAssets",  "type": "uint128"},
            {"name": "totalBorrowShares",  "type": "uint128"},
            {"name": "lastUpdate",         "type": "uint128"},
            {"name": "fee",                "type": "uint128"},
        ],
    },
    {
        "name": "idToMarketParams", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "id", "type": "bytes32"}],
        "outputs": [
            {"name": "loanToken",       "type": "address"},
            {"name": "collateralToken", "type": "address"},
            {"name": "oracle",          "type": "address"},
            {"name": "irm",             "type": "address"},
            {"name": "lltv",            "type": "uint256"},
        ],
    },
]

# ── Singleton lock ─────────────────────────────────────────────────────────────

def _acquire_lock() -> None:
    _lf = open(EXEC_LOCK, "w")
    try:
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another morpho_rights_executor is running ({EXEC_LOCK}). Exiting.")

_acquire_lock()

# ── Args ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser()
_parser.add_argument("--dry-run", action="store_true")
_args = _parser.parse_args()
DRY_RUN = _args.dry_run or os.environ.get("MORPHO_EXEC_DRY_RUN", "").lower() in ("1", "true")

if DRY_RUN:
    log.info("[DRY-RUN] mode active — liquidation txs will be simulated only")

# ── Web3 setup ────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not w3.is_connected():
    sys.exit("Cannot connect to Base RPC")

account = w3.eth.account.from_key(PRIV_KEY)
OWNER   = account.address

morpho = w3.eth.contract(address=MORPHO_ADDR, abi=_MORPHO_ABI)

if LIQ_ADDR:
    liquidator = w3.eth.contract(
        address=Web3.to_checksum_address(LIQ_ADDR),
        abi=_LIQUIDATOR_ABI,
    )
else:
    liquidator = None

rights = lrc.get_client()

log.info("=" * 60)
log.info("morpho_rights_executor starting")
log.info("owner      : %s", OWNER)
log.info("liquidator : %s", LIQ_ADDR or "NOT SET — deploy first")
log.info("dry_run    : %s", DRY_RUN)
log.info("=" * 60)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_scanner_state() -> list[dict]:
    """Read watchlist from morpho_scanner_state.json."""
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text()).get("watchlist", [])
    except Exception:
        return []


def _encode_swap_data(loan: str, collateral: str, borrow_assets: int) -> Optional[bytes]:
    """Build swap calldata for MorphoBlueRightsLiquidator._swapCollateral."""
    key = (loan.lower(), collateral.lower())
    route = _SWAP_ROUTES.get(key)
    if route is None:
        log.warning("no swap route for %s/%s — skipping", loan[:12], collateral[:12])
        return None
    router_type, tick_spacing, min_out_bps = route
    # amountOutMinimum = borrow_assets * minOutBps / 10000
    amount_out_min = borrow_assets * min_out_bps // 10000
    return w3.codec.encode(
        ["uint8", "int24", "uint256"],
        [router_type, tick_spacing, amount_out_min],
    )


def _compute_hf(market_id: str, borrower: str, loan: str, collateral: str,
                lltv: int, oracle: str) -> float:
    """Compute live HF from chain. Returns inf on error or no debt."""
    try:
        mid_bytes = bytes.fromhex(market_id)
        pos = morpho.functions.position(mid_bytes, Web3.to_checksum_address(borrower)).call()
        borrow_shares = pos[1]
        if borrow_shares == 0:
            return float("inf")
        mkt = morpho.functions.market(mid_bytes).call()
        if mkt[3] == 0:
            return float("inf")
        borrow_assets = borrow_shares * mkt[2] // mkt[3]
        if borrow_assets == 0:
            return float("inf")
        oracle_c = w3.eth.contract(
            address=Web3.to_checksum_address(oracle),
            abi=[{"name": "price", "type": "function", "stateMutability": "view",
                  "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}],
        )
        price = oracle_c.functions.price().call()
        if price == 0:
            return float("inf")
        collateral_val = pos[2] * price // (10 ** 36)
        max_borrow     = collateral_val * lltv // (10 ** 18)
        return max_borrow / borrow_assets
    except Exception as exc:
        log.debug("HF compute error %s/%s: %s", market_id[:12], borrower[:10], exc)
        return float("inf")


def _execute_liquidation(entry: dict) -> bool:
    """
    Attempt to liquidate a single Morpho Blue position.
    Returns True on success.
    """
    market_id = entry["market_id"]
    borrower  = entry["borrower"]
    loan      = entry.get("loan_token", "")
    collateral= entry.get("collateral_token", "")

    if not liquidator:
        log.warning("liquidator contract not deployed — cannot execute  borrower=%s", borrower[:12])
        return False

    # Verify HF is still below execution threshold (price might have moved)
    hf = _compute_hf(market_id, borrower, loan, collateral,
                     entry.get("lltv", 0), entry.get("oracle", ""))
    if hf >= HF_EXEC_THRESHOLD:
        log.info("HF recovered to %.4f before execution  borrower=%s — skipping", hf, borrower[:12])
        return False

    # Read position to get borrow shares
    try:
        mid_bytes = bytes.fromhex(market_id)
        pos = morpho.functions.position(mid_bytes, Web3.to_checksum_address(borrower)).call()
        mkt = morpho.functions.market(mid_bytes).call()
        repaid_shares = pos[1]
        borrow_assets = repaid_shares * mkt[2] // mkt[3] if mkt[3] > 0 else 0
    except Exception as exc:
        log.warning("position read failed %s: %s", borrower[:12], exc)
        return False

    if repaid_shares == 0:
        log.info("position already closed  borrower=%s", borrower[:12])
        return False

    # Build swap data
    swap_data = _encode_swap_data(loan, collateral, borrow_assets)
    if swap_data is None:
        return False

    # Fetch market params for the liquidation call
    try:
        mp_raw = morpho.functions.idToMarketParams(mid_bytes).call()
        market_params = {
            "loanToken":       mp_raw[0],
            "collateralToken": mp_raw[1],
            "oracle":          mp_raw[2],
            "irm":             mp_raw[3],
            "lltv":            mp_raw[4],
        }
    except Exception as exc:
        log.warning("idToMarketParams failed %s: %s", market_id[:12], exc)
        return False

    if DRY_RUN:
        log.info("[DRY-RUN] would executeLiquidation  borrower=%s  hf=%.4f  "
                 "repaidShares=%d  market=%s",
                 borrower[:12], hf, repaid_shares, market_id[:12])
        return True

    # Send the liquidation transaction
    try:
        with open(SIGNER_LOCK, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                base_fee  = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
                tip       = max(base_fee * 2, 100_000_000)
                gas_est   = liquidator.functions.executeLiquidation(
                    market_params, borrower, repaid_shares, swap_data
                ).estimate_gas({"from": OWNER})
                gas_limit = int(gas_est * 1.25)

                tx = liquidator.functions.executeLiquidation(
                    market_params, borrower, repaid_shares, swap_data
                ).build_transaction({
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

        log.info("liquidation tx sent  borrower=%s  tx=%s", borrower[:12], w3.to_hex(tx_hash))
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            log.warning("liquidation REVERTED  borrower=%s  tx=%s", borrower[:12], w3.to_hex(tx_hash))
            return False

        log.info("liquidation SUCCESS  borrower=%s  gas=%d  block=%d",
                 borrower[:12], receipt["gasUsed"], receipt["blockNumber"])
        return True

    except Exception as exc:
        log.warning("liquidation failed  borrower=%s: %s", borrower[:12], exc)
        return False


# ── Main loop ─────────────────────────────────────────────────────────────────

def _scan_and_execute() -> None:
    """One scan cycle: find registered positions at HF < threshold and execute."""
    watchlist = _read_scanner_state()
    if not watchlist:
        return

    executed = 0
    for entry in watchlist:
        if not entry.get("is_registered"):
            continue

        borrower = entry["borrower"]

        # Check that we still hold active rights
        if not rights.we_hold_rights(borrower):
            continue

        hf = entry.get("last_hf", 999.0)
        if hf >= HF_EXEC_THRESHOLD:
            continue

        log.info("execution candidate  borrower=%s  last_hf=%.4f", borrower[:12], hf)

        success = _execute_liquidation(entry)
        if success:
            # Reclaim our registration stake
            tx = rights.record_execution(borrower)
            if tx:
                log.info("recordExecution  borrower=%s  tx=%s", borrower[:12], tx)
            executed += 1
            time.sleep(3)   # brief pause between consecutive liquidations

    if executed > 0:
        log.info("cycle complete  executed=%d", executed)


def main() -> None:
    while True:
        try:
            _scan_and_execute()
        except Exception as exc:
            log.error("scan cycle error: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
