"""
rights_slasher.py — Autonomous slash bounty collector for LiquidationRightsRegistry.

Watches on-chain Registered events. When a registration window expires without
a recordExecution call, fires slash(borrower) to collect the 50% ETH bounty.
The other 50% goes to the protocol treasury (also our wallet, so we win twice).

Flow:
  1. Startup: backfill Registered/Executed/Slashed events from last BACKFILL_BLOCKS
  2. Every POLL_INTERVAL: fetch new events since last block, update watch queue
  3. For each expired + unexecuted entry: verify on-chain state, then slash

Revenue: SLASH_BOUNTY_BPS (50%) of each forfeited stake flows to this wallet.

Writes to:
  logs/slasher_state.json   — live stats + current watch queue (for dashboards)
  logs/slash_events.jsonl   — append-only per-slash audit log
  logs/rights_slasher.log   — rotating log file
"""
from __future__ import annotations

import argparse
import fcntl
import heapq
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from log_rotation import rotating_file_handler

# ── Bootstrap ─────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
load_dotenv(ENV_PATH)

# ── Config ─────────────────────────────────────────────────────────────────────

REGISTRY_ADDR = os.environ.get("LIQUIDATION_RIGHTS_REGISTRY", "").strip()
RPC_URL       = os.environ.get("BASE_MAINNET_RPC_URL", "https://mainnet.base.org").strip()
PRIV_KEY      = os.environ.get("VAULT_PRIVATE_KEY", "").strip()

POLL_INTERVAL_SEC = 6       # ~3 Base blocks; fast enough to catch expiries promptly
BACKFILL_BLOCKS   = int(os.environ.get("SLASHER_BACKFILL_BLOCKS", "10000"))  # ~3h default
SLASH_GAS_LIMIT   = 120_000  # conservative; actual cost ~60-80k
CB_MAX_FAILURES   = 8       # consecutive slash failures before pausing 60s

# Set at startup by argparse or env; checked inside _execute_slash.
DRY_RUN: bool = os.environ.get("SLASHER_DRY_RUN", "").lower() in ("1", "true", "yes")

SIGNER_LOCK   = LOGS_DIR / "base_8453_signer.lock"
DAEMON_LOCK   = LOGS_DIR / "rights_slasher.lock"
STATE_FILE    = LOGS_DIR / "slasher_state.json"
EVENTS_FILE   = LOGS_DIR / "slash_events.jsonl"
LOG_FILE      = LOGS_DIR / "rights_slasher.log"

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        rotating_file_handler(LOG_FILE),
    ],
)
log = logging.getLogger("rights_slasher")

# ── Pre-flight ─────────────────────────────────────────────────────────────────

if not REGISTRY_ADDR:
    log.critical("LIQUIDATION_RIGHTS_REGISTRY not set in .env — cannot start")
    sys.exit(1)
if not PRIV_KEY:
    log.critical("VAULT_PRIVATE_KEY not set in .env — cannot start")
    sys.exit(1)

# ── ABI ───────────────────────────────────────────────────────────────────────

_ABI = [
    # ── functions ──────────────────────────────────────────────────────────────
    {
        "name": "slash",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs":  [{"name": "borrower", "type": "address"}],
        "outputs": [],
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
    # ── events ─────────────────────────────────────────────────────────────────
    {
        "name": "Registered",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "borrower",   "type": "address", "indexed": True},
            {"name": "liquidator", "type": "address", "indexed": True},
            {"name": "stake",      "type": "uint256", "indexed": False},
            {"name": "expiresAt",  "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "Executed",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "borrower",      "type": "address", "indexed": True},
            {"name": "liquidator",    "type": "address", "indexed": True},
            {"name": "stakeReturned", "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "Slashed",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "borrower",          "type": "address", "indexed": True},
            {"name": "slashedLiquidator", "type": "address", "indexed": True},
            {"name": "slasher",           "type": "address", "indexed": True},
            {"name": "bounty",            "type": "uint256", "indexed": False},
            {"name": "treasuryShare",     "type": "uint256", "indexed": False},
        ],
    },
    {
        "name": "Outbid",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "borrower",      "type": "address", "indexed": True},
            {"name": "previous",      "type": "address", "indexed": True},
            {"name": "replacement",   "type": "address", "indexed": True},
            {"name": "refundedStake", "type": "uint256", "indexed": False},
        ],
    },
]

