// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import {ERC4626}    from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {ERC20}      from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}     from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20}  from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable}    from "@openzeppelin/contracts/access/Ownable.sol";

interface IWETH {
    function deposit() external payable;
}

/**
 * @title  SlashRevenueVault
 * @notice ERC-4626 yield vault powered by LiquidationRightsRegistry slash revenue.
 *
 * Underlying asset: WETH (Base mainnet 0x4200000000000000000000000000000000000006).
 * Share token:      srvETH — accrues value as slash revenue is added.
 *
 * Flow:
 *   1. Anyone deposits WETH, receives srvETH shares at current price.
 *   2. Owner calls addRevenue() (payable — ETH is auto-wrapped to WETH)
 *      whenever slash proceeds accumulate in the treasury wallet.
 *   3. totalAssets() increases → share price increases → all holders earn yield.
 *   4. Depositors redeem srvETH → receive more WETH than they deposited.
 *
 * No lock periods. No deposit fees. No withdrawal fees. Revenue is the only
 * yield source — if no registrations are slashed, the share price stays flat.
 *
 * Inflation attack protection: OZ v4.9 virtual shares offset (_decimalsOffset = 6).
 * Owner should seed the vault with a small initial deposit after deployment.
 */
contract SlashRevenueVault is ERC4626, Ownable {

    using SafeERC20 for IERC20;

    // ── Errors ─────────────────────────────────────────────────────────────────
    error ZeroValue();

    // ── Events ─────────────────────────────────────────────────────────────────
    event RevenueAdded(uint256 amount, address indexed sender, uint256 newTotalAssets);

    IWETH public immutable weth;

    // ── Constructor ────────────────────────────────────────────────────────────

    /**
     * @param _weth    WETH address (Base: 0x4200000000000000000000000000000000000006)
     * @param _owner   Initial owner — receives Ownable access; should be deployer
     */
    constructor(address _weth, address _owner)
        ERC4626(IERC20(_weth))
        ERC20("Slash Revenue ETH", "srvETH")
    {
        weth  = IWETH(_weth);
        _transferOwnership(_owner);
    }

    // ── Revenue injection ──────────────────────────────────────────────────────

    /**
     * @notice Inject slash proceeds into the vault as yield.
     *
     * Two call patterns are supported:
     *   A) Send raw ETH: addRevenue{value: amount}()
     *      → ETH is wrapped to WETH and added to the pool.
     *   B) Pre-wrap and call with value = 0, then transfer WETH separately.
     *      (Pattern A is easier from Python/TypeScript.)
     *
     * Adding assets without minting new shares increases the price per share,
     * distributing yield to all current holders proportionally.
     *
     * Only callable by owner to prevent arbitrary WETH deposits from polluting
     * the revenue accounting (deposits go through standard ERC4626 deposit()).
     */
    function addRevenue() external payable onlyOwner {
        if (msg.value == 0) revert ZeroValue();

        // Wrap ETH → WETH. The WETH lands in this contract's balance,
        // which is exactly what totalAssets() reads. No state update needed.
        weth.deposit{value: msg.value}();

        emit RevenueAdded(msg.value, msg.sender, totalAssets());
    }

    // ── Accept raw ETH ─────────────────────────────────────────────────────────

    /**
     * @dev Accept ETH sent directly (e.g. from the registry treasury wallet).
     *      Does NOT auto-add to the yield pool — owner must call addRevenue()
     *      explicitly. This prevents accidental share price manipulation.
     */
    receive() external payable {}

    // ── ERC4626 overrides ──────────────────────────────────────────────────────

    /**
     * @dev Offset gives 10^6 virtual shares, protecting against inflation attacks.
     *     See OZ ERC4626 docs.
     */
    function _decimalsOffset() internal pure override returns (uint8) {
        return 6;
    }
}
