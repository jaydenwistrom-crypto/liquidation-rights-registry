"""
deploy.py — Deploy LiquidationRightsRegistry to Base mainnet.

Reads PRIVATE_KEY + BASE_RPC_URL from environment (or .env file).
Sets treasury = deployer address.

Usage:
    pip install web3 python-dotenv
    PRIVATE_KEY=0x... BASE_RPC_URL=https://mainnet.base.org python deploy.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

RPC_URL  = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
PRIV_KEY = os.environ["PRIVATE_KEY"]

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

assert w3.is_connected(),          "Cannot connect to Base mainnet"
assert w3.eth.chain_id == 8453,    f"Wrong chain: expected 8453, got {w3.eth.chain_id}"

account = w3.eth.account.from_key(PRIV_KEY)
OWNER   = account.address
balance = w3.eth.get_balance(OWNER)

print(f"[*] Chain    : Base mainnet (8453)")
print(f"[*] Deployer : {OWNER}")
print(f"[*] Balance  : {w3.from_wei(balance, 'ether'):.6f} ETH")

if balance < w3.to_wei(0.002, "ether"):
    sys.exit("[-] Need ≥ 0.002 ETH for deployment gas")

# ── Load compiled artifact ────────────────────────────────────────────────────
# Run `forge build` first (requires Foundry: https://getfoundry.sh)

art_path = Path("out/LiquidationRightsRegistry.sol/LiquidationRightsRegistry.json")
if not art_path.exists():
    sys.exit("[-] Artifact not found — run `forge build` first")

art      = json.loads(art_path.read_text())
abi      = art["abi"]
bytecode = "0x" + art["bytecode"]["object"].lstrip("0x")

_fns = {x["name"] for x in abi if x.get("type") == "function"}
assert "register"        in _fns
assert "recordExecution" in _fns
assert "slash"           in _fns
assert "hasActiveRights" in _fns

print(f"[*] Bytecode : {len(bytecode) // 2 - 1} bytes")

# ── Gas estimate ──────────────────────────────────────────────────────────────

factory  = w3.eth.contract(abi=abi, bytecode=bytecode)
base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
gas_est  = factory.constructor(OWNER).estimate_gas({"from": OWNER})
gas_limit = int(gas_est * 1.30)

print(f"[*] Gas est  : {gas_est:,}  (limit: {gas_limit:,})")
print(f"[*] Base fee : {base_fee / 1e9:.4f} gwei")

# ── Dry-run ───────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

if args.dry_run:
    print("\n[DRY-RUN] Checks passed. Would deploy with treasury =", OWNER)
    sys.exit(0)

confirm = input("\n[?] Deploy LiquidationRightsRegistry to Base mainnet? [yes/no] ").strip().lower()
if confirm != "yes":
    sys.exit("Aborted.")

# ── Deploy ────────────────────────────────────────────────────────────────────

deploy_base_fee = w3.eth.get_block("latest").get("baseFeePerGas", base_fee)
priority_tip    = max(deploy_base_fee * 2, 50_000_000)
tx = factory.constructor(OWNER).build_transaction({
    "chainId":              8453,
    "gas":                  gas_limit,
    "maxFeePerGas":         deploy_base_fee * 2 + priority_tip,
    "maxPriorityFeePerGas": priority_tip,
    "nonce":                w3.eth.get_transaction_count(OWNER, "pending"),
})
signed  = w3.eth.account.sign_transaction(tx, PRIV_KEY)
raw     = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
tx_hash = w3.eth.send_raw_transaction(raw)

print(f"\n[*] TX hash  : {w3.to_hex(tx_hash)}")
print("[*] Waiting for receipt…")

import time
receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
if receipt["status"] != 1:
    sys.exit("[-] Deployment REVERTED")

addr = receipt.get("contractAddress")
if not addr:
    time.sleep(3)
    _w2 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 15}))
    _w2.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    addr = _w2.eth.get_transaction_receipt(tx_hash)["contractAddress"]

print(f"\n[+] Deployed : {addr}")
print(f"[+] Gas used : {receipt['gasUsed']:,}")
print(f"[+] Block    : {receipt['blockNumber']}")
print(f"\n    Set LIQUIDATION_RIGHTS_REGISTRY={addr} in your environment.")