# ── Data ───────────────────────────────────────────────────────────────────────

@dataclass
class WatchEntry:
    borrower:   str
    liquidator: str
    stake:      int      # wei
    expires_at: int      # unix timestamp

    @property
    def bounty_wei(self) -> int:
        return self.stake // 2   # 50% to slasher

    @property
    def is_expired(self) -> bool:
        return time.time() >= self.expires_at


@dataclass
class SlasherStats:
    lifetime_slashes:   int   = 0
    lifetime_bounty_wei: int  = 0
    races_lost:         int   = 0
    last_slash_at:      str   = ""
    last_slash_bounty:  float = 0.0
    last_block:         int   = 0

    def to_dict(self) -> dict:
        return {
            "lifetime_slashes":     self.lifetime_slashes,
            "lifetime_bounty_eth":  round(self.lifetime_bounty_wei / 1e18, 6),
            "races_lost":           self.races_lost,
            "last_slash_at":        self.last_slash_at,
            "last_slash_bounty_eth": round(self.last_slash_bounty, 6),
            "last_block_processed": self.last_block,
        }


# ── Web3 setup ────────────────────────────────────────────────────────────────

def _connect(url: str, timeout: int = 10) -> Web3:
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


w3       = _connect(RPC_URL, timeout=15)
account  = w3.eth.account.from_key(PRIV_KEY)
OWNER    = account.address
registry = w3.eth.contract(
    address=Web3.to_checksum_address(REGISTRY_ADDR),
    abi=_ABI,
)

# Fallback w3 for verification reads — Coinbase sequencer direct, avoids Beast stale reads.
_SEQ_URL = "https://mainnet.base.org"
w3_seq   = _connect(_SEQ_URL, timeout=10)
reg_seq  = w3_seq.eth.contract(
    address=Web3.to_checksum_address(REGISTRY_ADDR),
    abi=_ABI,
)

# ── Event topic hashes ───────────────────────────────────────────────────────
# Computed once at load time. Used in _fetch_events to dispatch by topic0 before
# calling process_log() — avoids silent misses from try/except shotgun matching.

_T_REGISTERED = Web3.keccak(text="Registered(address,address,uint256,uint256)").hex()
_T_EXECUTED   = Web3.keccak(text="Executed(address,address,uint256)").hex()
_T_SLASHED    = Web3.keccak(text="Slashed(address,address,address,uint256,uint256)").hex()

# ── Tip calculation ───────────────────────────────────────────────────────────

def _slash_priority_tip(bounty_wei: int, base_fee: int) -> int:
    """
    Priority tip for a slash tx.
    Scales aggressively for large bounties; stays lean for small ones.
    Gas cost at 0.1 gwei × 80k gas ≈ $0.03 — trivial vs any bounty.
    """
    floor = max(base_fee * 2, 50_000_000)    # 0.05 gwei minimum
    if bounty_wei >= int(0.05e18):            # bounty ≥ 0.05 ETH — go aggressive
        return min(500_000_000, floor * 10)   # 0.5 gwei cap
    return min(100_000_000, floor * 4)        # 0.1 gwei cap for small bounties


# ── Watch queue ───────────────────────────────────────────────────────────────
# dict is the source of truth; heap is an expiry-sorted index for fast iteration.
# Heap entries: (expires_at, borrower). Stale heap entries are discarded on pop.

_watch:  dict[str, WatchEntry]       = {}   # borrower → WatchEntry
_heap:   list[tuple[int, str]]       = []   # (expires_at, borrower) min-heap


