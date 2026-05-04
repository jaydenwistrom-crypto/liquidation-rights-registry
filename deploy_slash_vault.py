"""
deploy_slash_vault.py — Deploy SlashRevenueVault to Base mainnet.

Reads VAULT_PRIVATE_KEY + BASE_MAINNET_RPC_URL from .env.
Uses deployer as initial owner (treasury wallet).
Writes SLASH_REVENUE_VAULT back to .env on success.

After deployment:
  1. Transfer slash revenue to vault: vault.addRevenue{value: amount}()
  2. Anyone can deposit WETH at basescan to earn srvETH yield shares.
  3. In v2, set the registry treasury = vault address to route revenue automatically.

Usage:
    venv/bin/python3 deploy_slash_vault.py [--dry-run]
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv, set_key
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

# ── Bootstrap ─────────────────────────────────────────────────────────────────

BASE_DIR  = Path(__file__).resolve().parent
ENV_PATH  = BASE_DIR / ".env"
FORGE_OUT = BASE_DIR / "forge-out"
LOGS_DIR  = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
SIGNER_LOCK = LOGS_DIR / "base_8453_signer.lock"

load_dotenv(ENV_PATH)

RPC_URL  = os.environ["BASE_MAINNET_RPC_URL"]
PRIV_KEY = os.environ["VAULT_PRIVATE_KEY"]

WETH_BASE = Web3.to_checksum_address("0x4200000000000000000000000000000000000006")

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

# ── Pre-flight ─────────────────────────────────────────────────────────────────

assert w3.is_connected(),       "Cannot connect to Base mainnet"
assert w3.eth.chain_id == 8453, f"Wrong chain: expected 8453, got {w3.eth.chain_id}"

account = w3.eth.account.from_key(PRIV_KEY)
OWNER   = account.address
balance = w3.eth.get_balance(OWNER)

print(f"[*] Chain    : Base mainnet (8453)")
print(f"[*] Deployer : {OWNER}")
print(f"[*] WETH     : {WETH_BASE}")
print(f"[*] Balance  : {w3.from_wei(balance, 'ether'):.6f} ETH")

if balance < w3.to_wei(0.003, "ether"):
    sys.exit("[-] Need ≥ 0.003 ETH for deployment gas")

# ── Load artifact ─────────────────────────────────────────────────────────────

art_path = FORGE_OUT / "SlashRevenueVault.sol/SlashRevenueVault.json"
if not art_path.exists():
    sys.exit("[-] Artifact not found — run `forge build` first")

art      = json.loads(art_path.read_text())
abi      = art["abi"]
bytecode = "0x" + art["bytecode"]["object"].lstrip("0x")

# Verify ABI shape
_fns = {x["name"] for x in abi if x.get("type") == "function"}
assert "addRevenue"   in _fns, "addRevenue() missing from ABI"
assert "deposit"      in _fns, "deposit() missing from ABI"
assert "redeem"       in _fns, "redeem() missing from ABI"
assert "totalAssets"  in _fns, "totalAssets() missing from ABI"

_ctor = next((x for x in abi if x.get("type") == "constructor"), None)
assert _ctor is not None
assert [i["name"] for i in _ctor["inputs"]] == ["_weth", "_owner"], \
    f"unexpected constructor signature: {[i['name'] for i in _ctor['inputs']]}"

print(f"[*] Bytecode : {len(bytecode) // 2 - 1} bytes")

# ── Gas estimate ──────────────────────────────────────────────────────────────

factory   = w3.eth.contract(abi=abi, bytecode=bytecode)
base_fee  = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
gas_est   = factory.constructor(WETH_BASE, OWNER).estimate_gas({"from": OWNER})
gas_limit = int(gas_est * 1.30)

print(f"[*] Gas est  : {gas_est:,}  (limit: {gas_limit:,})")
print(f"[*] Base fee : {base_fee / 1e9:.4f} gwei")

# ── Dry-run guard ──────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

if args.dry_run:
    print("\n[DRY-RUN] Checks passed. Would deploy SlashRevenueVault.")
    print(f"          _weth  = {WETH_BASE}")
    print(f"          _owner = {OWNER}")
    sys.exit(0)

confirm = input("\n[?] Deploy SlashRevenueVault to Base mainnet? [yes/no] ").strip().lower()
if confirm != "yes":
    sys.exit("Aborted.")

# ── Deploy ────────────────────────────────────────────────────────────────────

with open(SIGNER_LOCK, "w") as _lf:
    fcntl.flock(_lf, fcntl.LOCK_EX)
    try:
        deploy_base_fee = w3.eth.get_block("latest").get("baseFeePerGas", base_fee)
        priority_tip    = max(deploy_base_fee * 2, 50_000_000)
        tx = factory.constructor(WETH_BASE, OWNER).build_transaction({
            "chainId":              8453,
            "gas":                  gas_limit,
            "maxFeePerGas":         deploy_base_fee * 2 + priority_tip,
            "maxPriorityFeePerGas": priority_tip,
            "nonce":                w3.eth.get_transaction_count(OWNER, "pending"),
        })
        signed  = w3.eth.account.sign_transaction(tx, PRIV_KEY)
        raw     = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        tx_hash = w3.eth.send_raw_transaction(raw)
    finally:
        fcntl.flock(_lf, fcntl.LOCK_UN)

print(f"\n[*] TX hash  : {w3.to_hex(tx_hash)}")
print("[*] Waiting for receipt…")

receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
if receipt["status"] != 1:
    sys.exit("[-] Deployment REVERTED — check tx on Basescan")

addr = receipt.get("contractAddress")
if not addr:
    time.sleep(3)
    _w2 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 15}))
    _w2.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    addr = _w2.eth.get_transaction_receipt(tx_hash)["contractAddress"]
    if not addr:
        sys.exit("[-] contractAddress is None — check tx on Basescan")

# ── Write .env before verification ────────────────────────────────────────────

set_key(str(ENV_PATH), "SLASH_REVENUE_VAULT", addr)
print(f"\n[+] Deployed  : {addr}")
print(f"[+] Gas used  : {receipt['gasUsed']:,}")
print(f"[+] Block     : {receipt['blockNumber']}")
print(f"[+] .env      : SLASH_REVENUE_VAULT={addr}")

# ── Verify on-chain state ──────────────────────────────────────────────────────

_vw3 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 15}))
_vw3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
contract = _vw3.eth.contract(address=addr, abi=abi)

def _retry(fn):
    for i in range(4):
        try: return fn()
        except Exception:
            if i < 3: time.sleep(3)
    raise RuntimeError("verification call failed")

on_chain_owner = _retry(contract.functions.owner().call)
on_chain_asset = _retry(contract.functions.asset().call)
on_chain_name  = _retry(contract.functions.name().call)
on_chain_sym   = _retry(contract.functions.symbol().call)

assert on_chain_owner.lower() == OWNER.lower(), f"owner mismatch: {on_chain_owner}"
assert on_chain_asset.lower() == WETH_BASE.lower(), f"asset mismatch: {on_chain_asset}"
assert on_chain_name  == "Slash Revenue ETH"
assert on_chain_sym   == "srvETH"

print(f"[+] owner()   verified : {on_chain_owner}")
print(f"[+] asset()   verified : {on_chain_asset} (WETH)")
print(f"[+] name()    verified : {on_chain_name}")
print(f"[+] symbol()  verified : {on_chain_sym}")

print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  DEPLOYED — SlashRevenueVault (srvETH)                          ║
║                                                                  ║
║  Address : {addr}  ║
║  Asset   : WETH (Base mainnet)                                   ║
║  Owner   : {OWNER}  ║
║                                                                  ║
║  Next steps:                                                     ║
║  1. Seed the vault (inflation attack protection):                ║
║     Call deposit(0.001 ETH worth of WETH, owner_address)         ║
║                                                                  ║
║  2. Forward slash revenue to vault:                              ║
║     vault.addRevenue{{value: amount}}()                           ║
║                                                                  ║
║  3. In v2 registry, set treasury = vault address so revenue      ║
║     flows automatically on every slash.                          ║
╚══════════════════════════════════════════════════════════════════╝
""")
