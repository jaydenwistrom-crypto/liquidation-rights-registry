"""
compound_scanner.py — Compound v3 position scanner for the Liquidation Rights Protocol.

Watches Compound v3 (Comet) markets on Base for liquidatable positions.
When isLiquidatable(account) returns true, registers with LiquidationRightsRegistryV2
to claim priority rights — feeding slash revenue into SlashRevenueVaultV2.

Flow:
  1. Startup: backfill Withdraw (borrow) events from COMPOUND_START_BLOCK → borrower watchlist
  2. Live:     poll Withdraw events every cycle for new borrowers
  3. Liquidatability check: every POLL_INTERVAL, call isLiquidatable() for all watched accounts
  4. Register: isLiquidatable() == True → register with rights registry
  5. Prune:    borrowBalanceOf(account) == 0 → remove from watchlist

Comet markets on Base:
  cUSDbCv3  0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf  (borrow USDbC)
  cWETHv3   0x46e6b214b524310239732D51387075E0e70970bf  (borrow WETH)

Writes:
  logs/compound_scanner_state.json  — watchlist + stats (atomic)
  logs/compound_positions.json      — borrower → protocol metadata (for executor)

Usage:
    venv/bin/python3 compound_scanner.py
    venv/bin/python3 compound_scanner.py --dry-run
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

SCANNER_LOCK    = LOGS_DIR / "compound_scanner.lock"
STATE_FILE      = LOGS_DIR / "compound_scanner_state.json"
POSITIONS_FILE  = LOGS_DIR / "compound_positions.json"

log = logging.getLogger("compound_scanner")
log.setLevel(logging.DEBUG)
log.addHandler(rotating_file_handler(LOGS_DIR / "compound_scanner.log"))
log.addHandler(logging.StreamHandler(sys.stdout))

# ── Config ────────────────────────────────────────────────────────────────────

RPC_PRIMARY  = os.environ.get("BASE_MAINNET_RPC_URL",  "https://mainnet.base.org")
RPC_FALLBACK = os.environ.get("BASE_POLLING_RPC_URL",  "https://base-rpc.publicnode.com")

# Compound v3 Comet markets on Base
_DEFAULT_COMETS = {
    "cUSDbCv3": "0x9c4ec768c28520B50860ea7a15bd7213a9fF58bf",
    "cWETHv3":  "0x46e6b214b524310239732D51387075E0e70970bf",
}

POLL_INTERVAL        = int(os.environ.get("COMPOUND_POLL_INTERVAL", "30"))
EVENT_CHUNK          = int(os.environ.get("COMPOUND_EVENT_CHUNK",   "3000"))
BACKFILL_LOOKBACK    = int(os.environ.get("COMPOUND_BACKFILL_LOOKBACK", "2_000_000"))  # ~1 month

# ── Pre-computed topic hashes ─────────────────────────────────────────────────
#
# Compound v3 event signatures:
#   Withdraw(address src, address to, uint256 amount)   — borrow/withdraw base
#   Supply(address from, address dst, uint256 amount)   — repay/supply base
#   AbsorbDebt(address absorber, address borrower, ...)  — liquidation completed
#
# In Comet, negative base balance = borrowing. Withdraw events create/increase borrows.

_T_WITHDRAW   = Web3.keccak(text="Withdraw(address,address,uint256)").hex()
_T_SUPPLY     = Web3.keccak(text="Supply(address,address,uint256)").hex()
_T_ABSORB_DEBT= Web3.keccak(text="AbsorbDebt(address,address,uint256,uint256)").hex()

# ── ABI ───────────────────────────────────────────────────────────────────────

_COMET_ABI = [
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
    {
        "name": "baseToken", "type": "function", "stateMutability": "view",
        "inputs":  [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "getLiquidationMargin", "type": "function", "stateMutability": "view",
        "inputs":  [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "int256"}],
    },
]

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CometMarket:
    name:        str
    address:     str
    base_token:  str
    contract:    object  # web3 contract instance


@dataclass
class WatchEntry:
    borrower:       str    # checksum address
    comet_address:  str    # which Comet market
    comet_name:     str
    base_token:     str
    is_registered:  bool  = False
    registered_at:  int   = 0
    is_liquidatable:bool  = False

    @property
    def key(self) -> tuple[str, str]:
        return (self.comet_address.lower(), self.borrower.lower())


@dataclass
class ScannerStats:
    watching:         int   = 0
    registrations:    int   = 0
    scans:            int   = 0
    rpc_errors:       int   = 0
    positions_pruned: int   = 0
    start_time:       float = field(default_factory=time.time)


# ── Singleton lock ─────────────────────────────────────────────────────────────

def _acquire_lock() -> None:
    _lf = open(SCANNER_LOCK, "w")
    try:
        fcntl.flock(_lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another compound_scanner is already running ({SCANNER_LOCK}). Exiting.")

_acquire_lock()

# ── Args ──────────────────────────────────────────────────────────────────────

_parser = argparse.ArgumentParser()
_parser.add_argument("--dry-run", action="store_true")
_args = _parser.parse_args()
DRY_RUN = _args.dry_run or os.environ.get("COMPOUND_SCANNER_DRY_RUN", "").lower() in ("1", "true")

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

# ── State ─────────────────────────────────────────────────────────────────────

# (comet_address_lower, borrower_lower) → WatchEntry
_watchlist: dict[tuple[str, str], WatchEntry] = {}

# comet_address_lower → CometMarket
_markets: dict[str, CometMarket] = {}

_stats  = ScannerStats()
_rights = lrc.get_client()
_last_event_blocks: dict[str, int] = {}   # comet_addr_lower → last processed block

# ── Market setup ──────────────────────────────────────────────────────────────

def _init_markets() -> None:
    """Initialize Comet market contracts."""
    ERC20_ABI = [{"name": "symbol", "type": "function", "stateMutability": "view",
                  "inputs": [], "outputs": [{"name": "", "type": "string"}]}]
    for name, addr in _DEFAULT_COMETS.items():
        try:
            c = w3.eth.contract(address=addr, abi=_COMET_ABI)
            base_token = c.functions.baseToken().call()
            _markets[addr.lower()] = CometMarket(
                name=name, address=addr, base_token=base_token, contract=c
            )
            log.info("market  %s  base=%s  address=%s", name, base_token[:12], addr)
        except Exception as exc:
            log.warning("failed to init market %s: %s", name, exc)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_state() -> None:
    payload = {
        "watching":         _stats.watching,
        "registrations":    _stats.registrations,
        "scans":            _stats.scans,
        "rpc_errors":       _stats.rpc_errors,
        "positions_pruned": _stats.positions_pruned,
        "watchlist": [
            {
                "borrower":       e.borrower,
                "comet_address":  e.comet_address,
                "comet_name":     e.comet_name,
                "base_token":     e.base_token,
                "is_registered":  e.is_registered,
                "is_liquidatable":e.is_liquidatable,
                "registered_at":  e.registered_at,
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
    for key, entry in _watchlist.items():
        if entry.is_liquidatable or entry.is_registered:
            positions[entry.borrower] = {
                "protocol":      "compound",
                "comet_address": entry.comet_address,
                "comet_name":    entry.comet_name,
                "base_token":    entry.base_token,
                "is_liquidatable": entry.is_liquidatable,
            }
    tmp = POSITIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(positions, indent=2))
    tmp.replace(POSITIONS_FILE)


def _add_to_watchlist(comet_addr: str, borrower: str) -> bool:
    """Add a borrower to the watchlist for a given Comet market. Returns True if new."""
    key = (comet_addr.lower(), borrower.lower())
    if key in _watchlist:
        return False
    market = _markets.get(comet_addr.lower())
    if market is None:
        return False
    _watchlist[key] = WatchEntry(
        borrower      = Web3.to_checksum_address(borrower),
        comet_address = comet_addr,
        comet_name    = market.name,
        base_token    = market.base_token,
    )
    _stats.watching = len(_watchlist)
    return True


def _extract_address_from_topic(topic) -> str:
    """Convert a 32-byte event topic to an address string."""
    raw = topic.hex() if isinstance(topic, bytes) else topic
    return Web3.to_checksum_address("0x" + raw[-40:])


# ── Event processing ──────────────────────────────────────────────────────────

def _process_withdraw_log(raw_log: dict, comet_addr: str) -> None:
    """
    Extract borrower from a Comet Withdraw event.
    In Compound v3, Withdraw(address src, address to, uint256 amount)
    src = indexed topic[1] = the account that withdrew (the borrower when base < 0)
    """
    topics = raw_log.get("topics", [])
    if len(topics) < 2:
        return

    t0 = topics[0]
    t0_hex = (t0.hex() if isinstance(t0, bytes) else t0).lstrip("0x")
    if t0_hex.lower() != _T_WITHDRAW.lower():
        return

    src = _extract_address_from_topic(topics[1])
    if _add_to_watchlist(comet_addr, src):
        log.debug("watching  comet=%s  borrower=%s", comet_addr[:12], src[:12])


# ── Backfill ──────────────────────────────────────────────────────────────────

def _backfill_market(comet_addr: str, from_block: int, to_block: int) -> int:
    """Backfill Withdraw events for a single Comet market."""
    w_pub = _mk_w3("https://mainnet.base.org", timeout=20)
    total = 0
    cur   = from_block

    while cur <= to_block:
        end = min(cur + EVENT_CHUNK - 1, to_block)
        got = False
        for w_try in (w3, w_pub):
            try:
                logs = w_try.eth.get_logs({
                    "address":   comet_addr,
                    "topics":    [["0x" + _T_WITHDRAW]],
                    "fromBlock": cur,
                    "toBlock":   end,
                })
                for raw in logs:
                    _process_withdraw_log(raw, comet_addr)
                total += len(logs)
                got = True
                break
            except Exception as exc:
                log.debug("backfill chunk %d-%d failed %s: %s",
                          cur, end, comet_addr[:12], str(exc)[:60])
                time.sleep(1)

        if not got:
            log.warning("skipping chunk %d-%d for %s", cur, end, comet_addr[:12])

        cur = end + 1
        time.sleep(0.05)

    return total


def _backfill_all(current_block: int) -> None:
    start_block = max(0, current_block - BACKFILL_LOOKBACK)
    end_block   = current_block - 100

    for addr, market in _markets.items():
        log.info("backfill  market=%s  blocks=%d→%d", market.name, start_block, end_block)
        total = _backfill_market(market.address, start_block, end_block)
        log.info("backfill  market=%s  events=%d  watching=%d", market.name, total, len(_watchlist))
        _last_event_blocks[addr] = end_block


# ── Live event polling ────────────────────────────────────────────────────────

def _poll_new_withdraws() -> None:
    """Poll for new Withdraw events across all Comet markets."""
    try:
        current = w3.eth.block_number
    except Exception as exc:
        log.warning("block number fetch failed: %s", exc)
        return

    for addr, market in _markets.items():
        last = _last_event_blocks.get(addr, 0)
        if last == 0:
            _last_event_blocks[addr] = current - 1
            continue
        if current <= last:
            continue

        from_block = last + 1
        try:
            logs = w3.eth.get_logs({
                "address":   market.address,
                "topics":    [["0x" + _T_WITHDRAW]],
                "fromBlock": from_block,
                "toBlock":   current,
            })
            for raw in logs:
                _process_withdraw_log(raw, market.address)
            if logs:
                log.info("new withdraws  market=%s  count=%d  watching=%d",
                         market.name, len(logs), len(_watchlist))
            _last_event_blocks[addr] = current
        except Exception as exc:
            log.warning("live event poll failed %s: %s", market.name, exc)


# ── Liquidatability check ─────────────────────────────────────────────────────

def _poll_liquidatability() -> None:
    """Check isLiquidatable() for all watched positions."""
    if not _watchlist:
        return

    _stats.scans += 1
    to_prune: list[tuple[str, str]] = []

    for key, entry in list(_watchlist.items()):
        market = _markets.get(entry.comet_address.lower())
        if market is None:
            continue

        try:
            borrow_bal = market.contract.functions.borrowBalanceOf(entry.borrower).call()
        except Exception as exc:
            _stats.rpc_errors += 1
            log.debug("borrowBalanceOf failed %s: %s", entry.borrower[:12], exc)
            continue

        if borrow_bal == 0:
            # Position closed — prune
            to_prune.append(key)
            continue

        try:
            is_liq = market.contract.functions.isLiquidatable(entry.borrower).call()
        except Exception as exc:
            _stats.rpc_errors += 1
            log.debug("isLiquidatable failed %s: %s", entry.borrower[:12], exc)
            continue

        entry.is_liquidatable = is_liq

        if is_liq and not entry.is_registered:
            _register_position(entry)
        elif is_liq and entry.is_registered:
            # Check if rights still active; re-register if window expired
            if not _rights.we_hold_rights(entry.borrower):
                entry.is_registered = False
                _register_position(entry)

        if is_liq:
            log.info("liquidatable!  comet=%s  borrower=%s  registered=%s",
                     market.name, entry.borrower[:12], entry.is_registered)

    for key in to_prune:
        del _watchlist[key]
        _stats.positions_pruned += 1

    if to_prune:
        log.info("pruned %d closed positions  watching=%d", len(to_prune), len(_watchlist))

    _stats.watching = len(_watchlist)


def _register_position(entry: WatchEntry) -> None:
    if DRY_RUN:
        log.info("[DRY-RUN] would register  borrower=%s  comet=%s",
                 entry.borrower[:12], entry.comet_name)
        entry.is_registered = True
        entry.registered_at = int(time.time())
        _stats.registrations += 1
        return

    tx = _rights.register(entry.borrower)
    if tx:
        entry.is_registered = True
        entry.registered_at = int(time.time())
        _stats.registrations += 1
        log.info("registered  borrower=%s  comet=%s  tx=%s",
                 entry.borrower[:12], entry.comet_name, tx)
    else:
        log.warning("registration failed  borrower=%s", entry.borrower[:12])


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _init_markets()
    current_block = w3.eth.block_number

    log.info("=" * 60)
    log.info("compound_scanner starting")
    log.info("registry : %s", os.environ.get("LIQUIDATION_RIGHTS_REGISTRY", "not set"))
    log.info("rpc      : %s", RPC_PRIMARY)
    log.info("markets  : %s", list(_DEFAULT_COMETS.keys()))
    log.info("dry_run  : %s", DRY_RUN)
    log.info("=" * 60)

    _backfill_all(current_block)

    log.info("initial watchlist: %d positions", len(_watchlist))

    # First liquidatability check immediately after backfill
    _poll_liquidatability()
    _save_state()
    _save_positions()

    next_liq_poll = time.time() + POLL_INTERVAL
    next_save     = time.time() + 60

    while True:
        now = time.time()

        _poll_new_withdraws()

        if now >= next_liq_poll:
            _poll_liquidatability()
            next_liq_poll = now + POLL_INTERVAL

        if now >= next_save:
            _save_state()
            _save_positions()
            log.info("state  watching=%d  registrations=%d  scans=%d  rpc_errors=%d",
                     _stats.watching, _stats.registrations, _stats.scans, _stats.rpc_errors)
            next_save = now + 60

        time.sleep(6)


if __name__ == "__main__":
    main()
