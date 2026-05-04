// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import {ReentrancyGuard} from "@openzeppelin/contracts/security/ReentrancyGuard.sol";
import {Ownable}         from "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title  LiquidationRightsRegistryV2
 * @notice Coordination layer for on-chain liquidators — v2.
 *
 * V2 changes from V1:
 *   - treasury is mutable (owner can update via setTreasury).
 *     Set treasury = SlashRevenueVaultV2 address so every slash automatically
 *     routes 50% of forfeited stake to srvETH holders — zero manual steps.
 *   - window and minStake are owner-adjustable with safety bounds.
 *   - SLASH_BOUNTY_BPS remains a constant — this split is a protocol invariant
 *     that participants rely on and must not change without redeployment.
 *   - version() returns "2.0.0" for integrator identification.
 *   - Admin events emitted for every parameter change (on-chain transparency).
 *
 * Core mechanics are unchanged from V1: register, recordExecution, slash,
 * outbidding with 2x stake, hasActiveRights, getRights.
 */
contract LiquidationRightsRegistryV2 is ReentrancyGuard, Ownable {

    // ── Errors ─────────────────────────────────────────────────────────────────

    error StakeTooLow();
    error OutbidRequires2x();
    error NotRightsHolder();
    error NoActiveRights();
    error WindowNotExpired();
    error AlreadyExecuted();
    error SelfSlash();
    error ETHTransferFailed();
    error ZeroAddress();
    error WindowOutOfBounds();
    error StakeOutOfBounds();

    // ── Constants ──────────────────────────────────────────────────────────────

    uint256 public constant SLASH_BOUNTY_BPS = 5_000;   // 50% — protocol invariant

    uint256 public constant MIN_WINDOW =  5 minutes;
    uint256 public constant MAX_WINDOW = 60 minutes;
    uint256 public constant MIN_STAKE_FLOOR = 0.001 ether;
    uint256 public constant MAX_STAKE_CAP   = 1 ether;

    // ── Mutable parameters (owner-controlled) ──────────────────────────────────

    address public treasury;
    uint256 public window;       // active exclusivity window in seconds
    uint256 public minStake;     // minimum registration stake in wei

    // ── State ──────────────────────────────────────────────────────────────────

    struct Rights {
        address liquidator;
        uint256 stake;
        uint256 expiresAt;
        bool    executed;
    }

    mapping(address => Rights) public rights;

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

    // Admin events — on-chain audit trail for parameter changes
    event TreasuryUpdated(address indexed oldTreasury, address indexed newTreasury);
    event WindowUpdated(uint256 oldWindow, uint256 newWindow);
    event MinStakeUpdated(uint256 oldMinStake, uint256 newMinStake);

    // ── Constructor ────────────────────────────────────────────────────────────

    /**
     * @param _treasury  Address that receives treasury share from slashes.
     *                   Set to SlashRevenueVaultV2 for automatic revenue routing.
     * @param _window    Exclusivity window in seconds (5 min to 60 min).
     * @param _minStake  Minimum registration stake in wei.
     * @param _owner     Contract owner — controls treasury, window, minStake.
     */
    constructor(
        address _treasury,
        uint256 _window,
        uint256 _minStake,
        address _owner
    ) {
        if (_treasury == address(0))              revert ZeroAddress();
        if (_window < MIN_WINDOW || _window > MAX_WINDOW) revert WindowOutOfBounds();
        if (_minStake < MIN_STAKE_FLOOR || _minStake > MAX_STAKE_CAP) revert StakeOutOfBounds();

        treasury = _treasury;
        window   = _window;
        minStake = _minStake;
        _transferOwnership(_owner);
    }

    // ── Admin ──────────────────────────────────────────────────────────────────

    function setTreasury(address _treasury) external onlyOwner {
        if (_treasury == address(0)) revert ZeroAddress();
        emit TreasuryUpdated(treasury, _treasury);
        treasury = _treasury;
    }

    function setWindow(uint256 _window) external onlyOwner {
        if (_window < MIN_WINDOW || _window > MAX_WINDOW) revert WindowOutOfBounds();
        emit WindowUpdated(window, _window);
        window = _window;
    }

    function setMinStake(uint256 _minStake) external onlyOwner {
        if (_minStake < MIN_STAKE_FLOOR || _minStake > MAX_STAKE_CAP) revert StakeOutOfBounds();
        emit MinStakeUpdated(minStake, _minStake);
        minStake = _minStake;
    }

    // ── Register ───────────────────────────────────────────────────────────────

    function register(address borrower) external payable nonReentrant {
        if (msg.value < minStake) revert StakeTooLow();

        Rights storage cur = rights[borrower];
        bool isActive = cur.liquidator != address(0)
                     && block.timestamp < cur.expiresAt
                     && !cur.executed;

        if (isActive) {
            if (msg.value < cur.stake * 2) revert OutbidRequires2x();
            address prev   = cur.liquidator;
            uint256 refund = cur.stake;
            emit Outbid(borrower, prev, msg.sender, refund);
            _sendETH(prev, refund);
        }

        cur.liquidator = msg.sender;
        cur.stake      = msg.value;
        cur.expiresAt  = block.timestamp + window;
        cur.executed   = false;

        emit Registered(borrower, msg.sender, msg.value, cur.expiresAt);
    }

    // ── Record execution ───────────────────────────────────────────────────────

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

    function slash(address borrower) external nonReentrant {
        Rights storage r = rights[borrower];
        if (r.liquidator == address(0))    revert NoActiveRights();
        if (r.executed)                    revert AlreadyExecuted();
        if (block.timestamp < r.expiresAt) revert WindowNotExpired();
        if (r.liquidator == msg.sender)    revert SelfSlash();

        address slashedLiquidator = r.liquidator;
        uint256 stake             = r.stake;
        delete rights[borrower];

        uint256 bounty        = stake * SLASH_BOUNTY_BPS / 10_000;
        uint256 treasuryShare = stake - bounty;

        emit Slashed(borrower, slashedLiquidator, msg.sender, bounty, treasuryShare);
        _sendETH(msg.sender, bounty);
        _sendETH(treasury,   treasuryShare);   // → SlashRevenueVaultV2.receive() → WETH → yield
    }

    // ── Views ──────────────────────────────────────────────────────────────────

    function hasActiveRights(address borrower, address liquidator) external view returns (bool) {
        Rights storage r = rights[borrower];
        return r.liquidator == liquidator
            && block.timestamp < r.expiresAt
            && !r.executed;
    }

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

    function version() external pure returns (string memory) {
        return "2.0.0";
    }

    // ── Internal ───────────────────────────────────────────────────────────────

    function _sendETH(address to, uint256 amount) internal {
        (bool ok,) = payable(to).call{value: amount}("");
        if (!ok) revert ETHTransferFailed();
    }

    receive() external payable { revert ETHTransferFailed(); }
}
