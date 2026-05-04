# Liquidation Rights Protocol

**Base Mainnet** · `LiquidationRightsRegistryV2`

---

## The problem

Every DeFi liquidator runs the same race. A position crosses HF 1.0. Five bots see it simultaneously. They all submit the same transaction with escalating gas tips. One wins. Four burn gas and get nothing. The winner pays a bloated tip to the sequencer. The borrower loses more collateral than necessary.

Gas wars are a tax on coordination failure. The winners pay for the losers' mistakes.

---

## What this protocol does

A liquidator stakes a small amount of ETH to claim **priority rights** on a specific borrower address for a time-bounded window. During that window, the registered liquidator executes the liquidation through their own infrastructure — whatever contracts they use — and then calls `recordExecution()` to reclaim their stake.

If they don't execute within the window, anyone can call `slash()` to collect 50% of the stake as a bounty. The other 50% routes automatically to the `SlashRevenueVaultV2` — wrapping to WETH and immediately increasing the srvETH share price for all depositors.

The protocol does not restrict Aave's permissionless `liquidationCall`. It creates an **economic coordination layer**: respecting registrations is strictly more profitable than ignoring them, because the alternative is competing in a gas war where the expected value is negative for most participants.

---

## Flow

```
1. Target enters at-risk zone (HF drops below ~1.05)

2. Liquidator calls register(borrower) with >= 0.005 ETH stake
   → receives priority rights for the current window (10 min default)

3. Liquidator executes the Aave liquidation via their own contracts
   → no change to their existing liquidation infrastructure

4. Liquidator calls recordExecution(borrower)
   → stake returned in full

   OR

4b. Window expires without execution
   → anyone calls slash(borrower)
   → slasher receives 50% of stake as bounty
   → 50% ETH routes to SlashRevenueVaultV2.receive()
   → auto-wrapped to WETH
   → srvETH share price rises for all depositors
```

---

## Contracts (v3 — current)

```
Network : Base Mainnet (chain ID 8453)

LiquidationRightsRegistryV2 : 0x51f338c1c1721d74b5feFAfbA5f067f7F850226A
SlashRevenueVaultV2          : 0xe792bcD8f6Eb30eAFE3a99dC87693F098839d77F
  Asset    : WETH (0x4200000000000000000000000000000000000006)
  Share    : srvETH
```

### Previous versions

```
LiquidationRightsRegistry (v1) : 0x8014d6bef9f17168E7b0Ea3CeacC57609e51ceEf
SlashRevenueVault (v1)          : 0x563666C16Ae4B6096245608CF10a453C7389A6CD
```

### Key parameters

| Parameter | Value |
|---|---|
| `SLASH_BOUNTY_BPS` | 5000 (50% to slasher — protocol invariant) |
| `window` | 10 minutes (owner-adjustable: 5–60 min) |
| `minStake` | 0.005 ETH (owner-adjustable: 0.001–1 ETH) |
| Treasury | `SlashRevenueVaultV2` — automatic revenue routing |

### Registry interface

```solidity
// Stake ETH to claim rights on a borrower.
// Must outbid current holder (2x stake) if active rights exist.
function register(address borrower) external payable;

// After successful liquidation — reclaims your stake.
function recordExecution(address borrower) external;

// Slash an expired rights holder. Caller gets 50% of their stake.
// Remaining 50% routes automatically to the yield vault.
function slash(address borrower) external;

// Check if a specific address holds active rights.
function hasActiveRights(address borrower, address liquidator)
    external view returns (bool);

// Full rights state for a borrower.
function getRights(address borrower) external view returns (
    address liquidator,
    uint256 stake,
    uint256 expiresAt,
    bool    executed,
    bool    active
);

// Protocol version.
function version() external pure returns (string memory); // "2.0.0"
```

---

## SlashRevenueVaultV2 (srvETH)

**ERC-4626 yield vault powered by automatic slash revenue.**

Anyone can deposit WETH and earn yield from the protocol's slash activity — no liquidation infrastructure needed.

