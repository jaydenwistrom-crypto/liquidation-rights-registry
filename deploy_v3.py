"""
deploy_v3.py — Deploy SlashRevenueVaultV2 + LiquidationRightsRegistryV2 to Base mainnet.

V3 of the LiquidationRightsProtocol:
  - SlashRevenueVaultV2: ERC-4626, receive() auto-wraps ETH → WETH → yield
  - LiquidationRightsRegistryV2: mutable treasury, window, minStake + version()
  - Registry treasury = vault address → every slash auto-routes 50% to srvETH holders

Writes to .env:
  SLASH_REVENUE_VAULT_V2
  LIQUIDATION_RIGHTS_REGISTRY_V2

Usage:
    venv/bin/python3 deploy_v3.py [--dry-run]
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
WINDOW    = 600    # 10 minutes (seconds)
MIN_STAKE = int(0.005e18)

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

if balance < w3.to_wei(0.01, "ether"):
    sys.exit("[-] Need ≥ 0.01 ETH for two deployments")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_artifact(name: str) -> tuple[list, str]:
    art_path = FORGE_OUT / f"{name}.sol/{name}.json"
    if not art_path.exists():
        sys.exit(f"[-] Artifact {art_path} not found — run `forge build` first")
    art      = json.loads(art_path.read_text())
    abi      = art["abi"]
    bytecode = "0x" + art["bytecode"]["object"].lstrip("0x")
    return abi, bytecode


def _deploy(name: str, abi: list, bytecode: str, *ctor_args) -> str:
    """Deploy a contract, return its address. Writes .env BEFORE verification."""
    factory  = w3.eth.contract(abi=abi, bytecode=bytecode)
    base_fee = w3.eth.get_block("latest").get("baseFeePerGas", 1_000_000)
    gas_est  = factory.constructor(*ctor_args).estimate_gas({"from": OWNER})
    gas_limit = int(gas_est * 1.30)
    print(f"[*] {name}  gas_est={gas_est:,}  limit={gas_limit:,}")

    with open(SIGNER_LOCK, "w") as _lf:
        fcntl.flock(_lf, fcntl.LOCK_EX)
        try:
            deploy_base_fee = w3.eth.get_block("latest").get("baseFeePerGas", base_fee)
            tip  = max(deploy_base_fee * 2, 50_000_000)
            tx   = factory.constructor(*ctor_args).build_transaction({
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

    addr = receipt.get("contractAddress")
    if not addr:
        time.sleep(3)
        _w2 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 15}))
        _w2.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        addr = _w2.eth.get_transaction_receipt(tx_hash)["contractAddress"]
        if not addr:
            sys.exit(f"[-] {name} contractAddress is None")

    print(f"[+] {name}  : {addr}  (gas={receipt['gasUsed']:,}  block={receipt['blockNumber']})")
    return addr


def _verify_call(fn):
    for i in range(4):
        try: return fn()
        except Exception:
            if i < 3: time.sleep(3)
    raise RuntimeError("verification failed")


# ── Dry-run ───────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()

if args.dry_run:
    vault_abi, vault_bc = _load_artifact("SlashRevenueVaultV2")
    reg_abi,   reg_bc   = _load_artifact("LiquidationRightsRegistryV2")
    print(f"[DRY-RUN] SlashRevenueVaultV2    bytecode={len(vault_bc)//2-1} bytes")
    print(f"[DRY-RUN] LiquidationRightsRegistryV2 bytecode={len(reg_bc)//2-1} bytes")
    print(f"[DRY-RUN] vault args: _weth={WETH_BASE}  _owner={OWNER}")
    print(f"[DRY-RUN] registry args: _treasury=<vault>  _window={WINDOW}  _minStake={MIN_STAKE}  _owner={OWNER}")
    sys.exit(0)

confirm = input("\n[?] Deploy V3 (vault + registry) to Base mainnet? [yes/no] ").strip().lower()
if confirm != "yes":
    sys.exit("Aborted.")

# ── Deploy vault first ────────────────────────────────────────────────────────

print("\n── Deploying SlashRevenueVaultV2 ──────────────────────────────────────")
vault_abi, vault_bc = _load_artifact("SlashRevenueVaultV2")
vault_addr = _deploy("SlashRevenueVaultV2", vault_abi, vault_bc, WETH_BASE, OWNER)
set_key(str(ENV_PATH), "SLASH_REVENUE_VAULT_V2", vault_addr)
print(f"[+] .env: SLASH_REVENUE_VAULT_V2={vault_addr}")

time.sleep(2)   # let the node settle before next nonce query

# ── Deploy registry with vault as treasury ────────────────────────────────────

print("\n── Deploying LiquidationRightsRegistryV2 ──────────────────────────────")
reg_abi, reg_bc = _load_artifact("LiquidationRightsRegistryV2")
reg_addr = _deploy(
    "LiquidationRightsRegistryV2", reg_abi, reg_bc,
    vault_addr,   # treasury = vault
    WINDOW,
    MIN_STAKE,
    OWNER,
)
set_key(str(ENV_PATH), "LIQUIDATION_RIGHTS_REGISTRY_V2", reg_addr)
print(f"[+] .env: LIQUIDATION_RIGHTS_REGISTRY_V2={reg_addr}")

# ── Verify on-chain state ──────────────────────────────────────────────────────

print("\n── Verifying ──────────────────────────────────────────────────────────")
_vw3 = Web3(Web3.HTTPProvider("https://mainnet.base.org", request_kwargs={"timeout": 15}))
_vw3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

vault_c = _vw3.eth.contract(address=vault_addr, abi=vault_abi)
reg_c   = _vw3.eth.contract(address=reg_addr,   abi=reg_abi)

v_asset  = _verify_call(vault_c.functions.asset().call)
v_name   = _verify_call(vault_c.functions.name().call)
v_sym    = _verify_call(vault_c.functions.symbol().call)
v_owner  = _verify_call(vault_c.functions.owner().call)

r_treasury = _verify_call(reg_c.functions.treasury().call)
r_window   = _verify_call(reg_c.functions.window().call)
r_minstake = _verify_call(reg_c.functions.minStake().call)
r_version  = _verify_call(reg_c.functions.version().call)
r_owner    = _verify_call(reg_c.functions.owner().call)

assert v_asset.lower()  == WETH_BASE.lower()
assert v_name           == "Slash Revenue ETH"
assert v_sym            == "srvETH"
assert v_owner.lower()  == OWNER.lower()
assert r_treasury.lower()== vault_addr.lower()
assert r_window         == WINDOW
assert r_minstake       == MIN_STAKE
assert r_version        == "2.0.0"
assert r_owner.lower()  == OWNER.lower()

print(f"[+] Vault  asset()    : {v_asset} (WETH)")
print(f"[+] Vault  symbol()   : {v_sym}")
print(f"[+] Vault  owner()    : {v_owner}")
print(f"[+] Reg    treasury() : {r_treasury}  ← vault ✓")
print(f"[+] Reg    window()   : {r_window}s ({r_window//60} min)")
print(f"[+] Reg    minStake() : {r_minstake/1e18:.4f} ETH")
print(f"[+] Reg    version()  : {r_version}")

print(f"""
╔══════════════════════════════════════════════════════════════════╗
║  V3 DEPLOYED — Full automatic revenue routing                   ║
║                                                                  ║
║  SlashRevenueVaultV2    : {vault_addr}  ║
║  LiquidationRightsV2    : {reg_addr}  ║
║                                                                  ║
║  Flow:                                                           ║
║    slash(borrower) → 50% bounty to caller                        ║
║                   → 50% ETH to vault.receive()                   ║
║                   → auto-wrapped WETH                            ║
║                   → srvETH share price increases                 ║
║                   → all depositors earn yield                    ║
║                                                                  ║
║  Next steps:                                                     ║
║  1. Seed vault: deposit WETH to {vault_addr[:20]}...  ║
║  2. Update LIQUIDATION_RIGHTS_REGISTRY in .env → _V2 address     ║
║  3. Restart rights_slasher.py + JIT executors                    ║
║  4. Point public repo README to new addresses                    ║
╚══════════════════════════════════════════════════════════════════╝
""")