def _add_watch(e: WatchEntry) -> None:
    _watch[e.borrower] = e
    heapq.heappush(_heap, (e.expires_at, e.borrower))


def _remove_watch(borrower: str) -> None:
    _watch.pop(borrower, None)
    # Heap entry will be discarded lazily when popped.


def _expired_entries() -> list[WatchEntry]:
    """Return all watched entries whose window has expired. Does not remove them."""
    now = time.time()
    result = []
    for e in list(_watch.values()):
        if e.is_expired:
            result.append(e)
    return result


# ── Event processing ──────────────────────────────────────────────────────────

def _fetch_events(from_block: int, to_block: int) -> None:
    """
    Fetch Registered, Executed, and Slashed events in [from_block, to_block]
    and update the watch queue accordingly.
    """
    try:
        reg_logs = w3_seq.eth.get_logs({
            "address":   Web3.to_checksum_address(REGISTRY_ADDR),
            "fromBlock": from_block,
            "toBlock":   to_block,
        })
    except Exception as exc:
        log.warning("get_logs failed [%d-%d]: %s", from_block, to_block, exc)
        return

    for raw in reg_logs:
        if not raw.get("topics"):
            continue
        t0 = raw["topics"][0].hex()

        try:
            if t0 == _T_REGISTERED:
                evt        = registry.events.Registered().process_log(raw)
                borrower   = evt["args"]["borrower"]
                liquidator = evt["args"]["liquidator"]
                stake      = evt["args"]["stake"]
                expires_at = evt["args"]["expiresAt"]

                if liquidator.lower() == OWNER.lower():
                    _remove_watch(borrower)
                    log.debug("skip own registration  borrower=%s…", borrower[:10])
                    continue

                _add_watch(WatchEntry(
                    borrower=borrower,
                    liquidator=liquidator,
                    stake=stake,
                    expires_at=expires_at,
                ))
                remaining = max(0, expires_at - time.time())
                log.info(
                    "watching  borrower=%s…  liquidator=%s…  stake=%.4f ETH  "
                    "expires_in=%.0fs",
                    borrower[:10], liquidator[:10], stake / 1e18, remaining,
                )

            elif t0 == _T_EXECUTED:
                evt      = registry.events.Executed().process_log(raw)
                borrower = evt["args"]["borrower"]
                _remove_watch(borrower)
                log.info("executed — removed from watch  borrower=%s…", borrower[:10])

            elif t0 == _T_SLASHED:
                evt      = registry.events.Slashed().process_log(raw)
                borrower = evt["args"]["borrower"]
                slasher  = evt["args"]["slasher"]
                bounty   = evt["args"]["bounty"]
                _remove_watch(borrower)
                if slasher.lower() != OWNER.lower():
                    log.info(
                        "slashed by other  borrower=%s…  slasher=%s…  bounty=%.4f ETH",
                        borrower[:10], slasher[:10], bounty / 1e18,
                    )

        except Exception as exc:
            log.debug("event parse error t0=%s: %s", t0[:10], exc)


# ── Pre-slash verification ────────────────────────────────────────────────────

def _verify_slashable(borrower: str) -> Optional[tuple[str, int]]:
    """
    On-chain check before sending the slash tx.
    Returns (liquidator, stake) if slashable, else None.
    Uses the sequencer RPC to avoid stale Beast proxy reads.
    """
    for attempt in range(3):
        try:
            liq, stake, expires_at, executed, active = reg_seq.functions.getRights(
                Web3.to_checksum_address(borrower)
            ).call()
            now = w3_seq.eth.get_block("latest")["timestamp"]

            if liq == "0x0000000000000000000000000000000000000000":
                return None   # no rights at all
            if executed:
                return None   # already recorded execution
            if now < expires_at:
                return None   # window not yet expired
            if liq.lower() == OWNER.lower():
                return None   # cannot self-slash
            return liq, stake
        except Exception as exc:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning("getRights verification failed for %s…: %s", borrower[:10], exc)
                return None
    return None