**How yield accrues (v3 — fully automatic):**
1. Liquidator registers on a borrower, stakes ETH.
2. They miss their window (doesn't execute the liquidation).
3. Anyone calls `slash(borrower)` — 50% bounty to slasher, 50% ETH sent to vault.
4. Vault's `receive()` auto-wraps ETH → WETH.
5. `totalAssets()` increases, `totalSupply()` unchanged → srvETH share price rises.
6. All depositors earn proportional yield. No manual step required.

There are no fees, no lock periods, and no minimum deposit. The only yield source is protocol revenue. If no registrations are slashed, the share price stays flat.

```typescript
// TypeScript — deposit WETH, receive srvETH
const VAULT = '0xe792bcD8f6Eb30eAFE3a99dC87693F098839d77F'
const WETH  = '0x4200000000000000000000000000000000000006'

// Approve WETH, then deposit
await walletClient.writeContract({
  address: VAULT,
  abi: VAULT_ABI,
  functionName: 'deposit',
  args: [parseEther('1'), myAddress],   // deposit 1 WETH, receive srvETH
})
```

---

## Integration (TypeScript / viem)

```bash
npm install viem
```

```typescript
import { createRegistryClient } from './client'

const registry = createRegistryClient({
  rpcUrl:     'https://mainnet.base.org',
  privateKey: process.env.PRIVATE_KEY as `0x${string}`,
})

// Before firing — check if someone else holds priority
if (await registry.otherHoldsRights(borrower)) {
  console.log('backing off — another liquidator has rights')
} else {
  await registry.register(borrower)          // stake 0.005 ETH
  // ... execute your Aave liquidation here (unchanged) ...
  await registry.recordExecution(borrower)   // reclaims stake
}

// Slash a registration that expired without execution (earn 50% bounty)
if (await registry.isSlashable(borrower)) {
  const tx = await registry.slash(borrower)
  console.log('slashed:', tx)
}
```

Full TypeScript client: [`client.ts`](client.ts)

---

## Integration (Python / web3.py)

```python
from web3 import Web3

REGISTRY = "0x51f338c1c1721d74b5feFAfbA5f067f7F850226A"
REGISTRY_ABI = [...]  # see liquidation_rights_client.py

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
registry = w3.eth.contract(address=REGISTRY, abi=REGISTRY_ABI)

# Before firing — check if someone else has rights
liq, stake, expires, executed, active = registry.functions.getRights(borrower).call()
if active and liq.lower() != my_address.lower():
    print("backing off — another liquidator has rights")
else:
    # Register (stake 0.005 ETH)
    tx = registry.functions.register(
        Web3.to_checksum_address(borrower)
    ).build_transaction({
        "value": int(0.005e18),
        "from":  my_address,
        "nonce": w3.eth.get_transaction_count(my_address, "pending"),
        ...
    })
    # sign and send

    # Execute your liquidation here (unchanged from your existing flow)

    # Reclaim stake after success
    tx = registry.functions.recordExecution(
        Web3.to_checksum_address(borrower)
    ).build_transaction({...})
    # sign and send
```

Full Python client: [`liquidation_rights_client.py`](liquidation_rights_client.py)

---

## Economics

### For liquidators

| Scenario | Outcome |
|---|---|
| Register → execute → record | Stake returned. No gas war. Full profit. |
| Register → someone else fires | Your stake stays locked until window expires, then you can claim a refund by re-registering. Or wait for the slasher bounty if they forfeited. |
| Don't register, compete raw | Gas war. Expected value decreases as more bots compete. |

The dominant strategy: register on any target you intend to liquidate. The coordination cost (0.005 ETH stake) is trivial against even a modest liquidation profit.

### For passive yield earners (v3 — automatic)

The 50% treasury share from every slashed registration routes directly to `SlashRevenueVaultV2`. Deposit WETH, receive srvETH, earn yield automatically as the protocol accrues slash revenue. No liquidation infrastructure required, no manual revenue injection — every slash increases your share price in the same block.

---

## Outbidding

If a registered liquidator is slow and you're faster, stake 2x their amount to unseat them. Their original stake is immediately refunded. You take priority for the next window period.

This creates a price discovery mechanism for liquidation priority: the market determines what exclusive windows are worth based on the profit available in the position.

---

## Why this works without Aave changes

A rational liquidator compares two strategies:

**A) Register → execute without gas competition**
- Cost: 0.005 ETH stake (returned on success) + execution gas + small registration gas
- Revenue: full liquidation profit

**B) Don't register → compete in gas war**
- Cost: escalating gas tips + execution gas (paid regardless of win/loss)
- Revenue: liquidation profit × win probability

As more liquidators use Strategy A, Strategy B becomes strictly worse. The protocol reaches Nash equilibrium when registration is universal — gas wars stop because everyone expects to face registered competition, and jumping a registration costs more (via escalating outbid) than registering first.

---

## Source

```
contracts/LiquidationRightsRegistryV2.sol   ← active (v3)
contracts/SlashRevenueVaultV2.sol            ← active (v3)
contracts/LiquidationRightsRegistry.sol     ← v1 (reference)
contracts/SlashRevenueVault.sol              ← v1 (reference)

test/LiquidationRightsRegistryV2Test.t.sol  (30 tests, all passing)
test/SlashRevenueVaultV2Test.t.sol          (15 tests, all passing)
test/LiquidationRightsRegistryTest.t.sol    (21 tests, all passing)
test/SlashRevenueVaultTest.t.sol            (15 tests, all passing)

liquidation_rights_client.py                Python integration client
client.ts                                   TypeScript/viem integration client
deploy_v3.py                                Deploy vault + registry v3
deploy_slash_vault.py                       Deploy vault standalone
deploy.py                                   Deploy registry v1
```

---

## Roadmap

- **v1 (deployed)**: Core registration + slash + coordination. Treasury accumulates as protocol fees.
- **v2 (deployed)**: `SlashRevenueVault` — ERC-4626 yield on slash revenue. Manual revenue injection by owner.
- **v3 (deployed)**: `SlashRevenueVaultV2` + `LiquidationRightsRegistryV2` — treasury = vault address, fully automatic ETH → WETH routing on every slash. Owner-adjustable window and minStake with safety bounds.
- **v4 (planned)**: Cross-protocol support — Morpho, Euler, Compound v3.
