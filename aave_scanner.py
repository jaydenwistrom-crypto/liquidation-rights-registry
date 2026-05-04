"""
aave_scanner.py — Aave v3 position scanner for the Liquidation Rights Protocol.

Watches Aave v3 Pool on Base for under-collateralised positions.
When getUserAccountData(user).healthFactor drops below REGISTER_THRESHOLD,
registers the borrower with LiquidationRightsRegistryV2.

Flow:
  1. Startup: backfill Borrow events from (currentBlock - BACKFILL_LOOKBACK) → borrower list
  2. Live:    poll Borrow events every cycle for new borrowers
  3. HF check: every POLL_INTERVAL, call getUserAccountData() for all watched accounts
  4. Register: HF < REGISTER_THRESHOLD → register with rights registry
  5. Prune:    totalDebtBase == 0 → remove from watchlist

Aave v3 Pool on Base:
  0xA238Dd80C259a72e81d7e4664a9801593F98d1c5

HF is returned scaled by 1e18 — no oracle math needed.
Position is liquidatable when healthFactor < 1e18.

Writes:
  logs/aave_scanner_state.json   — watchlist + stats (atomic)
  logs/aave_positions.json       — borrower metadata (for executor)

Usage:
    venv/bin/python3 aave_scanner.py
    venv/bin/python3 aave_scanner.py --dry-run
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

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

SCANNER_LOCK = LOGS_DIR / "aave_scanner.lock"
STATE_FILE   = LOGS_DIR / "aave_scanner_state.json"
POSITIONS_FILE = LOGS_DIR / "aave_positions.json"

log = logging.getLogger("aave_scanner")
log.setLevel(logging.DEBUG)
log.addHandler(rotating_file_handler(LOGS_DIR / "aave_scanner.log"))
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Config ────────────────────────────────────────────────────────────────────

RPC_PRIMARY = os.environ.get("BASE_MAINNET_RPC_URL", "https://mainnet.base.org")
POLL_INTERVAL    = int(os.environ.get("AAVE_POLL_INTERVAL",      "30"))
REGISTER_THRESHOLD = float(os.environ.get("AAVE_REGISTER_THRESHOLD", "1.08"))
WATCH_THRESHOLD    = float(os.environ.get("AAVE_WATCH_THRESHOLD",    "1.20"))
_BACKFILL_LOOKBACK = int(os.environ.get("AAVE_BACKFILL_LOOKBACK",   "2_000_000"))
_LOG_CHUNK   = int(os.environ.get("AAVE_LOG_CHUNK",   "2000"))
REGISTRATION_RECHECK_SECONDS = int(os.environ.get("AAVE_REGISTRATION_RECHECK_SECONDS", "900"))

AAVE_POOL = Web3.to_checksum_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")

# Pre-computed topic hashes
# Borrow(address indexed reserve, address user, address indexed onBehalfOf,
#        uint256 amount, uint8 interestRateMode, uint256 borrowRate, uint16 indexed referralCode)
_T_BORROW = Web3.keccak(
    text="Borrow(address,address,address,uint256,uint8,uint256,uint16)"
).hex()
# Repay(address indexed reserve, address indexed user, address indexed repayer,
#       uint256 amount, bool useATokens)
_T_REPAY = Web3.keccak(
    text="Repay(address,address,address,uint256,bool)"
).hex()

# ── ABIs ──────────────────────────────────────────────────────────────────────

_POOL_ABI = [
    {
        "name": "getUserAccountData", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
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
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]

# ── Singleton lock ─────────────────────────────────────────────────────────────

_LOCK_FD = None

def _acquire_lock() -> None:
    global _LOCK_FD
    _lf = open(SCANNER_LOCK, "w")
    try:
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another aave_scanner is already running ({SCANNER_LOCK}). Exiting.")
    _LOCK_FD = _lf

_acquire_lock()

# ── Args ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser()
_parser.add_argument("--dry-run", action="store_true")
_args    = _parser.parse_args()
DRY_RUN  = _args.dry_run or os.environ.get("AAVE_SCANNER_DRY_RUN", "").lower() in ("1", "true")

if DRY_RUN:
    print("[DRY-RUN] mode active — rights registrations will be simulated only")

# ── Web3 ──────────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(RPC_PRIMARY, request_kwargs={"timeout": 30}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

if not w3.is_connected():
    sys.exit("Cannot connect to Base RPC")

pool   = w3.eth.contract(address=AAVE_POOL, abi=_POOL_ABI)
rights = lrc.get_client()

# ── State ──────────────────────────────────────────────────────────────────────

@dataclass
class WatchEntry:
    borrower:          str
    last_hf:           float = 9999.0
    total_collateral:  int   = 0   # USD base (8 decimals)
    total_debt:        int   = 0   # USD base (8 decimals)
    is_registered:     bool  = False
    registered_at:     int   = 0
    registration_status: str = "unregistered"
    registration_tx:    str  = ""
    first_seen_block:  int   = 0

@dataclass
class Stats:
    watching:         int = 0
    registrations:    int = 0
    scans:            int = 0
    positions_pruned: int = 0
    rpc_errors:       int = 0

_watchlist: dict[str, WatchEntry] = {}    # borrower_lower → WatchEntry
_stats = Stats()
_last_event_block: int = 0


def _addr_from_topic(topic_bytes) -> str:
    """Extract checksummed address from a 32-byte padded topic."""
    return Web3.to_checksum_address("0x" + topic_bytes.hex()[24:])


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state() -> None:
    _stats.watching = len(_watchlist)
    doc = {
        "stats": _stats.__dict__,
        "last_event_block": _last_event_block,
        "watchlist": [
            {
                "borrower":        e.borrower,
                "last_hf":         round(e.last_hf, 6),
                "total_collateral": e.total_collateral,
                "total_debt":      e.total_debt,
                "is_registered":   e.is_registered,
                "registered_at":   e.registered_at,
                "registration_status": e.registration_status,
                "registration_tx":  e.registration_tx,
                "first_seen_block": e.first_seen_block,
            }
            for e in _watchlist.values()
        ],
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.rename(STATE_FILE)


def _save_positions() -> None:
    doc = {
        e.borrower: {
            "protocol":         "aave_v3",
            "pool":             AAVE_POOL,
            "health_factor":    round(e.last_hf, 6),
            "total_collateral": e.total_collateral,
            "total_debt":       e.total_debt,
            "is_registered":    e.is_registered,
            "registered_at":    e.registered_at,
            "registration_status": e.registration_status,
            "registration_tx":   e.registration_tx,
        }
        for e in _watchlist.values()
    }
    tmp = POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.rename(POSITIONS_FILE)


def _load_state() -> None:
    global _last_event_block
    if not STATE_FILE.exists():
        return
    try:
        doc = json.loads(STATE_FILE.read_text())
        _last_event_block = doc.get("last_event_block", 0)
        for row in doc.get("watchlist", []):
            b = row["borrower"]
            _watchlist[b.lower()] = WatchEntry(
                borrower=b,
                last_hf=row.get("last_hf", 9999.0),
                total_collateral=row.get("total_collateral", 0),
                total_debt=row.get("total_debt", 0),
                is_registered=row.get("is_registered", False),
                registered_at=row.get("registered_at", 0),
                registration_status=row.get("registration_status", "unknown"),
                registration_tx=row.get("registration_tx", ""),
                first_seen_block=row.get("first_seen_block", 0),
            )
        log.info("state loaded  watching=%d  last_event_block=%d",
                 len(_watchlist), _last_event_block)
    except Exception as exc:
        log.warning("failed to load state: %s", exc)


def _add_borrower(borrower: str, block: int) -> None:
    key = borrower.lower()
    if key not in _watchlist:
        _watchlist[key] = WatchEntry(
            borrower=Web3.to_checksum_address(borrower),
            first_seen_block=block,
        )
        log.debug("watching  borrower=%s", borrower[:12])


# ── Backfill ──────────────────────────────────────────────────────────────────

def _backfill(current_block: int) -> None:
    global _last_event_block
    from_block = max(
        _last_event_block + 1 if _last_event_block else 0,
        current_block - _BACKFILL_LOOKBACK,
    )
    to_block = current_block

    log.info("backfill  blocks=%d→%d", from_block, to_block)

    # Prefer public fallback for large range scans
    rpc_url = "https://mainnet.base.org"
    w3_pub  = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    w3_pub.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    total  = 0
    cur    = from_block
    while cur <= to_block:
        end = min(cur + _LOG_CHUNK - 1, to_block)
        try:
            logs = w3_pub.eth.get_logs({
                "address":   AAVE_POOL,
                "topics":    [["0x" + _T_BORROW]],
                "fromBlock": cur,
                "toBlock":   end,
            })
            for lg in logs:
                # topics[2] = onBehalfOf (the actual borrower/position owner)
                borrower = _addr_from_topic(lg["topics"][2])
                _add_borrower(borrower, lg["blockNumber"])
                total += 1
        except Exception as exc:
            log.debug("backfill chunk failed %d-%d: %s", cur, end, exc)
        cur = end + 1

    _last_event_block = to_block
    log.info("backfill complete  events=%d  watching=%d", total, len(_watchlist))


# ── Live event polling ─────────────────────────────────────────────────────────

def _poll_new_borrows() -> None:
    global _last_event_block
    current_block = w3.eth.block_number
    if current_block <= _last_event_block:
        return

    from_block = _last_event_block + 1
    to_block   = min(current_block, _last_event_block + _LOG_CHUNK)

    try:
        logs = w3.eth.get_logs({
            "address":   AAVE_POOL,
            "topics":    [["0x" + _T_BORROW]],
            "fromBlock": from_block,
            "toBlock":   to_block,
        })
        for lg in logs:
            borrower = _addr_from_topic(lg["topics"][2])
            _add_borrower(borrower, lg["blockNumber"])

        _last_event_block = to_block
    except Exception as exc:
        log.warning("live borrow poll failed: %s", exc)


# ── HF polling ────────────────────────────────────────────────────────────────

def _poll_hf() -> None:
    """Check getUserAccountData() for all watched positions."""
    if not _watchlist:
        return

    _stats.scans += 1
    to_prune: list[str] = []

    for key, entry in list(_watchlist.items()):
        try:
            data = pool.functions.getUserAccountData(
                Web3.to_checksum_address(entry.borrower)
            ).call()
        except Exception as exc:
            _stats.rpc_errors += 1
            log.debug("getUserAccountData failed %s: %s", entry.borrower[:12], exc)
            continue

        total_collateral = data[0]   # uint256, 8-decimal USD base
        total_debt       = data[1]
        health_factor    = data[5]   # uint256, scaled 1e18

        # Prune: no debt left
        if total_debt == 0:
            to_prune.append(key)
            continue

        # Convert HF from 1e18 scale to float
        # Clamp to a sane max to avoid float overflow for positions with near-zero debt
        hf = min(health_factor / 1e18, 9999.0)

        entry.last_hf          = hf
        entry.total_collateral = total_collateral
        entry.total_debt       = total_debt

        # Register if HF is close to liquidation threshold
        if hf < REGISTER_THRESHOLD and not entry.is_registered:
            _register_position(entry)
        elif hf < REGISTER_THRESHOLD and entry.is_registered:
            # Do not re-check on-chain rights immediately after submission.
            # Registration txs can remain pending under nonce/base-fee pressure,
            # and treating "not confirmed yet" as "not registered" burns gas.
            age = int(time.time()) - entry.registered_at
            if age > REGISTRATION_RECHECK_SECONDS:
                if rights.we_hold_rights(entry.borrower):
                    entry.registration_status = "active"
                else:
                    entry.is_registered = False
                    entry.registration_status = "expired_or_missing"
                    _register_position(entry)

        if hf < REGISTER_THRESHOLD:
            log.info("at-risk  borrower=%s  hf=%.4f  registered=%s  status=%s",
                     entry.borrower[:12], hf, entry.is_registered, entry.registration_status)

        # Prune healthy positions to keep watchlist lean
        if hf > WATCH_THRESHOLD and not entry.is_registered:
            to_prune.append(key)

    for key in to_prune:
        del _watchlist[key]
        _stats.positions_pruned += 1

    if to_prune:
        log.info("pruned %d positions  watching=%d", len(to_prune), len(_watchlist))

    _stats.watching = len(_watchlist)


def _register_position(entry: WatchEntry) -> None:
    if DRY_RUN:
        log.info("[DRY-RUN] would register  borrower=%s  hf=%.4f",
                 entry.borrower[:12], entry.last_hf)
        entry.is_registered = True
        entry.registered_at = int(time.time())
        entry.registration_status = "dry_run"
        entry.registration_tx = ""
        _stats.registrations += 1
        return

    tx = rights.register(entry.borrower)
    entry.registered_at = int(time.time())
    if tx:
        entry.is_registered = True
        entry.registration_status = "active" if tx == "already-active" else "submitted"
        entry.registration_tx = tx
        _stats.registrations += 1
        log.info("registered  borrower=%s  hf=%.4f  tx=%s",
                 entry.borrower[:12], entry.last_hf, tx)
    else:
        entry.is_registered = False
        entry.registration_status = "deferred"
        entry.registration_tx = ""
        log.warning("registration failed  borrower=%s — will retry after %ds",
                    entry.borrower[:12], REGISTRATION_RECHECK_SECONDS)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _last_event_block

    _load_state()
    current_block = w3.eth.block_number

    log.info("=" * 60)
    log.info("aave_scanner starting")
    log.info("registry : %s", os.environ.get("LIQUIDATION_RIGHTS_REGISTRY", "not set"))
    log.info("pool     : %s", AAVE_POOL)
    log.info("rpc      : %s", RPC_PRIMARY)
    log.info("dry_run  : %s", DRY_RUN)
    log.info("=" * 60)

    _backfill(current_block)

    log.info("initial watchlist: %d positions", len(_watchlist))

    _poll_hf()
    _save_state()
    _save_positions()

    next_hf_poll = time.time() + POLL_INTERVAL
    next_save    = time.time() + 60

    while True:
        now = time.time()

        _poll_new_borrows()

        if now >= next_hf_poll:
            _poll_hf()
            next_hf_poll = now + POLL_INTERVAL

        if now >= next_save:
            _save_state()
            _save_positions()
            log.info("state  watching=%d  registrations=%d  scans=%d  rpc_errors=%d",
                     _stats.watching, _stats.registrations, _stats.scans, _stats.rpc_errors)
            next_save = now + 60

        time.sleep(6)


if __name__ == "__main__":
    main()
