/**
 * client.ts — TypeScript/viem integration for LiquidationRightsRegistry.
 *
 * Works in Node.js, Bun, or any browser bundler (Vite, Webpack, etc.).
 * Uses viem v2.x.
 *
 * Install:
 *   npm install viem
 *   # or: bun add viem
 *
 * Usage:
 *   import { createRegistryClient } from './client'
 *
 *   const registry = createRegistryClient({
 *     rpcUrl:     'https://mainnet.base.org',
 *     privateKey: process.env.PRIVATE_KEY as `0x${string}`,
 *   })
 *
 *   // Before firing your liquidation:
 *   if (await registry.otherHoldsRights(borrower)) {
 *     console.log('backing off — another liquidator has priority')
 *   } else {
 *     await registry.register(borrower)
 *     // ... execute your Aave liquidation here ...
 *     await registry.recordExecution(borrower)  // reclaims stake
 *   }
 */

import {
  createPublicClient,
  createWalletClient,
  http,
  parseEther,
  type Address,
  type Hash,
  type PublicClient,
  type WalletClient,
} from 'viem'
import { base } from 'viem/chains'
import { privateKeyToAccount } from 'viem/accounts'

// ── Contract ──────────────────────────────────────────────────────────────────

export const REGISTRY_ADDRESS: Address =
  '0x51f338c1c1721d74b5feFAfbA5f067f7F850226A'   // LiquidationRightsRegistryV2

export const REGISTRY_ADDRESS_V1: Address =
  '0x8014d6bef9f17168E7b0Ea3CeacC57609e51ceEf'   // v1 — reference only

export const REGISTRY_ABI = [
  {
    name: 'register',
    type: 'function',
    stateMutability: 'payable',
    inputs: [{ name: 'borrower', type: 'address' }],
    outputs: [],
  },
  {
    name: 'recordExecution',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [{ name: 'borrower', type: 'address' }],
    outputs: [],
  },
  {
    name: 'slash',
    type: 'function',
    stateMutability: 'nonpayable',
    inputs: [{ name: 'borrower', type: 'address' }],
    outputs: [],
  },
  {
    name: 'hasActiveRights',
    type: 'function',
    stateMutability: 'view',
    inputs: [
      { name: 'borrower', type: 'address' },
      { name: 'liquidator', type: 'address' },
    ],
    outputs: [{ name: '', type: 'bool' }],
  },
  {
    name: 'getRights',
    type: 'function',
    stateMutability: 'view',
    inputs: [{ name: 'borrower', type: 'address' }],
    outputs: [
      { name: 'liquidator', type: 'address' },
      { name: 'stake',      type: 'uint256' },
      { name: 'expiresAt',  type: 'uint256' },
      { name: 'executed',   type: 'bool' },
      { name: 'active',     type: 'bool' },
    ],
  },
  {
    name: 'WINDOW',
    type: 'function',
    stateMutability: 'view',
    inputs: [],
    outputs: [{ name: '', type: 'uint256' }],
  },
  {
    name: 'MIN_STAKE',
    type: 'function',
    stateMutability: 'view',
    inputs: [],
    outputs: [{ name: '', type: 'uint256' }],
  },
  // Events
  {
    name: 'Registered',
    type: 'event',
    inputs: [
      { name: 'borrower',   type: 'address', indexed: true },
      { name: 'liquidator', type: 'address', indexed: true },
      { name: 'stake',      type: 'uint256', indexed: false },
      { name: 'expiresAt',  type: 'uint256', indexed: false },
    ],
  },
  {
    name: 'Executed',
    type: 'event',
    inputs: [
      { name: 'borrower',      type: 'address', indexed: true },
      { name: 'liquidator',    type: 'address', indexed: true },
      { name: 'stakeReturned', type: 'uint256', indexed: false },
    ],
  },
  {
    name: 'Slashed',
    type: 'event',
    inputs: [
      { name: 'borrower',          type: 'address', indexed: true },
      { name: 'slashedLiquidator', type: 'address', indexed: true },
      { name: 'slasher',           type: 'address', indexed: true },
      { name: 'bounty',            type: 'uint256', indexed: false },
      { name: 'treasuryShare',     type: 'uint256', indexed: false },
    ],
  },
] as const

// ── Types ─────────────────────────────────────────────────────────────────────

export interface RightsState {
  liquidator: Address
  stake: bigint
  expiresAt: bigint
  executed: boolean
  active: boolean
}

export interface RegistryClientConfig {
  rpcUrl?:    string              // defaults to https://mainnet.base.org
  privateKey: `0x${string}`      // your wallet's private key
  address?:   Address            // override registry address (for testing)
}

// ── Client ────────────────────────────────────────────────────────────────────

