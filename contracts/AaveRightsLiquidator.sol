// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import {ReentrancyGuard} from "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import {IERC20}          from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20}       from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

// ─── Interfaces ───────────────────────────────────────────────────────────────

interface IAavePool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes   calldata params,
        uint16  referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool    receiveAToken
    ) external;
}

interface IFlashLoanSimpleReceiver {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes   calldata params
    ) external returns (bool);
}

interface ISlipstreamRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        int24   tickSpacing;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata p) external returns (uint256);
}

interface IUniswapV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata p) external returns (uint256);
}

/**
 * @title  AaveRightsLiquidator
 * @notice Flash-liquidates under-water Aave v3 positions on Base.
 *
 * Flow:
 *   1. Owner calls executeLiquidation(collateral, debt, user, debtToCover, swapData).
 *   2. Contract flash-borrows debtToCover of debtAsset from Aave (0.05% fee).
 *   3. In executeOperation callback:
 *        a. Calls pool.liquidationCall(collateral, debt, user, debtToCover, false)
 *           — seizes discounted collateral into this contract.
 *        b. Swaps seized collateral → debtAsset via Aerodrome Slipstream or Uniswap V3.
 *        c. Approves Aave pool to pull back (amount + premium).
 *   4. Profit = collateral_sale_proceeds - (debtToCover + premium) stays in contract.
 *      Owner sweeps via sweep().
 *
 * swapData encoding:
 *   abi.encode(routerType, tickSpacing, amountOutMinimum)
 *   routerType  uint8  — 0=Aerodrome Slipstream, 1=Uniswap V3 / Aerodrome V2
 *   tickSpacing int24  — tick spacing for Slipstream; interpreted as fee tier if routerType=1
 *   amountOutMinimum uint256 — slippage floor in debt token units
 */
contract AaveRightsLiquidator is IFlashLoanSimpleReceiver, ReentrancyGuard {

    using SafeERC20 for IERC20;

    // ── Errors ─────────────────────────────────────────────────────────────────
    error Unauthorized();
    error CallbackUnauthorized();
    error InitiatorMismatch();
    error ZeroDebt();

    // ── Constants ──────────────────────────────────────────────────────────────
    // Aave v3 Pool on Base
    address public constant AAVE_POOL = 0xA238Dd80C259a72e81d7e4664a9801593F98d1c5;

    // Aerodrome Slipstream router on Base
    address public constant SLIPSTREAM_ROUTER = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;

    // ── State ──────────────────────────────────────────────────────────────────
    address public immutable owner;

    // ── Constructor ────────────────────────────────────────────────────────────

    constructor(address _owner) {
        owner = _owner;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    // ── Entry point ───────────────────────────────────────────────────────────

    /**
     * @notice Liquidate an Aave v3 position.
     * @param collateralAsset  Collateral token to seize.
     * @param debtAsset        Debt token to repay.
     * @param user             Address of the under-water borrower.
     * @param debtToCover      Amount of debtAsset to repay (flash-borrowed).
     * @param swapData         abi.encode(routerType, tickSpacing, amountOutMinimum)
     */
    function executeLiquidation(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bytes   calldata swapData
    ) external onlyOwner nonReentrant {
        if (debtToCover == 0) revert ZeroDebt();

        // Encode callback params: (collateralAsset, user, swapData)
        bytes memory params = abi.encode(collateralAsset, user, swapData);

        // Flash borrow debtToCover of debtAsset from Aave (0.05% premium)
        IAavePool(AAVE_POOL).flashLoanSimple(
            address(this),
            debtAsset,
            debtToCover,
            params,
            0   // referralCode
        );
    }

    // ── Flash callback ────────────────────────────────────────────────────────

    /**
     * @dev Called by Aave after transferring `amount` of `asset` to this contract.
     *      Must approve Aave to pull back (amount + premium) before returning true.
     */
    function executeOperation(
        address asset,       // debtAsset (what we borrowed)
        uint256 amount,      // debtToCover
        uint256 premium,     // Aave flash fee (0.05%)
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender   != AAVE_POOL)       revert CallbackUnauthorized();
        if (initiator    != address(this))   revert InitiatorMismatch();

        (address collateralAsset, address user, bytes memory swapData) =
            abi.decode(params, (address, address, bytes));

        // Step 1: Approve pool to pull debt tokens for liquidation
        IERC20(asset).forceApprove(AAVE_POOL, amount);

        // Step 2: Liquidate — repay debt, seize collateral (receiveAToken=false)
        IAavePool(AAVE_POOL).liquidationCall(
            collateralAsset,
            asset,        // debtAsset
            user,
            amount,       // debtToCover (all of what we borrowed)
            false         // receive underlying collateral, not aToken
        );

        // Step 3: Swap seized collateral → debtAsset to repay flash loan
        uint256 collateralBalance = IERC20(collateralAsset).balanceOf(address(this));
        if (collateralBalance > 0) {
            _swapCollateral(collateralAsset, asset, collateralBalance, swapData);
        }

        // Step 4: Approve Aave to pull back the flash loan + premium
        uint256 totalOwed = amount + premium;
        IERC20(asset).forceApprove(AAVE_POOL, totalOwed);

        return true;
    }

    // ── Internal: swap collateral → debt asset ────────────────────────────────

    function _swapCollateral(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        bytes memory swapData
    ) internal {
        (uint8 routerType, int24 tickSpacing, uint256 amountOutMin) =
            abi.decode(swapData, (uint8, int24, uint256));

        IERC20(tokenIn).forceApprove(SLIPSTREAM_ROUTER, amountIn);

        if (routerType == 0) {
            // Aerodrome Slipstream
            ISlipstreamRouter(SLIPSTREAM_ROUTER).exactInputSingle(
                ISlipstreamRouter.ExactInputSingleParams({
                    tokenIn:           tokenIn,
                    tokenOut:          tokenOut,
                    tickSpacing:       tickSpacing,
                    recipient:         address(this),
                    deadline:          block.timestamp,
                    amountIn:          amountIn,
                    amountOutMinimum:  amountOutMin,
                    sqrtPriceLimitX96: 0
                })
            );
        } else {
            // Uniswap V3 / Aerodrome V2 — tickSpacing cast to fee tier
            uint24 fee = uint24(uint256(int256(tickSpacing)));
            IUniswapV3Router(SLIPSTREAM_ROUTER).exactInputSingle(
                IUniswapV3Router.ExactInputSingleParams({
                    tokenIn:           tokenIn,
                    tokenOut:          tokenOut,
                    fee:               fee,
                    recipient:         address(this),
                    deadline:          block.timestamp,
                    amountIn:          amountIn,
                    amountOutMinimum:  amountOutMin,
                    sqrtPriceLimitX96: 0
                })
            );
        }
    }

    // ── Admin ─────────────────────────────────────────────────────────────────

    function sweep(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        if (bal > 0) IERC20(token).safeTransfer(owner, bal);
    }

    function sweepEth() external onlyOwner {
        uint256 bal = address(this).balance;
        if (bal > 0) payable(owner).transfer(bal);
    }

    receive() external payable {}
}
