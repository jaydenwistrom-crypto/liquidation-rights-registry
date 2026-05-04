// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import {ReentrancyGuard} from "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import {IERC20}          from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20}       from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

// ─── Interfaces ───────────────────────────────────────────────────────────────

interface IMorpho {
    struct MarketParams {
        address loanToken;
        address collateralToken;
        address oracle;
        address irm;
        uint256 lltv;
    }

    function flashLoan(address token, uint256 assets, bytes calldata data) external;

    function liquidate(
        MarketParams calldata marketParams,
        address borrower,
        uint256 seizedAssets,
        uint256 repaidShares,
        bytes   calldata data
    ) external returns (uint256, uint256);
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
 * @title  MorphoBlueRightsLiquidator
 * @notice Flash-liquidates under-water Morpho Blue positions on Base.
 *
 * Flow:
 *   1. Owner calls executeLiquidation(marketParams, borrower, repaidShares, swapData).
 *   2. Contract flash-borrows the required loan token from Morpho Blue (0% fee).
 *   3. In the flash callback: liquidates the position, receives seized collateral.
 *   4. Sells collateral → loan token via Aerodrome Slipstream (or Uniswap V3).
 *      Route specified off-chain in swapData — no hardcoded routes needed.
 *   5. Repays flash loan (Morpho pulls tokens via safeTransferFrom).
 *   6. Profit stays in contract; owner sweeps via sweep().
 *
 * Designed for the Liquidation Rights Protocol:
 *   After a successful liquidation the Python executor calls recordExecution(borrower)
 *   on LiquidationRightsRegistryV2 to reclaim the registration stake.
 *
 * Supported swap routers:
 *   routerType = 0 → Aerodrome Slipstream (int24 tickSpacing)
 *   routerType = 1 → Uniswap V3 / Aerodrome V2 (uint24 fee tier)
 */
contract MorphoBlueRightsLiquidator is ReentrancyGuard {

    using SafeERC20 for IERC20;

    // ── Errors ─────────────────────────────────────────────────────────────────
    error Unauthorized();
    error FlashCallbackUnauthorized();
    error InsufficientProfit(uint256 received, uint256 required);
    error SwapFailed();
    error ZeroShares();

    // ── Constants ──────────────────────────────────────────────────────────────
    address public constant MORPHO = 0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb;

    // Aerodrome Slipstream router on Base
    address public constant SLIPSTREAM_ROUTER = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;

    // ── State ──────────────────────────────────────────────────────────────────
    address public immutable owner;

    // Transient context for the flash callback (set before flash, cleared after)
    IMorpho.MarketParams private _cbMarketParams;
    address              private _cbBorrower;
    uint256              private _cbRepaidShares;
    bytes                private _cbSwapData;

    // ── SwapData encoding ─────────────────────────────────────────────────────
    //
    // swapData = abi.encode(routerType, tickSpacing, amountOutMinimum)
    //   routerType      uint8   — 0=Slipstream, 1=UniV3/AeroV2
    //   tickSpacing     int24   — for Slipstream; interpreted as uint24 fee if routerType=1
    //   amountOutMinimum uint256 — slippage floor in loan token units (1e-6 USDC basis)

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
     * @notice Liquidate a Morpho Blue position.
     * @param marketParams   Full market params for the target market.
     * @param borrower       Address of the under-water borrower.
     * @param repaidShares   Debt shares to repay (type(uint256).max = all).
     * @param swapData       abi.encode(routerType, tickSpacing, amountOutMinimum)
     */
    function executeLiquidation(
        IMorpho.MarketParams calldata marketParams,
        address   borrower,
        uint256   repaidShares,
        bytes     calldata swapData
    ) external onlyOwner nonReentrant {
        if (repaidShares == 0) revert ZeroShares();

        // Store context for the flash callback
        _cbMarketParams  = marketParams;
        _cbBorrower      = borrower;
        _cbRepaidShares  = repaidShares;
        _cbSwapData      = swapData;

        // Estimate flash amount needed = repaidShares * (totalBorrowAssets / totalBorrowShares)
        // We over-estimate by 1 wei to handle rounding; exactness isn't required since
        // any surplus loan token stays in the contract as profit.
        //
        // The Python executor pre-computes the required amount and passes it via
        // repaidShares. If shares = type(uint256).max the contract liquidates everything.
        // We flash-borrow a round estimate; Morpho only pulls what we owe.
        uint256 flashAmount = _estimateFlashAmount(marketParams, repaidShares);

        IMorpho(MORPHO).flashLoan(marketParams.loanToken, flashAmount, "");
    }

    // ── Morpho flash callback ─────────────────────────────────────────────────

    /**
     * @dev Called by Morpho after sending flashAmount of loanToken to this contract.
     *      We liquidate the position, swap collateral, approve repayment.
     */
    function onMorphoFlashLoan(uint256 assets, bytes calldata) external {
        if (msg.sender != MORPHO) revert FlashCallbackUnauthorized();

        IMorpho.MarketParams memory mp = _cbMarketParams;
        address borrower   = _cbBorrower;
        uint256 repaidShares = _cbRepaidShares;
        bytes memory swapData = _cbSwapData;

        // Step 1: Approve Morpho to pull loan tokens (used as repayment)
        IERC20(mp.loanToken).forceApprove(MORPHO, assets);

        // Step 2: Liquidate — repay borrower's debt, receive seized collateral
        IMorpho(MORPHO).liquidate(mp, borrower, 0, repaidShares, "");

        // Step 3: Sell seized collateral → loan token
        uint256 collateralBalance = IERC20(mp.collateralToken).balanceOf(address(this));
        if (collateralBalance > 0) {
            _swapCollateral(mp.collateralToken, mp.loanToken, collateralBalance, swapData);
        }

        // Step 4: Morpho pulls back `assets` via safeTransferFrom (we already approved).
        // Any loan token balance above `assets` stays as profit.
    }

    // ── Internal: estimate flash amount ──────────────────────────────────────

    function _estimateFlashAmount(
        IMorpho.MarketParams memory mp,
        uint256 repaidShares
    ) internal view returns (uint256) {
        // Read live market state to compute repaidAssets from repaidShares
        (bool ok, bytes memory data) = MORPHO.staticcall(
            abi.encodeWithSignature("market(bytes32)", _marketId(mp))
        );
        if (!ok || data.length == 0) {
            // Fallback: assume 1:1 ratio + 5% buffer (handles edge cases gracefully)
            return repaidShares + repaidShares / 20;
        }
        // market() returns: (totalSupplyAssets, totalSupplyShares,
        //                    totalBorrowAssets, totalBorrowShares, lastUpdate, fee)
        (,, uint128 totalBorrowAssets, uint128 totalBorrowShares,,) =
            abi.decode(data, (uint128, uint128, uint128, uint128, uint128, uint128));

        if (totalBorrowShares == 0) return repaidShares;

        // repaidAssets = repaidShares * totalBorrowAssets / totalBorrowShares (rounded up)
        uint256 repaidAssets = (uint256(repaidShares) * uint256(totalBorrowAssets) + uint256(totalBorrowShares) - 1)
            / uint256(totalBorrowShares);

        // Add 0.1% buffer for interest accrual between estimation and execution
        return repaidAssets + repaidAssets / 1000;
    }

    // ── Internal: swap collateral → loan token ────────────────────────────────

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
                    tokenIn:          tokenIn,
                    tokenOut:         tokenOut,
                    tickSpacing:      tickSpacing,
                    recipient:        address(this),
                    deadline:         block.timestamp,
                    amountIn:         amountIn,
                    amountOutMinimum: amountOutMin,
                    sqrtPriceLimitX96: 0
                })
            );
        } else {
            // Uniswap V3 / Aerodrome V2  — tickSpacing cast to uint24 fee tier
            uint24 fee = uint24(uint256(int256(tickSpacing)));
            IERC20(tokenIn).forceApprove(SLIPSTREAM_ROUTER, amountIn);
            IUniswapV3Router(SLIPSTREAM_ROUTER).exactInputSingle(
                IUniswapV3Router.ExactInputSingleParams({
                    tokenIn:          tokenIn,
                    tokenOut:         tokenOut,
                    fee:              fee,
                    recipient:        address(this),
                    deadline:         block.timestamp,
                    amountIn:         amountIn,
                    amountOutMinimum: amountOutMin,
                    sqrtPriceLimitX96: 0
                })
            );
        }
    }

    // ── Internal: compute Morpho market ID ───────────────────────────────────

    function _marketId(IMorpho.MarketParams memory mp) internal pure returns (bytes32) {
        return keccak256(abi.encode(mp));
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
