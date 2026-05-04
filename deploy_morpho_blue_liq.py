"""
deploy_morpho_blue_liq.py — Deploy MorphoBlueRightsLiquidator to Base mainnet.

Writes MORPHO_BLUE_RIGHTS_LIQUIDATOR to .env on success.

Usage:
    venv/bin/python3 deploy_morpho_blue_liq.py [--dry-run]
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

BASE_DIR  = Path(__file__).resolve().parent
ENV_PATH  = BASE_DIR / ".env"
FORGE_OUT = BASE_DIR / "forge-out"
LOGS_DIR  = BASE_DIR / "logs"
SIGNER_LOCK = LOGS_DIR / "base_8453_signer.lock"

load_dotenv(ENV_PATH)

RPC_URL  = os.environ["BASE_MAINNET_RPC_URL"]
PRIV_KEY = os.environ["VAULT_PRIVATE_KEY"]

w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

assert w3.is_connected(),       "Cannot connect to Base mainnet"
assert w3.eth.chain_id == 8453, f"Wrong chain: {w3.eth.chain_id}"

account = w3.eth.account.from_key(PRIV_KEY)
OWNER   = account.address
balance = w3.eth.get_balance(OWNER)

print(f"[*] Chain    : Base mainnet (8453)")
print(f"[*] Deployer : {OWNER}")
print(f"[*] Balance  : {w3.from_wei(balance, 'ether'):.6f} ETH")

if balance < w3.to_wei(0.003, "ether"):
    sys.exit("[-] Need ≥ 0.003 ETH for deployment")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_artifact(name: str) -> tuple[list, str]:
    art_path = FORGE_OUT / f"{name}.sol/{name}.json"
    if not art_path.exists():
        sys.exit(f"[-] Artifact not found — run `forge build` first: {art_path}")
    art      = json.loads(art_path.read_text())
    abi      = art["abi"]
    bytecode = "0x" + art["bytecode"]["object"].lstrip("0x")
    return abi, bytecode


def _deploy(name: str, abi: list, bytecode: str, *ctor_args) -> str:
    factory   = w3.eth.contract(abi=abi, bytecode=bytecode)
    base_fee  = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
    gas_est   = factory.constructor(*ctor_args).estimate_gas({"from": OWNER})
    gas_limit = int(gas_est * 1.30)
    print(f"[*] {name}  gas_est={gas_est:,}  limit={gas_limit:,}")

    with open(SIGNER_LOCK, "w") as _lf:
        fcntl.flock(_lf, fcntl.LOCK_EX)
        try:
            deploy_base_fee = w3.eth.get_block("latest").get("baseFeePerGas", base_fee)
            tip    = max(deploy_base_fee * 2, 50_000_000)
            tx     = factory.constructor(*ctor_args).build_transaction({
                "chainId":              8453,
                "gas":                  gas_limit,
                "maxFeePerGas":         deploy_base_fee * 2 + tip,
                "maxPriorityFeePerGas": tip,
                "nonce":                w3.eth.get_transaction_count(OWNER, "pending"),
            })
            signed  = account.sign_transaction(tx)
            raw     = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
            tx_hash = w3.eth.send_raw_transaction(raw)
        finally:
            fcntl.flock(_lf, fcntl.LOCK_UN)

    print(f"[*] TX       : {w3.to_hex(tx_hash)}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt["status"] != 1:
        sys.exit(f"[-] {name} REVERTED")

    addr = receipt["contractAddress"]
    print(f"[+] {name} : {addr}  (gas={receipt['gasUsed']:,}  block={receipt['blockNumber']})")
    return addr


# ── Main ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

abi, bytecode = _load_artifact("MorphoBlueRightsLiquidator")

if args.dry_run:
    print(f"[DRY-RUN] MorphoBlueRightsLiquidator  bytecode={len(bytecode)//2-1} bytes")
    print(f"[DRY-RUN] constructor arg: _owner={OWNER}")
    sys.exit(0)

confirm = input("\n[?] Deploy MorphoBlueRightsLiquidator to Base mainnet? [yes/no] ").strip().lower()
if confirm != "yes":
    sys.exit("Aborted.")

print("\n── Deploying MorphoBlueRightsLiquidator ──────────────────────────────")
addr = _deploy("MorphoBlueRightsLiquidator", abi, bytecode, OWNER)
set_key(str(ENV_PATH), "MORPHO_BLUE_RIGHTS_LIQUIDATOR", addr)
print(f"[+] .env: MORPHO_BLUE_RIGHTS_LIQUIDATOR={addr}")
print(f"\n[+] Start executor: venv/bin/python3 morpho_rights_executor.py")