# ── Slash execution ───────────────────────────────────────────────────────────

def _execute_slash(entry: WatchEntry, stats: SlasherStats) -> bool:
    """
    Fire slash(borrower). Returns True on confirmed success.
    Race losses (revert) are handled gracefully — returns False, increments races_lost.
    """
    borrower = entry.borrower
    log.info(
        "attempting slash  borrower=%s…  stake=%.4f ETH  bounty≈%.4f ETH",
        borrower[:10], entry.stake / 1e18, entry.bounty_wei / 1e18,
    )

    if DRY_RUN:
        log.info(
            "[DRY-RUN] would slash  borrower=%s…  bounty≈%.4f ETH  (no tx sent)",
            borrower[:10], entry.bounty_wei / 1e18,
        )
        _remove_watch(entry.borrower)
        return True

    try:
        with open(SIGNER_LOCK, "w") as _lf:
            fcntl.flock(_lf, fcntl.LOCK_EX)
            try:
                base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
                tip      = _slash_priority_tip(entry.bounty_wei, base_fee)
                tx = registry.functions.slash(
                    Web3.to_checksum_address(borrower)
                ).build_transaction({
                    "chainId":              8453,
                    "gas":                  SLASH_GAS_LIMIT,
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
        log.info("slash tx sent  borrower=%s…  tx=%s", borrower[:10], tx_hex)

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] != 1:
            log.warning(
                "slash REVERTED (race lost)  borrower=%s…  tx=%s",
                borrower[:10], tx_hex,
            )
            stats.races_lost += 1
            _remove_watch(borrower)
            return False

        # ── Confirmed ──────────────────────────────────────────────────────────
        actual_bounty = entry.bounty_wei   # 50% of stake; matches contract math
        stats.lifetime_slashes    += 1
        stats.lifetime_bounty_wei += actual_bounty
        stats.last_slash_at        = datetime.now(timezone.utc).isoformat()
        stats.last_slash_bounty    = actual_bounty / 1e18

        log.info(
            "SLASHED  borrower=%s…  bounty=%.4f ETH  gas=%d  block=%d  "
            "lifetime_slashes=%d  lifetime_bounty=%.4f ETH",
            borrower[:10], actual_bounty / 1e18,
            receipt["gasUsed"], receipt["blockNumber"],
            stats.lifetime_slashes, stats.lifetime_bounty_wei / 1e18,
        )

        # Audit log entry
        audit = {
            "ts":               stats.last_slash_at,
            "borrower":         borrower,
            "slashed_liquidator": entry.liquidator,
            "stake_eth":        round(entry.stake / 1e18, 6),
            "bounty_eth":       round(actual_bounty / 1e18, 6),
            "tx_hash":          tx_hex,
            "gas_used":         receipt["gasUsed"],
            "block":            receipt["blockNumber"],
        }
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(audit) + "\n")

        _remove_watch(borrower)
        return True

    except Exception as exc:
        err = str(exc).lower()
        if "already known" in err or "nonce too low" in err:
            log.info("slash already submitted  borrower=%s…", borrower[:10])
            _remove_watch(borrower)
            return False
        log.warning("slash execution error  borrower=%s…: %s", borrower[:10], exc)
        return False


# ── State writer ──────────────────────────────────────────────────────────────