export class LiquidationRightsClient {
  private readonly pub: PublicClient
  private readonly wallet: WalletClient
  private readonly owner: Address
  private readonly contract: Address

  readonly MIN_STAKE = parseEther('0.005')
  readonly WINDOW_SEC = 600n   // 10 minutes

  constructor(config: RegistryClientConfig) {
    const account  = privateKeyToAccount(config.privateKey)
    this.owner     = account.address
    this.contract  = config.address ?? REGISTRY_ADDRESS

    this.pub = createPublicClient({
      chain:     base,
      transport: http(config.rpcUrl ?? 'https://mainnet.base.org'),
    })

    this.wallet = createWalletClient({
      account,
      chain:     base,
      transport: http(config.rpcUrl ?? 'https://mainnet.base.org'),
    })
  }

  // ── Read ───────────────────────────────────────────────────────────────────

  /** Full rights state for a borrower. */
  async getRights(borrower: Address): Promise<RightsState> {
    const result = await this.pub.readContract({
      address:      this.contract,
      abi:          REGISTRY_ABI,
      functionName: 'getRights',
      args:         [borrower],
    })
    const [liquidator, stake, expiresAt, executed, active] = result as [
      Address, bigint, bigint, boolean, boolean
    ]
    return { liquidator, stake, expiresAt, executed, active }
  }

  /**
   * Returns true if another liquidator (not us) holds active rights.
   * Back off if this returns true — competing is strictly worse than
   * waiting for their window to expire or attempting an outbid.
   */
  async otherHoldsRights(borrower: Address): Promise<boolean> {
    try {
      const r = await this.getRights(borrower)
      return r.active && r.liquidator.toLowerCase() !== this.owner.toLowerCase()
    } catch {
      return false   // fail open — never block a liquidation on RPC error
    }
  }

  /** Returns true if we hold active rights on this borrower. */
  async weHoldRights(borrower: Address): Promise<boolean> {
    try {
      return await this.pub.readContract({
        address:      this.contract,
        abi:          REGISTRY_ABI,
        functionName: 'hasActiveRights',
        args:         [borrower, this.owner],
      }) as boolean
    } catch {
      return false
    }
  }

  // ── Write ──────────────────────────────────────────────────────────────────

  /**
   * Stake MIN_STAKE ETH to claim 10-minute priority rights on `borrower`.
   * If another liquidator holds active rights, you must stake 2x their amount
   * to outbid them (their stake is refunded automatically).
   *
   * Returns the transaction hash.
   */
  async register(borrower: Address, stakeWei?: bigint): Promise<Hash> {
    const value = stakeWei ?? this.MIN_STAKE
    return this.wallet.writeContract({
      address:      this.contract,
      abi:          REGISTRY_ABI,
      functionName: 'register',
      args:         [borrower],
      value,
    })
  }

  /**
   * Call after successfully liquidating `borrower` to reclaim your stake.
   * Must be called by the rights holder. Window may have expired — late
   * confirmation still works as long as no slash has been triggered.
   *
   * Returns the transaction hash.
   */
  async recordExecution(borrower: Address): Promise<Hash> {
    return this.wallet.writeContract({
      address:      this.contract,
      abi:          REGISTRY_ABI,
      functionName: 'recordExecution',
      args:         [borrower],
    })
  }

  /**
   * Slash a rights holder who let their window expire without executing.
   * You receive 50% of their stake as a bounty; 50% goes to the treasury.
   *
   * Will revert if:
   *   - The window has not yet expired
   *   - The rights holder already called recordExecution
   *   - No rights exist for this borrower
   *   - You are the rights holder (no self-slash)
   *
   * Returns the transaction hash.
   */
  async slash(borrower: Address): Promise<Hash> {
    return this.wallet.writeContract({
      address:      this.contract,
      abi:          REGISTRY_ABI,
      functionName: 'slash',
      args:         [borrower],
    })
  }

  // ── Helpers ────────────────────────────────────────────────────────────────

  /** True if a borrower's registration window has expired without execution. */
  async isSlashable(borrower: Address): Promise<boolean> {
    try {
      const r   = await this.getRights(borrower)
      const now = BigInt(Math.floor(Date.now() / 1000))
      return (
        r.liquidator !== '0x0000000000000000000000000000000000000000' &&
        !r.executed &&
        now >= r.expiresAt &&
        r.liquidator.toLowerCase() !== this.owner.toLowerCase()
      )
    } catch {
      return false
    }
  }

  get ownerAddress(): Address {
    return this.owner
  }
}

// ── Factory ───────────────────────────────────────────────────────────────────

export function createRegistryClient(config: RegistryClientConfig): LiquidationRightsClient {
  return new LiquidationRightsClient(config)
}
