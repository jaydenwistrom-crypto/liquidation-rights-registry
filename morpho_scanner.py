"""
morpho_scanner.py — Morpho Blue position scanner for the Liquidation Rights Protocol.

Watches all Morpho Blue markets on Base for at-risk positions (HF < REGISTER_THRESHOLD).
When a position approaches the liquidation boundary, registers with
LiquidationRightsRegistryV2 to claim priority rights — feeding slash revenue into
SlashRevenueVaultV2 when liquidators miss their window.

Flow:
  1. Startup: backfill Borrow events from MORPHO_START_BLOCK → find all active borrowers
  2. Live:     poll Borrow events every POLL_INTERVAL for new positions
  3. HF check: every POLL_INTERVAL, compute HF for all watched (market_id, borrower) pairs
  4. Register: HF < REGISTER_THRESHOLD (1.08) → register with rights registry
  5. Prune:    borrowShares == 0 → remove from watchlist

Writes:
  logs/morpho_scanner_state.json  — watchlist + lifetime stats (atomic)
  logs/morpho_positions.json      — borrower → protocol metadata (read by executors)

Usage:
    venv/bin/python3 morpho_scanner.py
    venv/bin/python3 morpho_scanner.py --dry-run
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from log_rotation import rotating_file_handler
import liquidation_rights_client as lrc

# ── Bootstrap ─────────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).resolve().parent
ENV_PATH    = BASE_DIR / ".env"
LOGS_DIR    = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

load_dotenv(ENV_PATH)

SCANNER_LOCK    = LOGS_DIR / "morpho_scanner.lock"
STATE_FILE      = LOGS_DIR / "morpho_scanner_state.json"
POSITIONS_FILE  = LOGS_DIR / "morpho_positions.json"  # read by morpho_rights_executor

log = logging.getLogger("morpho_scanner")
log.setLevel(logging.DEBUG)
log.addHandler(rotating_file_handler(LOGS_DIR / "morpho_scanner.log"))
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Config ────────────────────────────────────────────────────────────────────

RPC_PRIMARY  = os.environ.get("BASE_MAINNET_RPC_URL",  "https://mainnet.base.org")
RPC_FALLBACK = os.environ.get("BASE_POLLING_RPC_URL",  "https://base-rpc.publicnode.com")

# Morpho Blue — same address on all EVM chains
MORPHO_ADDR  = Web3.to_checksum_address("0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb")

# Default: backfill last ~3 months (4.5M blocks).  Override via env to go deeper.
# Full history (~Aug 2023) requires an archive node; recent positions are sufficient
# for the rights protocol — anyone who hasn't repaid in 3 months is either liquidated
# or still active and catchable via live events going forward.
_BACKFILL_LOOKBACK   = int(os.environ.get("MORPHO_BACKFILL_LOOKBACK", "4_500_000"))
MORPHO_START_BLOCK   = int(os.environ.get("MORPHO_START_BLOCK", "0"))   # 0 = auto

POLL_INTERVAL       = int(os.environ.get("MORPHO_POLL_INTERVAL", "30"))    # seconds
REGISTER_THRESHOLD  = float(os.environ.get("MORPHO_REGISTER_THRESHOLD", "1.08"))
WATCH_THRESHOLD     = float(os.environ.get("MORPHO_WATCH_THRESHOLD", "1.15"))
EVENT_CHUNK         = int(os.environ.get("MORPHO_EVENT_CHUNK", "2000"))     # blocks per eth_getLogs

# ── Pre-computed topic hashes ─────────────────────────────────────────────────

_T_CREATE_MARKET = Web3.keccak(
    text="CreateMarket(bytes32,(address,address,address,address,uint256))"
).hex()
_T_BORROW        = Web3.keccak(
    text="Borrow(bytes32,address,address,address,uint256,uint256)"
).hex()
_T_REPAY         = Web3.keccak(
    text="Repay(bytes32,address,address,uint256,uint256)"
).hex()
_T_LIQUIDATE     = Web3.keccak(
    text="Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
).hex()

# ── ABIs ──────────────────────────────────────────────────────────────────────

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

_ORACLE_ABI = [
    {
        "name": "price", "type": "function", "stateMutability": "view",
        "inputs": [], "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class MarketParams:
    loan_token:       str
    collateral_token: str
    oracle:           str
    irm:              str
    lltv:             int   # 1e18 scale


@dataclass
class WatchEntry:
    market_id:     str          # hex, no 0x prefix
    borrower:      str          # checksum address
    loan_token:    str
    collateral_token: str
    lltv:          int
    oracle:        str
    last_hf:       float = 999.0
    is_registered: bool  = False
    registered_at: int   = 0    # unix timestamp

    @property
    def key(self) -> tuple[str, str]:
        return (self.market_id, self.borrower.lower())


@dataclass
class ScannerStats:
    watching:            int   = 0
    registrations:       int   = 0
    scans:               int   = 0
    rpc_errors:          int   = 0
    positions_pruned:    int   = 0
    start_time:          float = field(default_factory=time.time)


# ── Singleton lock ─────────────────────────────────────────────────────────────

def _acquire_lock() -> None:
    _lf = open(SCANNER_LOCK, "w")
    try:
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another morpho_scanner is already running ({SCANNER_LOCK}). Exiting.")

_acquire_lock()

# ── Args ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser()
_parser.add_argument("--dry-run", action="store_true")
_args = _parser.parse_args()
DRY_RUN = _args.dry_run or os.environ.get("MORPHO_SCANNER_DRY_RUN", "").lower() in ("1", "true")

if DRY_RUN:
    log.info("[DRY-RUN] mode active — rights registrations will be simulated only")

# ── Web3 setup ────────────────────────────────────────────────────────────────

def _mk_w3(url: str, timeout: int = 15) -> Web3:
    w = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
    w.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w

w3 = _mk_w3(RPC_PRIMARY)
if not w3.is_connected():
    w3 = _mk_w3(RPC_FALLBACK)
    if not w3.is_connected():
        sys.exit("Cannot connect to any Base RPC")

morpho = w3.eth.contract(address=MORPHO_ADDR, abi=_MORPHO_ABI)

# ── State ─────────────────────────────────────────────────────────────────────

# (market_id_hex, borrower_lower) → WatchEntry
_watchlist: dict[tuple[str, str], WatchEntry] = {}

# market_id_hex → MarketParams  (immutable, cache forever)
_market_cache: dict[str, MarketParams] = {}

# oracle_addr → price  (refreshed each HF poll cycle)
_oracle_cache: dict[str, int] = {}

_stats = ScannerStats()
_rights = lrc.get_client()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_state() -> None:
    """Atomically persist watchlist and stats to disk."""
    payload = {
        "watching":         _stats.watching,
        "registrations":    _stats.registrations,
        "scans":            _stats.scans,
        "rpc_errors":       _stats.rpc_errors,
        "positions_pruned": _stats.positions_pruned,
        "watchlist": [
            {
                "market_id":        e.market_id,
                "borrower":         e.borrower,
                "loan_token":       e.loan_token,
                "collateral_token": e.collateral_token,
                "last_hf":          e.last_hf,
                "is_registered":    e.is_registered,
                "registered_at":    e.registered_at,
            }
            for e in _watchlist.values()
        ],
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATE_FILE)


def _save_positions() -> None:
    """Write borrower → protocol metadata for executor consumption."""
    positions: dict[str, dict] = {}
    for (mid, borrower), entry in _watchlist.items():
        if entry.last_hf < REGISTER_THRESHOLD * 1.5:
            positions[entry.borrower] = {
                "protocol":       "morpho",
                "market_id":      entry.market_id,
                "loan_token":     entry.loan_token,
                "collateral_token": entry.collateral_token,
                "last_hf":        entry.last_hf,
            }
    tmp = POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(positions, indent=2))
    tmp.replace(POSITIONS_FILE)


def _add_to_watchlist(market_id: str, borrower: str, mparams: MarketParams) -> bool:
    """Add a (market_id, borrower) pair if not already watched. Returns True if new."""
    key = (market_id, borrower.lower())
    if key in _watchlist:
        return False
    _watchlist[key] = WatchEntry(
        market_id        = market_id,
        borrower         = Web3.to_checksum_address(borrower),
        loan_token       = mparams.loan_token,
        collateral_token = mparams.collateral_token,
        lltv             = mparams.lltv,
        oracle           = mparams.oracle,
    )
    _stats.watching = len(_watchlist)
    return True


def _ensure_market(market_id_bytes: bytes) -> Optional[MarketParams]:
    """Fetch and cache market params for a market id."""
    mid_hex = market_id_bytes.hex()
    if mid_hex in _market_cache:
        return _market_cache[mid_hex]
    try:
        raw = morpho.functions.idToMarketParams(market_id_bytes).call()
        mp = MarketParams(
            loan_token       = raw[0],
            collateral_token = raw[1],
            oracle           = raw[2],
            irm              = raw[3],
            lltv             = raw[4],
        )
        _market_cache[mid_hex] = mp
        return mp
    except Exception as exc:
        log.warning("idToMarketParams failed %s: %s", mid_hex[:12], exc)
        return None


def _oracle_price(oracle_addr: str) -> Optional[int]:
    """Return oracle price (collateral/loan, scaled 1e36)."""
    try:
        oracle_c = w3.eth.contract(
            address=Web3.to_checksum_address(oracle_addr),
            abi=_ORACLE_ABI,
        )
        price = oracle_c.functions.price().call()
        _oracle_cache[oracle_addr] = price
        return price
    except Exception as exc:
        log.debug("oracle price failed %s: %s", oracle_addr[:12], exc)
        # Fallback to cached value if available
        return _oracle_cache.get(oracle_addr)


def _compute_hf(entry: WatchEntry) -> float:
    """Compute health factor for a watch entry. Returns inf on any issue."""
    try:
        mid_bytes = bytes.fromhex(entry.market_id)
        pos  = morpho.functions.position(mid_bytes, entry.borrower).call()
        # pos: (supplyShares, borrowShares, collateral)
        borrow_shares = pos[1]
        collateral    = pos[2]

        if borrow_shares == 0:
            return float("inf")  # position fully repaid

        mkt = morpho.functions.market(mid_bytes).call()
        # mkt: (totalSupplyAssets, totalSupplyShares, totalBorrowAssets, totalBorrowShares, ...)
        total_borrow_assets = mkt[2]
        total_borrow_shares = mkt[3]

        if total_borrow_shares == 0:
            return float("inf")

        borrow_assets = borrow_shares * total_borrow_assets // total_borrow_shares

        if borrow_assets == 0:
            return float("inf")

        price = _oracle_price(entry.oracle)
        if price is None or price == 0:
            return float("inf")

        # collateral_val = collateral amount in loan token base units
        collateral_val = collateral * price // (10 ** 36)
        # max_borrow is collateral_val * LLTV (1e18 scale)
        max_borrow = collateral_val * entry.lltv // (10 ** 18)

        return max_borrow / borrow_assets

    except Exception as exc:
        _stats.rpc_errors += 1
        log.debug("HF compute failed %s/%s: %s", entry.market_id[:12], entry.borrower[:10], exc)
        return float("inf")


# ── Event processing ──────────────────────────────────────────────────────────

def _process_borrow_log(raw_log: dict) -> None:
    """Extract (market_id, borrower) from a Borrow event log and add to watchlist."""
    topics = raw_log.get("topics", [])
    if len(topics) < 3:
        return
    t0 = topics[0].hex() if isinstance(topics[0], bytes) else topics[0]
    if not t0.startswith("0x"):
        t0 = "0x" + t0
    if t0.lower() != ("0x" + _T_BORROW).lower():
        return

    # topics[1] = market id (bytes32)
    # topics[2] = onBehalf (address, zero-padded to 32 bytes)
    raw_mid      = topics[1]
    raw_borrower = topics[2]

    mid_hex  = (raw_mid.hex() if isinstance(raw_mid, bytes) else raw_mid).lstrip("0x")
    mid_hex  = mid_hex.zfill(64)
    b_hex    = (raw_borrower.hex() if isinstance(raw_borrower, bytes) else raw_borrower)
    borrower = "0x" + b_hex[-40:]

    mid_bytes = bytes.fromhex(mid_hex)
    mparams   = _ensure_market(mid_bytes)
    if mparams is None:
        return

    if _add_to_watchlist(mid_hex, borrower, mparams):
        log.debug("watching  market=%s  borrower=%s", mid_hex[:12], borrower[:12])


# ── Backfill ──────────────────────────────────────────────────────────────────

def _backfill(from_block: int, to_block: int) -> int:
    """
    Backfill Borrow events in chunks. Returns total events processed.
    Tries local RPC first, then public mainnet.base.org — never the archive
    (its auth key may be expired).
    """
    w_pub = _mk_w3("https://mainnet.base.org", timeout=20)
    total = 0
    chunk = EVENT_CHUNK
    cur   = from_block

    log.info("backfill  blocks %d → %d  (chunk=%d)", from_block, to_block, chunk)

    while cur <= to_block:
        end = min(cur + chunk - 1, to_block)
        got = False
        for w_try in (w3, w_pub):
            try:
                logs = w_try.eth.get_logs({
                    "address":   MORPHO_ADDR,
                    "topics":    [["0x" + _T_BORROW]],
                    "fromBlock": cur,
                    "toBlock":   end,
                })
                for raw in logs:
                    _process_borrow_log(raw)
                total += len(logs)
                if len(logs) > 0:
                    log.debug("backfill %d-%d  events=%d  watching=%d",
                              cur, end, len(logs), len(_watchlist))
                got = True
                break
            except Exception as exc:
                log.debug("backfill chunk %d-%d failed: %s", cur, end, str(exc)[:80])
                time.sleep(1)

        if not got:
            log.warning("skipping chunk %d-%d — both RPCs failed", cur, end)

        cur = end + 1
        time.sleep(0.05)

    log.info("backfill complete  total_events=%d  watching=%d", total, len(_watchlist))
    return total


# ── HF poll cycle ─────────────────────────────────────────────────────────────

def _poll_hf_all() -> None:
    """
    Check HF for every watched position. Register at-risk ones, prune closed ones.
    """
    if not _watchlist:
        return

    _stats.scans += 1
    to_prune: list[tuple[str, str]] = []

    for key, entry in list(_watchlist.items()):
        hf = _compute_hf(entry)

        if hf == float("inf"):
            # Position fully repaid or oracle issue — keep watching for 1 more cycle,
            # then verify by checking borrow shares directly
            entry.last_hf = hf
            continue

        entry.last_hf = hf

        if hf > WATCH_THRESHOLD:
            # Healthy — stop watching
            to_prune.append(key)
            continue

        if hf <= REGISTER_THRESHOLD and not entry.is_registered:
            _register_position(entry, hf)

        elif hf <= REGISTER_THRESHOLD and entry.is_registered:
            # Check if rights are still active; if window expired re-register
            if not _rights.we_hold_rights(entry.borrower):
                entry.is_registered = False
                _register_position(entry, hf)

        log.debug("hf=%.4f  market=%s  borrower=%s  registered=%s",
                  hf, entry.market_id[:12], entry.borrower[:12], entry.is_registered)

    # Prune positions that recovered above WATCH_THRESHOLD
    for key in to_prune:
        del _watchlist[key]
        _stats.positions_pruned += 1

    if to_prune:
        log.info("pruned %d recovered positions  watching=%d", len(to_prune), len(_watchlist))

    _stats.watching = len(_watchlist)


def _register_position(entry: WatchEntry, hf: float) -> None:
    """Register rights on a position approaching liquidation."""
    if DRY_RUN:
        log.info("[DRY-RUN] would register  borrower=%s  hf=%.4f  market=%s",
                 entry.borrower[:12], hf, entry.market_id[:12])
        entry.is_registered = True
        entry.registered_at = int(time.time())
        _stats.registrations += 1
        return

    tx = _rights.register(entry.borrower)
    if tx:
        entry.is_registered = True
        entry.registered_at = int(time.time())
        _stats.registrations += 1
        log.info("registered  borrower=%s  hf=%.4f  market=%s  tx=%s",
                 entry.borrower[:12], hf, entry.market_id[:12], tx)
    else:
        log.warning("registration failed  borrower=%s  hf=%.4f", entry.borrower[:12], hf)


# ── Live event poll ───────────────────────────────────────────────────────────

_last_event_block: int = 0


def _poll_new_borrows() -> None:
    """Poll for new Borrow events since last check block."""
    global _last_event_block

    try:
        current = w3.eth.block_number
    except Exception as exc:
        log.warning("block number fetch failed: %s", exc)
        return

    if _last_event_block == 0:
        _last_event_block = current - 1
        return

    if current <= _last_event_block:
        return

    from_block = _last_event_block + 1
    to_block   = current

    try:
        logs = w3.eth.get_logs({
            "address":   MORPHO_ADDR,
            "topics":    [["0x" + _T_BORROW]],
            "fromBlock": from_block,
            "toBlock":   to_block,
        })
        for raw in logs:
            _process_borrow_log(raw)
        if logs:
            log.info("new borrows  blocks=%d-%d  count=%d  watching=%d",
                     from_block, to_block, len(logs), len(_watchlist))
        _last_event_block = to_block
    except Exception as exc:
        log.warning("live event poll failed %d-%d: %s", from_block, to_block, exc)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    current_block = w3.eth.block_number
    log.info("=" * 60)
    log.info("morpho_scanner starting")
    log.info("morpho   : %s", MORPHO_ADDR)
    log.info("registry : %s", os.environ.get("LIQUIDATION_RIGHTS_REGISTRY", "not set"))
    log.info("rpc      : %s", RPC_PRIMARY)
    log.info("register_threshold : %.2f", REGISTER_THRESHOLD)
    log.info("watch_threshold    : %.2f", WATCH_THRESHOLD)
    log.info("dry_run  : %s", DRY_RUN)
    log.info("=" * 60)

    # Determine backfill window.  MORPHO_START_BLOCK=0 means auto: last _BACKFILL_LOOKBACK blocks.
    start_block  = MORPHO_START_BLOCK if MORPHO_START_BLOCK > 0 \
                   else max(0, current_block - _BACKFILL_LOOKBACK)
    backfill_to  = current_block - 150   # leave a 150-block tail for live poll
    _backfill(start_block, backfill_to)

    global _last_event_block
    _last_event_block = backfill_to

    log.info("initial watchlist: %d positions", len(_watchlist))

    # Initial HF scan immediately after backfill
    _poll_hf_all()
    _save_state()
    _save_positions()

    next_hf_poll  = time.time() + POLL_INTERVAL
    next_save     = time.time() + 60

    while True:
        now = time.time()

        # Live event ingestion — every cycle
        _poll_new_borrows()

        # HF poll — every POLL_INTERVAL
        if now >= next_hf_poll:
            _poll_hf_all()
            next_hf_poll = now + POLL_INTERVAL

        # State persistence — every 60s
        if now >= next_save:
            _save_state()
            _save_positions()
            log.info("state  watching=%d  registrations=%d  scans=%d  rpc_errors=%d",
                     _stats.watching, _stats.registrations, _stats.scans, _stats.rpc_errors)
            next_save = now + 60

        time.sleep(6)


if __name__ == "__main__":
    main()
