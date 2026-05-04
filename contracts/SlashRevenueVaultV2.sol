// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import {ERC4626}   from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {ERC20}     from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20}    from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Ownable}   from "@openzeppelin/contracts/access/Ownable.sol";

interface IWETH9 {
    function deposit() external payable;
}

/**
 * @title  SlashRevenueVaultV2
 * @notice ERC-4626 yield vault with automatic ETH → WETH revenue routing.
 *
 * V2 changes from V1:
 *   - receive() now auto-wraps incoming ETH to WETH and credits it as yield.
 *     When LiquidationRightsRegistryV2 sends the treasury share from a slash,
 *     it hits receive() and immediately increases the share price for all holders.
 *     No manual addRevenue() call needed.
 *   - addRevenue() is now permissionless (anyone can donate yield to holders).
 *     The onlyOwner restriction is removed because receive() is already open
 *     and the attack surface is identical.
 *
 * Invariants:
 *   - Only ETH/WETH can increase totalAssets (no arbitrary ERC20 injection).
 *   - Depositors cannot lose principal via receive() — it can only increase value.
 *   - Inflation attack protection: OZ v4.9 virtual shares (_decimalsOffset = 6).
 *
 * Underlying: WETH (Base 0x4200000000000000000000000000000000000006)
 * Share:      srvETH
 */
contract SlashRevenueVaultV2 is ERC4626, Ownable {

    using SafeERC20 for IERC20;

    // ── Errors ─────────────────────────────────────────────────────────────────
    error ZeroValue();

    // ── Events ─────────────────────────────────────────────────────────────────
    event RevenueAdded(uint256 amount, address indexed sender, uint256 newTotalAssets);

    IWETH9 public immutable weth;

    // ── Constructor ────────────────────────────────────────────────────────────

    constructor(address _weth, address _owner)
        ERC4626(IERC20(_weth))
        ERC20("Slash Revenue ETH", "srvETH")
    {
        weth = IWETH9(_weth);
        _transferOwnership(_owner);
    }

    // ── Revenue injection (explicit) ───────────────────────────────────────────

    /**
     * @notice Explicitly inject ETH as yield. Permissionless — anyone can
     *         donate yield to all current srvETH holders.
     *         Wraps ETH → WETH, increases totalAssets, raises share price.
     */
    function addRevenue() external payable {
        if (msg.value == 0) revert ZeroValue();
        weth.deposit{value: msg.value}();
        emit RevenueAdded(msg.value, msg.sender, totalAssets());
    }

    // ── Revenue injection (automatic) ─────────────────────────────────────────

    /**
     * @notice Auto-routes any ETH received directly into the yield pool.
     *         Called by LiquidationRightsRegistryV2._sendETH(treasury, share)
     *         on every slash — no manual step required.
     *
     *         Security: adding ETH to the pool can only increase share price.
     *         There is no way for a caller to extract value via this path.
     */
    receive() external payable {
        if (msg.value > 0) {
            weth.deposit{value: msg.value}();
            emit RevenueAdded(msg.value, msg.sender, totalAssets());
        }
    }

    // ── ERC4626 override ───────────────────────────────────────────────────────

    function _decimalsOffset() internal pure override returns (uint8) {
        return 6;
    }
}
