"""
protocol_stats.py — Live status board for the Liquidation Rights Protocol.

Reads on-chain state every POLL_INTERVAL seconds and prints a compact board:
  - SlashRevenueVaultV2 : totalAssets (WETH), srvETH supply, share price
  - LiquidationRightsRegistryV2 : active registrations, window, minStake, version
  - Slasher daemon stats
  - Cross-protocol scanner stats (Morpho, Compound v3, Aave v3)

Usage:
    venv/bin/python3 protocol_stats.py              # one-shot
    venv/bin/python3 protocol_stats.py --watch      # continuous, refreshes every 30s
    venv/bin/python3 protocol_stats.py --watch 60   # custom interval (seconds)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RPC_URL      = os.environ.get("BASE_MAINNET_RPC_URL", "https://mainnet.base.org")
VAULT_V2     = Web3.to_checksum_address(os.environ["SLASH_REVENUE_VAULT_V2"])
VAULT_V1     = Web3.to_checksum_address(os.environ["SLASH_REVENUE_VAULT"])
REGISTRY_V2  = Web3.to_checksum_address(os.environ["LIQUIDATION_RIGHTS_REGISTRY_V2"])
REGISTRY_V1  = Web3.to_checksum_address(os.environ.get("LIQUIDATION_RIGHTS_REGISTRY",
                "0x8014d6bef9f17168E7b0Ea3CeacC57609e51ceEf"))

SLASH_LOG    = BASE_DIR / "logs" / "slash_events.jsonl"
SLASHER_STATE= BASE_DIR / "logs" / "slasher_state.json"
LOGS_DIR     = BASE_DIR / "logs"

_SCANNER_CONFIGS = [
    ("Morpho Blue",    "morpho_scanner_state.json",   "morpho_scanner.lock"),
    ("Compound v3",    "compound_scanner_state.json",  "compound_scanner.lock"),
    ("Aave v3",        "aave_scanner_state.json",      "aave_scanner.lock"),
]


def _load_abi(name: str) -> list:
    art_path = BASE_DIR / "forge-out" / f"{name}.sol" / f"{name}.json"
    if not art_path.exists():
        return []
    return json.loads(art_path.read_text())["abi"]


def _w3() -> Web3:
    w = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
    w.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w


def _slash_history() -> tuple[int, float]:
    """Return (count, total_bounty_eth) from slash_events.jsonl."""
    if not SLASH_LOG.exists():
        return 0, 0.0
    count = 0
    total = 0.0
    for line in SLASH_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            count += 1
            total += float(ev.get("bounty_eth", ev.get("bounty_wei", 0)) or 0)
        except Exception:
            pass
    return count, total


def _slasher_state() -> dict:
    if not SLASHER_STATE.exists():
        return {}
    try:
        return json.loads(SLASHER_STATE.read_text())
    except Exception:
        return {}


def _fmt_eth(wei: int) -> str:
    return f"{wei / 1e18:.6f}"


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def print_board(w: Web3) -> None:
    vault_abi    = _load_abi("SlashRevenueVaultV2")
    reg_abi      = _load_abi("LiquidationRightsRegistryV2")

    now     = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    block   = w.eth.block_number

    # ── Vault V2 ──────────────────────────────────────────────────────────────
    vault = w.eth.contract(address=VAULT_V2, abi=vault_abi)
    try:
        total_assets = vault.functions.totalAssets().call()
        total_supply = vault.functions.totalSupply().call()
        # share price: 1e6 virtual shares offset means supply baseline is 1e6
        # price = totalAssets / (totalSupply / 1e6 + 1) but simpler: convertToAssets(1e18)
        share_price_raw = vault.functions.convertToAssets(10**18).call()
        share_price = share_price_raw / 1e18
        vault_ok = True
    except Exception as e:
        total_assets = total_supply = share_price_raw = 0
        share_price = 0.0
        vault_ok = False
        vault_err = str(e)

    # ── Registry V2 ───────────────────────────────────────────────────────────
    reg = w.eth.contract(address=REGISTRY_V2, abi=reg_abi)
    try:
        treasury  = reg.functions.treasury().call()
        window    = reg.functions.window().call()
        min_stake = reg.functions.minStake().call()
        version   = reg.functions.version().call()
        reg_ok = True
    except Exception as e:
        treasury = window = min_stake = version = None
        reg_ok = False
        reg_err = str(e)

    # ── Slash history ─────────────────────────────────────────────────────────
    slash_count, slash_bounty_eth = _slash_history()
    state = _slasher_state()
    watching    = state.get("watching", 0)
    lifetime_sl = state.get("lifetime_slashes", slash_count)
    lifetime_b  = state.get("lifetime_bounty_wei", 0)
    races_lost  = state.get("races_lost", 0)
    cb_failures = state.get("cb_failures", 0)
    daemon_alive = (BASE_DIR / "logs" / "rights_slasher.lock").exists()

    # ── Print board ───────────────────────────────────────────────────────────
    W = 64   # inner width (between ║ and ║)
    bar = "═" * W

    def row(label: str, value: str) -> str:
        content = f"  {label:<16}: {value}"
        return f"║{content:<{W}}║"

    def section(title: str) -> str:
        return f"║  {title:<{W-2}}║"

    def divider() -> str:
        return f"╠{bar}╣"

    print(f"\n╔{bar}╗")
    print(section("LIQUIDATION RIGHTS PROTOCOL — STATUS BOARD"))
    print(section(f"{now}   block {block:,}"))
    print(divider())

    print(section(f"SlashRevenueVaultV2  {VAULT_V2[:20]}…"))
    if vault_ok:
        print(row("totalAssets", f"{_fmt_eth(total_assets)} WETH"))
        print(row("totalSupply", f"{total_supply / 1e24:.6f} srvETH"))
        print(row("share price", f"{share_price:.8f} WETH / srvETH"))
        treasury_match = treasury and treasury.lower() == VAULT_V2.lower()
    else:
        print(section(f"  [RPC error: {vault_err[:50]}]"))
        treasury_match = False

    print(divider())
    print(section(f"LiquidationRightsRegistryV2  {REGISTRY_V2[:20]}…"))
    if reg_ok:
        t_label = "← vault ✓" if treasury_match else (treasury or "none")
        print(row("treasury", t_label))
        print(row("window", f"{window // 60} min"))
        print(row("minStake", f"{min_stake / 1e18:.4f} ETH"))
        print(row("version", version))
    else:
        print(section(f"  [RPC error: {reg_err[:50]}]"))

    print(divider())
    print(section("SLASHER DAEMON"))
    print(row("status", "running ✓" if daemon_alive else "NOT RUNNING ✗"))
    print(row("watching", f"{watching} positions"))
    print(row("slashes", f"{lifetime_sl} lifetime"))
    bounty_eth = lifetime_b / 1e18 if lifetime_b else slash_bounty_eth
    print(row("bounty earned", f"{bounty_eth:.6f} ETH"))
    print(row("races lost", str(races_lost)))
    if cb_failures > 0:
        print(row("⚠ CB failures", str(cb_failures)))

    # ── Cross-protocol scanners ────────────────────────────────────────────────
    print(divider())
    print(section("CROSS-PROTOCOL SCANNERS"))
    total_watching = 0
    total_registered = 0
    for label, state_file, lock_file in _SCANNER_CONFIGS:
        sf = LOGS_DIR / state_file
        running = (LOGS_DIR / lock_file).exists()
        if sf.exists():
            try:
                sd = json.loads(sf.read_text())
                stats = sd.get("stats", {})
                w_count = stats.get("watching", 0)
                r_count = stats.get("registrations", 0)
                sc_count = stats.get("scans", 0)
                wl = sd.get("watchlist", [])
                at_risk = sum(1 for e in wl if e.get("last_hf", 9) < 1.05 or e.get("is_liquidatable"))
                total_watching  += w_count
                total_registered += r_count
                status = "up" if running else "down"
                print(row(label, f"{w_count} watching  {at_risk} at-risk  {r_count} reg  [{status}]"))
            except Exception:
                print(row(label, "[state unreadable]"))
        else:
            print(row(label, "not started"))
    print(row("TOTAL", f"{total_watching} watching  {total_registered} registered"))

    print(f"╚{bar}╝\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", nargs="?", const=30, type=int, metavar="SECS",
                        help="refresh continuously (default 30s)")
    args = parser.parse_args()

    w = _w3()
    if not w.is_connected():
        sys.exit("Cannot connect to RPC")

    if args.watch is None:
        print_board(w)
    else:
        interval = args.watch
        print(f"[protocol_stats] watching — refresh every {interval}s  (Ctrl-C to stop)")
        while True:
            try:
                print_board(w)
            except Exception as e:
                print(f"[ERROR] {e}")
            time.sleep(interval)


if __name__ == "__main__":
    main()
