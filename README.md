# Liquidation Rights Protocol

**Base Mainnet** · `LiquidationRightsRegistry`

---

## The problem

Every DeFi liquidator runs the same race. A position crosses HF 1.0. Five bots see it simultaneously. They all submit the same transaction with escalating gas tips. One wins. Four burn gas and get nothing. The winner pays a bloated tip to the sequencer. The borrower loses more collateral than necessary.

Gas wars are a tax on coordination failure. The winners pay for the losers' mistakes.

---

## What this protocol does

A liquidator stakes a small amount of ETH to claim **priority rights** on a specific borrower address for a 10-minute window. During that window, the registered liquidator executes the liquidation through their own infrastructure — whatever contracts they use — and then calls `recordExecution()` to reclaim their stake.

If they don't execute within the window, anyone can call `slash()` to collect 50% of the stake as a bounty. The other 50% accumulates in a treasury (future LP yield vault).

The protocol does not restrict Aave's permissionless `liquidationCall`. It creates an **economic coordination layer**: respecting registrations is strictly more profitable than ignoring them, because the alternative is competing in a gas war where the expected value is negative for most participants.

---

## Flow

```
1. Target enters at-risk zone (HF drops below ~1.05)

2. Liquidator calls register(borrower) with >= 0.005 ETH stake
   → receives priority rights for 10 minutes

3. Liquidator executes the Aave liquidation via their own contracts
   → no change to their existing liquidation infrastructure

4. Liquidator calls recordExecution(borrower)
   → stake returned in full

   OR

4b. Window expires without execution
   → anyone calls slash(borrower)
   → slasher receives 50% of stake as bounty
   → 50% goes to treasury
```

---

## Contract

```
Network  : Base Mainnet (chain ID 8453)
Address  : 0x8014d6bef9f17168E7b0Ea3CeacC57609e51ceEf
Verified : Basescan
```

### Key parameters

| Parameter | Value |
|---|---|
| `WINDOW` | 10 minutes |
| `MIN_STAKE` | 0.005 ETH |
| `SLASH_BOUNTY_BPS` | 5000 (50% to slasher) |
| Treasury | Protocol treasury (v2: LP yield vault) |

### Interface

```solidity
// Stake ETH to claim rights on a borrower.
// Must outbid current holder (2x stake) if active rights exist.
function register(address borrower) external payable;

// After successful liquidation — reclaims your stake.
function recordExecution(address borrower) external;

// Slash an expired rights holder. Caller gets 50% of their stake.
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

REGISTRY = "0x8014d6bef9f17168E7b0Ea3CeacC57609e51ceEf"
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

The dominant strategy: register on any target you intend to liquidate. The coordination cost (0.005 ETH stake, ~$11) is trivial against even a $500 liquidation profit.

### For everyone else (v2 — LP vault)

The 50% treasury share from every slashed registration accumulates. In v2, the treasury becomes a yield-bearing LP vault: deposit ETH, earn yield from failed liquidation registrations. No liquidation infrastructure required.

---

## Outbidding

If a registered liquidator is slow and you're faster, stake 2x their amount to unseat them. Their original stake is immediately refunded. You take priority for the next 10 minutes.

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
contracts/LiquidationRightsRegistry.sol
foundry/LiquidationRightsRegistryTest.t.sol   (21 tests, all passing)
liquidation_rights_client.py                  (Python integration client)
deploy_rights_registry.py                     (deployment script)
```

---

## Roadmap

- **v1 (now)**: Core registration + slash + coordination. Deployed. Public.
- **v2**: LP yield vault. Treasury becomes a managed ERC-4626 vault. Depositors earn yield from slash revenue passively.
- **v3**: Cross-protocol. Extend coordination layer to Morpho, Euler, Compound v3 positions on Base.