def _write_state(stats: SlasherStats) -> None:
    now = time.time()
    queue = []
    for e in sorted(_watch.values(), key=lambda x: x.expires_at):
        queue.append({
            "borrower":        e.borrower,
            "liquidator":      e.liquidator,
            "stake_eth":       round(e.stake / 1e18, 6),
            "bounty_eth":      round(e.bounty_wei / 1e18, 6),
            "expires_at":      datetime.fromtimestamp(e.expires_at, timezone.utc).isoformat(),
            "seconds_remaining": max(0, round(e.expires_at - now)),
            "expired":         e.is_expired,
        })

    state = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "watching":   len(_watch),
        **stats.to_dict(),
        "watch_queue": queue,
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(STATE_FILE)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    assert w3.is_connected(),       "Cannot connect to RPC"
    assert w3.eth.chain_id == 8453, f"Wrong chain: expected 8453, got {w3.eth.chain_id}"

    log.info("=" * 60)
    log.info("rights_slasher starting")
    log.info("registry  : %s", REGISTRY_ADDR)
    log.info("owner     : %s", OWNER)
    log.info("rpc       : %s", RPC_URL)
    log.info("=" * 60)

    stats   = SlasherStats()
    consec_failures = 0

    # ── Backfill: scan events from the last BACKFILL_BLOCKS ───────────────────
    latest_block = w3_seq.eth.block_number
    backfill_from = max(0, latest_block - BACKFILL_BLOCKS)
    log.info("backfill  blocks %d → %d", backfill_from, latest_block)
    _fetch_events(backfill_from, latest_block)
    stats.last_block = latest_block
    log.info("backfill complete  watching=%d", len(_watch))

    # ── Main cycle ────────────────────────────────────────────────────────────
    while True:
        cycle_start = time.monotonic()

        # ── Fetch new events ──────────────────────────────────────────────────
        try:
            current_block = w3_seq.eth.block_number
            if current_block > stats.last_block:
                _fetch_events(stats.last_block + 1, current_block)
                stats.last_block = current_block
        except Exception as exc:
            log.warning("block number fetch failed: %s", exc)

        # ── Slash expired entries ─────────────────────────────────────────────
        expired = _expired_entries()
        if expired:
            log.info("%d expired entr%s to slash", len(expired), "y" if len(expired) == 1 else "ies")

        for entry in expired:
            # Circuit breaker: pause if we're hitting repeated failures
            if consec_failures >= CB_MAX_FAILURES:
                log.warning(
                    "CB: %d consecutive failures — pausing 60s", consec_failures
                )
                time.sleep(60)
                consec_failures = 0

            # On-chain verification before spending gas
            result = _verify_slashable(entry.borrower)
            if result is None:
                log.info(
                    "pre-check: not slashable  borrower=%s…  (executed or race-won)",
                    entry.borrower[:10],
                )
                _remove_watch(entry.borrower)
                continue

            liquidator_on_chain, stake_on_chain = result
            # Sync watch entry with on-chain state in case of outbid
            entry.liquidator = liquidator_on_chain
            entry.stake      = stake_on_chain

            success = _execute_slash(entry, stats)
            if success:
                consec_failures = 0
            else:
                consec_failures += 1

        # ── Write state ───────────────────────────────────────────────────────
        try:
            _write_state(stats)
        except Exception as exc:
            log.warning("state write failed: %s", exc)

        # ── Sleep to cadence ──────────────────────────────────────────────────
        elapsed   = time.monotonic() - cycle_start
        sleep_for = max(0.0, POLL_INTERVAL_SEC - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(BASE_DIR)

    _parser = argparse.ArgumentParser(description="LiquidationRightsRegistry slash bounty collector")
    _parser.add_argument("--dry-run", action="store_true", help="Watch and verify but never send transactions")
    _args = _parser.parse_args()
    if _args.dry_run:
        DRY_RUN = True   # noqa: F811

    if DRY_RUN:
        log.info("[DRY-RUN] mode active — slash transactions will be simulated only")

    # Singleton — one instance only
    _singleton_fd = open(DAEMON_LOCK, "w")
    try:
        fcntl.flock(_singleton_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.error("Another rights_slasher is already running (%s). Exiting.", DAEMON_LOCK)
        sys.exit(1)

    try:
        main()
    except KeyboardInterrupt:
        log.info("rights_slasher stopped by user")
    finally:
        fcntl.flock(_singleton_fd, fcntl.LOCK_UN)
        _singleton_fd.close()
