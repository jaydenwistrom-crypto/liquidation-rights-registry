"""
aave_rights_executor.py — Executes Aave v3 flash liquidations for registered positions.

Reads aave_scanner_state.json to find positions where:
  - We hold active rights (LiquidationRightsRegistryV2)
  - getUserAccountData().healthFactor < 1e18 (position is liquidatable)

When both conditions are true:
  1. Inspects the user's positions to find the best (collateral, debt) pair.
  2. Calls AaveRightsLiquidator.executeLiquidation() — flash borrows, liquidates, swaps.
  3. On success, calls LiquidationRightsRegistry.recordExecution() to reclaim stake.

Aave v3 close factor:
  HF >= 0.95  → can repay 50% of debt (DEFAULT_LIQUIDATION_CLOSE_FACTOR)
  HF  < 0.95  → can repay 100% of debt (MAX_LIQUIDATION_CLOSE_FACTOR)

Swap routing (collateral → debt token):
  All dominant Base Aave v3 markets route via Aerodrome Slipstream.
  Unknown pairs are skipped (stake forfeited to slasher).

Usage:
    venv/bin/python3 aave_rights_executor.py
    venv/bin/python3 aave_rights_executor.py --dry-run
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
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

EXEC_LOCK      = LOGS_DIR / "aave_rights_executor.lock"
STATE_FILE     = LOGS_DIR / "aave_scanner_state.json"
POSITIONS_FILE = LOGS_DIR / "aave_positions.json"
SIGNER_LOCK    = LOGS_DIR / "base_8453_signer.lock"

log = logging.getLogger("aave_exec")
log.setLevel(logging.DEBUG)
log.addHandler(rotating_file_handler(LOGS_DIR / "aave_rights_executor.log"))
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Config ────────────────────────────────────────────────────────────────────

RPC_URL       = os.environ.get("BASE_MAINNET_RPC_URL", "https://mainnet.base.org")
PRIV_KEY      = os.environ.get("VAULT_PRIVATE_KEY", "")
LIQ_ADDR      = os.environ.get("AAVE_RIGHTS_LIQUIDATOR", "")
POLL_INTERVAL = int(os.environ.get("AAVE_EXEC_POLL_INTERVAL", "15"))
HF_EXEC_THRESHOLD = float(os.environ.get("AAVE_EXEC_HF_THRESHOLD", "0.999"))

# Aave close factor thresholds
CLOSE_FACTOR_HF_THRESHOLD = 0.95   # below this: 100% close, above: 50% close

# ── Swap route table ──────────────────────────────────────────────────────────
#
# Maps (collateral_lower, debt_lower) → (routerType, tickSpacing, minOutBps)
# routerType: 0 = Aerodrome Slipstream, 1 = Uniswap V3 / Aerodrome V2
# Dominant Aave v3 Base collateral/debt combos:
#
#   Collateral → Debt (most common)
#   WETH  → USDC  (variable borrow)
#   cbETH → WETH
#   wstETH→ WETH
#   weETH → WETH
#   cbBTC → USDC
#   USDbC → USDC (these are essentially peg arb, skip)

WETH   = "0x4200000000000000000000000000000000000006"
cbETH  = "0x2ae3f1ec7f1f5012cfc3be3072f3527a7e9a4504"
wstETH = "0xc1cba3fcea344f92d9239c08c0568f6f2f0ee452"
weETH  = "0x04c0599ae5a44757c0af6f9ec3b93da8976c150a"
ezETH  = "0x2416092f143378750bb29b79ed961ab195ccea5a"
cbBTC  = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
USDC   = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDbC  = "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"

_SWAP_ROUTES: dict[tuple[str, str], tuple[int, int, int]] = {
    # (collateral_lower, debt_lower): (routerType, tickSpacing, minOutBps)
    (WETH,   USDC):  (0, 1,   9900),   # WETH  → USDC  Slipstream CL1
    (WETH,   USDbC): (0, 1,   9900),   # WETH  → USDbC Slipstream CL1
    (cbETH,  WETH):  (0, 1,   9900),   # cbETH → WETH  Slipstream CL1
    (cbETH,  USDC):  (0, 1,   9800),   # cbETH → USDC  Slipstream CL1
    (cbETH,  USDbC): (0, 1,   9800),
    (wstETH, WETH):  (0, 1,   9900),   # wstETH→ WETH  Slipstream CL1
    (wstETH, USDC):  (0, 1,   9800),
    (weETH,  WETH):  (0, 1,   9900),   # weETH → WETH  Slipstream CL1
    (weETH,  USDC):  (0, 1,   9800),
    (ezETH,  WETH):  (0, 100, 9700),   # ezETH → WETH  Slipstream CL100
    (cbBTC,  USDC):  (0, 1,   9800),   # cbBTC → USDC  Slipstream CL1
    (cbBTC,  USDbC): (0, 1,   9800),
    (cbBTC,  WETH):  (0, 1,   9800),
}

# ── ABIs ──────────────────────────────────────────────────────────────────────

_LIQUIDATOR_ABI = [
    {
        "name": "executeLiquidation", "type": "function", "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralAsset", "type": "address"},
            {"name": "debtAsset",       "type": "address"},
            {"name": "user",            "type": "address"},
            {"name": "debtToCover",     "type": "uint256"},
            {"name": "swapData",        "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "sweep", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "token", "type": "address"}],
        "outputs": [],
    },
]

_POOL_ABI = [
    {
        "name": "getUserAccountData", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "user", "type": "address"}],
        "outputs": [
            {"name": "totalCollateralBase",        "type": "uint256"},
            {"name": "totalDebtBase",               "type": "uint256"},
            {"name": "availableBorrowsBase",        "type": "uint256"},
            {"name": "currentLiquidationThreshold", "type": "uint256"},
            {"name": "ltv",                         "type": "uint256"},
            {"name": "healthFactor",                "type": "uint256"},
        ],
    },
    {
        "name": "getReservesList", "type": "function", "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getReserveData", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "asset", "type": "address"}],
        "outputs": [{"name": "", "type": "tuple",
            "components": [
                {"name": "configuration",              "type": "uint256"},
                {"name": "liquidityIndex",             "type": "uint128"},
                {"name": "currentLiquidityRate",       "type": "uint128"},
                {"name": "variableBorrowIndex",        "type": "uint128"},
                {"name": "currentVariableBorrowRate",  "type": "uint128"},
                {"name": "currentStableBorrowRate",    "type": "uint128"},
                {"name": "lastUpdateTimestamp",        "type": "uint40"},
                {"name": "id",                         "type": "uint16"},
                {"name": "aTokenAddress",              "type": "address"},
                {"name": "stableDebtTokenAddress",     "type": "address"},
                {"name": "variableDebtTokenAddress",   "type": "address"},
                {"name": "interestRateStrategyAddress","type": "address"},
                {"name": "accruedToTreasury",          "type": "uint128"},
                {"name": "unbacked",                   "type": "uint128"},
                {"name": "isolationModeTotalDebt",     "type": "uint128"},
            ]}],
    },
]

_ERC20_BALANCE_ABI = [
    {
        "name": "balanceOf", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ── Singleton lock ─────────────────────────────────────────────────────────────

def _acquire_lock() -> None:
    _lf = open(EXEC_LOCK, "w")
    try:
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another aave_rights_executor is running ({EXEC_LOCK}). Exiting.")

_acquire_lock()

# ── Args ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser()
_parser.add_argument("--dry-run", action="store_true")
_args   = _parser.parse_args()
DRY_RUN = _args.dry_run or os.environ.get("AAVE_EXEC_DRY_RUN", "").lower() in ("1", "true")

if DRY_RUN:
    log.info("[DRY-RUN] mode active — liquidation txs will be simulated only")

# ── Web3 setup ────────────────────────────────────────────────────────────────

AAVE_POOL_ADDR = Web3.to_checksum_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 20}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not w3.is_connected():
    sys.exit("Cannot connect to Base RPC")

account = w3.eth.account.from_key(PRIV_KEY)
OWNER   = account.address

pool   = w3.eth.contract(address=AAVE_POOL_ADDR, abi=_POOL_ABI)
rights = lrc.get_client()

if LIQ_ADDR:
    liquidator = w3.eth.contract(
        address=Web3.to_checksum_address(LIQ_ADDR),
        abi=_LIQUIDATOR_ABI,
    )
else:
    liquidator = None

log.info("=" * 60)
log.info("aave_rights_executor starting")
log.info("owner      : %s", OWNER)
log.info("liquidator : %s", LIQ_ADDR or "NOT SET — run deploy_aave_liq.py first")
log.info("dry_run    : %s", DRY_RUN)
log.info("=" * 60)

# ── Reserve map: asset → (aToken, variableDebtToken) ─────────────────────────

@dataclass
class ReserveInfo:
    asset:         str
    a_token:       str
    v_debt_token:  str

_reserve_map: dict[str, ReserveInfo] = {}   # asset_lower → ReserveInfo


def _build_reserve_map() -> None:
    """Fetch aToken + variableDebtToken addresses for all Aave v3 reserves."""
    reserves = pool.functions.getReservesList().call()
    for asset in reserves:
        try:
            rd = pool.functions.getReserveData(asset).call()
            a_token    = rd[8]
            v_debt_tok = rd[10]
            _reserve_map[asset.lower()] = ReserveInfo(
                asset=asset,
                a_token=v_debt_tok,     # intentional: re-assigned below
                v_debt_token=v_debt_tok,
            )
            _reserve_map[asset.lower()].a_token = a_token
        except Exception as exc:
            log.debug("getReserveData failed %s: %s", asset[:12], exc)

    log.info("reserve map built  reserves=%d", len(_reserve_map))


_build_reserve_map()

# ── Position inspection ───────────────────────────────────────────────────────

def _find_best_pair(user: str) -> Optional[tuple[str, str, int]]:
    """
    Inspect all Aave v3 reserves to find this user's largest collateral and debt positions.
    Returns (collateral_asset, debt_asset, debt_amount) or None if nothing found / no route.
    """
    cs_user = Web3.to_checksum_address(user)

    best_debt_asset  = ""
    best_debt_amount = 0
    best_coll_asset  = ""
    best_coll_amount = 0

    for asset_lower, ri in _reserve_map.items():
        try:
            tok = w3.eth.contract(address=Web3.to_checksum_address(ri.v_debt_token), abi=_ERC20_BALANCE_ABI)
            debt_bal = tok.functions.balanceOf(cs_user).call()
            if debt_bal > best_debt_amount:
                best_debt_amount = debt_bal
                best_debt_asset  = asset_lower
        except Exception:
            pass

        try:
            atk = w3.eth.contract(address=Web3.to_checksum_address(ri.a_token), abi=_ERC20_BALANCE_ABI)
            coll_bal = atk.functions.balanceOf(cs_user).call()
            if coll_bal > best_coll_amount:
                best_coll_amount = coll_bal
                best_coll_asset  = asset_lower
        except Exception:
            pass

    if not best_debt_asset or not best_coll_asset or best_debt_amount == 0:
        return None

    route_key = (best_coll_asset, best_debt_asset)
    if route_key not in _SWAP_ROUTES:
        log.warning("no swap route for collateral=%s debt=%s — skipping",
                    best_coll_asset[:12], best_debt_asset[:12])
        return None

    return best_coll_asset, best_debt_asset, best_debt_amount


# ── Execution ─────────────────────────────────────────────────────────────────

def _compute_hf_live(user: str) -> float:
    """Return live health factor for the user (float, 1e18-scaled → plain)."""
    try:
        data = pool.functions.getUserAccountData(
            Web3.to_checksum_address(user)
        ).call()
        if data[1] == 0:
            return float("inf")
        return min(data[5] / 1e18, 9999.0)
    except Exception:
        return float("inf")


def _encode_swap_data(collateral: str, debt: str, debt_amount: int) -> Optional[bytes]:
    key = (collateral.lower(), debt.lower())
    route = _SWAP_ROUTES.get(key)
    if route is None:
        return None
    router_type, tick_spacing, min_out_bps = route
    amount_out_min = debt_amount * min_out_bps // 10000
    return w3.codec.encode(
        ["uint8", "int24", "uint256"],
        [router_type, tick_spacing, amount_out_min],
    )


def _execute_liquidation(borrower: str) -> bool:
    if not liquidator:
        log.warning("liquidator not deployed — cannot execute  borrower=%s", borrower[:12])
        return False

    hf = _compute_hf_live(borrower)
    if hf >= HF_EXEC_THRESHOLD:
        log.info("HF recovered to %.4f  borrower=%s — skipping", hf, borrower[:12])
        return False

    pair = _find_best_pair(borrower)
    if pair is None:
        log.warning("could not find liquidatable pair  borrower=%s", borrower[:12])
        return False

    collateral_asset, debt_asset, debt_amount = pair

    # Apply close factor
    if hf < CLOSE_FACTOR_HF_THRESHOLD:
        debt_to_cover = debt_amount           # 100% close factor
    else:
        debt_to_cover = debt_amount // 2      # 50% close factor

    if debt_to_cover == 0:
        return False

    swap_data = _encode_swap_data(collateral_asset, debt_asset, debt_to_cover)
    if swap_data is None:
        return False

    cs_collateral = Web3.to_checksum_address(collateral_asset)
    cs_debt       = Web3.to_checksum_address(debt_asset)
    cs_borrower   = Web3.to_checksum_address(borrower)

    if DRY_RUN:
        log.info("[DRY-RUN] would executeLiquidation  borrower=%s  hf=%.4f  "
                 "collateral=%s  debt=%s  debtToCover=%d",
                 borrower[:12], hf, cs_collateral[:12], cs_debt[:12], debt_to_cover)
        return True

    try:
        with open(SIGNER_LOCK, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                base_fee  = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
                tip       = max(base_fee * 2, 100_000_000)
                gas_est   = liquidator.functions.executeLiquidation(
                    cs_collateral, cs_debt, cs_borrower, debt_to_cover, swap_data
                ).estimate_gas({"from": OWNER})
                gas_limit = int(gas_est * 1.25)

                tx = liquidator.functions.executeLiquidation(
                    cs_collateral, cs_debt, cs_borrower, debt_to_cover, swap_data
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

        tx_hex = w3.to_hex(tx_hash)
        log.info("liquidation tx sent  borrower=%s  tx=%s", borrower[:12], tx_hex)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            log.warning("liquidation REVERTED  borrower=%s  tx=%s", borrower[:12], tx_hex)
            return False

        log.info("liquidation SUCCESS  borrower=%s  gas=%d  block=%d",
                 borrower[:12], receipt["gasUsed"], receipt["blockNumber"])
        return True

    except Exception as exc:
        log.warning("liquidation failed  borrower=%s: %s", borrower[:12], exc)
        return False


# ── Main scan loop ─────────────────────────────────────────────────────────────

def _read_scanner_state() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text()).get("watchlist", [])
    except Exception:
        return []


def _scan_and_execute() -> None:
    watchlist = _read_scanner_state()
    if not watchlist:
        return

    executed = 0
    for entry in watchlist:
        if not entry.get("is_registered"):
            continue

        borrower = entry.get("borrower", "")
        if not borrower:
            continue

        if not rights.we_hold_rights(borrower):
            continue

        hf = entry.get("last_hf", 9999.0)
        if hf >= HF_EXEC_THRESHOLD:
            continue

        log.info("execution candidate  borrower=%s  last_hf=%.4f", borrower[:12], hf)

        success = _execute_liquidation(borrower)
        if success:
            tx = rights.record_execution(borrower)
            if tx:
                log.info("recordExecution  borrower=%s  tx=%s", borrower[:12], tx)
            executed += 1
            time.sleep(3)

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
