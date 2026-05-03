// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import {ReentrancyGuard} from "@openzeppelin/contracts/security/ReentrancyGuard.sol";

/**
 * @title  LiquidationRightsRegistry
 * @notice Coordination layer for on-chain liquidators.
 *
 * Liquidators register priority rights on a specific Aave borrower by staking
 * ETH. During the window, other rational liquidators back off (coordinated by
 * economic incentive, not by restricting Aave's permissionless liquidationCall).
 *
 * Flow:
 *   1. Liquidator calls register(borrower) with msg.value >= MIN_STAKE.
 *   2. Liquidator executes the Aave liquidation within WINDOW seconds.
 *   3. Liquidator calls recordExecution(borrower) to reclaim stake.
 *   4. If the window expires without recordExecution, anyone calls
 *      slash(borrower): caller receives SLASH_BOUNTY_BPS of the stake,
 *      remainder goes to treasury.
 *
 * Outbidding: to unseat an active rights holder, new registrant must stake
 * >= 2x the current holder's stake. Previous stake is refunded immediately.
 *
 * v2 roadmap: treasury becomes a yield-bearing LP vault. Stakers earn passive
 * income from slash revenues without running liquidation infrastructure.
 */
contract LiquidationRightsRegistry is ReentrancyGuard {

    // ── Errors ─────────────────────────────────────────────────────────────────

    error StakeTooLow();
    error OutbidRequires2x();
    error NotRightsHolder();
    error NoActiveRights();
    error WindowNotExpired();
    error AlreadyExecuted();
    error SelfSlash();
    error ETHTransferFailed();

    // ── Constants ──────────────────────────────────────────────────────────────

    uint256 public constant WINDOW           = 10 minutes;
    uint256 public constant MIN_STAKE        = 0.005 ether;
    uint256 public constant SLASH_BOUNTY_BPS = 5_000;   // 50% to slasher, 50% to treasury

    // ── State ──────────────────────────────────────────────────────────────────

    address public immutable treasury;

    struct Rights {
        address liquidator;
        uint256 stake;
        uint256 expiresAt;
        bool    executed;
    }

    mapping(address => Rights) public rights;   // borrower → Rights

    // ── Events ─────────────────────────────────────────────────────────────────

    event Registered(
        address indexed borrower,
        address indexed liquidator,
        uint256 stake,
        uint256 expiresAt
    );

    event Outbid(
        address indexed borrower,
        address indexed previous,
        address indexed replacement,
        uint256 refundedStake
    );

    event Executed(
        address indexed borrower,
        address indexed liquidator,
        uint256 stakeReturned
    );

    event Slashed(
        address indexed borrower,
        address indexed slashedLiquidator,
        address indexed slasher,
        uint256 bounty,
        uint256 treasuryShare
    );

    // ── Constructor ────────────────────────────────────────────────────────────

    constructor(address _treasury) {
        treasury = _treasury;
    }

    // ── Register ───────────────────────────────────────────────────────────────

    /**
     * @notice Stake ETH to claim priority rights on `borrower`.
     *         Current rights must be expired/executed, or caller must stake
     *         >= 2x the current holder's stake to outbid them.
     */
    function register(address borrower) external payable nonReentrant {
        if (msg.value < MIN_STAKE) revert StakeTooLow();

        Rights storage cur = rights[borrower];
        bool isActive = cur.liquidator != address(0)
                     && block.timestamp < cur.expiresAt
                     && !cur.executed;

        if (isActive) {
            if (msg.value < cur.stake * 2) revert OutbidRequires2x();

            // Refund the outbid holder before overwriting state.
            address prev   = cur.liquidator;
            uint256 refund = cur.stake;
            emit Outbid(borrower, prev, msg.sender, refund);
            _sendETH(prev, refund);
        }

        cur.liquidator = msg.sender;
        cur.stake      = msg.value;
        cur.expiresAt  = block.timestamp + WINDOW;
        cur.executed   = false;

        emit Registered(borrower, msg.sender, msg.value, cur.expiresAt);
    }

    // ── Record execution ───────────────────────────────────────────────────────

    /**
     * @notice Call after successfully liquidating `borrower` to reclaim stake.
     *         Must be called by the rights holder; window may be expired (late
     *         confirmation is fine as long as no slash has been triggered).
     */
    function recordExecution(address borrower) external nonReentrant {
        Rights storage r = rights[borrower];
        if (r.liquidator != msg.sender) revert NotRightsHolder();
        if (r.executed)                 revert AlreadyExecuted();

        r.executed = true;
        uint256 stake = r.stake;
        r.stake = 0;

        emit Executed(borrower, msg.sender, stake);
        _sendETH(msg.sender, stake);
    }

    // ── Slash ──────────────────────────────────────────────────────────────────

    /**
     * @notice Slash a rights holder who let their window expire without executing.
     *         Caller receives SLASH_BOUNTY_BPS of the stake; rest goes to treasury.
     */
    function slash(address borrower) external nonReentrant {
        Rights storage r = rights[borrower];
        if (r.liquidator == address(0))       revert NoActiveRights();
        if (r.executed)                        revert AlreadyExecuted();
        if (block.timestamp < r.expiresAt)    revert WindowNotExpired();
        if (r.liquidator == msg.sender)        revert SelfSlash();

        address slashedLiquidator = r.liquidator;
        uint256 stake             = r.stake;
        delete rights[borrower];

        uint256 bounty        = stake * SLASH_BOUNTY_BPS / 10_000;
        uint256 treasuryShare = stake - bounty;

        emit Slashed(borrower, slashedLiquidator, msg.sender, bounty, treasuryShare);
        _sendETH(msg.sender,  bounty);
        _sendETH(treasury,    treasuryShare);
    }

    // ── Views ──────────────────────────────────────────────────────────────────

    /**
     * @notice Returns true if `liquidator` holds active rights on `borrower`.
     */
    function hasActiveRights(address borrower, address liquidator) external view returns (bool) {
        Rights storage r = rights[borrower];
        return r.liquidator == liquidator
            && block.timestamp < r.expiresAt
            && !r.executed;
    }

    /**
     * @notice Full rights state for a borrower.
     */
    function getRights(address borrower) external view returns (
        address liquidator,
        uint256 stake,
        uint256 expiresAt,
        bool    executed,
        bool    active
    ) {
        Rights storage r = rights[borrower];
        return (
            r.liquidator,
            r.stake,
            r.expiresAt,
            r.executed,
            r.liquidator != address(0) && block.timestamp < r.expiresAt && !r.executed
        );
    }

    // ── Internal ───────────────────────────────────────────────────────────────

    function _sendETH(address to, uint256 amount) internal {
        (bool ok,) = payable(to).call{value: amount}("");
        if (!ok) revert ETHTransferFailed();
    }

    receive() external payable { revert ETHTransferFailed(); }
}
