// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import "forge-std/Test.sol";
import "../contracts/LiquidationRightsRegistryV2.sol";
import "../contracts/SlashRevenueVaultV2.sol";

// Minimal WETH mock for auto-wrap tests
contract MockWETH is ERC20 {
    constructor() ERC20("Wrapped Ether", "WETH") {}
    function deposit() external payable { _mint(msg.sender, msg.value); }
    function withdraw(uint256 a) external { _burn(msg.sender, a); payable(msg.sender).transfer(a); }
    receive() external payable {}
}

contract LiquidationRightsRegistryV2Test is Test {

    LiquidationRightsRegistryV2 reg;
    SlashRevenueVaultV2          vault;
    MockWETH                     weth;

    address owner   = address(0x0A00);
    address alice   = address(0xA1CE);
    address bob     = address(0xB0B0);
    address charlie = address(0xC4A7);
    address borrower= address(0xBEEF);

    uint256 constant WIN = 10 minutes;
    uint256 constant MIN = 0.005 ether;

    function setUp() public {
        weth  = new MockWETH();
        vault = new SlashRevenueVaultV2(address(weth), owner);
        reg   = new LiquidationRightsRegistryV2(
            address(vault),   // treasury = vault
            WIN,
            MIN,
            owner
        );

        vm.deal(alice,   10 ether);
        vm.deal(bob,     10 ether);
        vm.deal(charlie, 10 ether);
        vm.deal(owner,   10 ether);
    }

    // ── constructor validation ────────────────────────────────────────────────

    function test_constructor_stores_params() public {
        assertEq(reg.treasury(), address(vault));
        assertEq(reg.window(),   WIN);
        assertEq(reg.minStake(), MIN);
        assertEq(reg.owner(),    owner);
        assertEq(reg.version(),  "2.0.0");
    }

    function test_constructor_zero_treasury_reverts() public {
        vm.expectRevert(LiquidationRightsRegistryV2.ZeroAddress.selector);
        new LiquidationRightsRegistryV2(address(0), WIN, MIN, owner);
    }

    function test_constructor_window_too_short_reverts() public {
        vm.expectRevert(LiquidationRightsRegistryV2.WindowOutOfBounds.selector);
        new LiquidationRightsRegistryV2(address(vault), 4 minutes, MIN, owner);
    }

    function test_constructor_window_too_long_reverts() public {
        vm.expectRevert(LiquidationRightsRegistryV2.WindowOutOfBounds.selector);
        new LiquidationRightsRegistryV2(address(vault), 61 minutes, MIN, owner);
    }

    function test_constructor_stake_too_low_reverts() public {
        vm.expectRevert(LiquidationRightsRegistryV2.StakeOutOfBounds.selector);
        new LiquidationRightsRegistryV2(address(vault), WIN, 0.0009 ether, owner);
    }

    // ── admin: setTreasury ────────────────────────────────────────────────────

    function test_set_treasury() public {
        address newTreasury = address(0xDEAD);
        vm.prank(owner);
        reg.setTreasury(newTreasury);
        assertEq(reg.treasury(), newTreasury);
    }

    function test_set_treasury_emits_event() public {
        vm.prank(owner);
        vm.expectEmit(true, true, false, false);
        emit LiquidationRightsRegistryV2.TreasuryUpdated(address(vault), address(0xDEAD));
        reg.setTreasury(address(0xDEAD));
    }

    function test_set_treasury_zero_reverts() public {
        vm.prank(owner);
        vm.expectRevert(LiquidationRightsRegistryV2.ZeroAddress.selector);
        reg.setTreasury(address(0));
    }

    function test_set_treasury_non_owner_reverts() public {
        vm.prank(alice);
        vm.expectRevert();
        reg.setTreasury(address(0xDEAD));
    }

    // ── admin: setWindow ──────────────────────────────────────────────────────

    function test_set_window() public {
        vm.prank(owner);
        reg.setWindow(15 minutes);
        assertEq(reg.window(), 15 minutes);
    }

    function test_set_window_at_bounds() public {
        vm.prank(owner);
        reg.setWindow(5 minutes);   // min bound
        assertEq(reg.window(), 5 minutes);

        vm.prank(owner);
        reg.setWindow(60 minutes);  // max bound
        assertEq(reg.window(), 60 minutes);
    }

    function test_set_window_out_of_bounds_reverts() public {
        vm.prank(owner);
        vm.expectRevert(LiquidationRightsRegistryV2.WindowOutOfBounds.selector);
        reg.setWindow(4 minutes);

        vm.prank(owner);
        vm.expectRevert(LiquidationRightsRegistryV2.WindowOutOfBounds.selector);
        reg.setWindow(61 minutes);
    }

    // ── admin: setMinStake ────────────────────────────────────────────────────

    function test_set_min_stake() public {
        vm.prank(owner);
        reg.setMinStake(0.01 ether);
        assertEq(reg.minStake(), 0.01 ether);
    }

    function test_set_min_stake_out_of_bounds_reverts() public {
        vm.prank(owner);
        vm.expectRevert(LiquidationRightsRegistryV2.StakeOutOfBounds.selector);
        reg.setMinStake(0.0009 ether);

        vm.prank(owner);
        vm.expectRevert(LiquidationRightsRegistryV2.StakeOutOfBounds.selector);
        reg.setMinStake(1.0001 ether);
    }

    // ── register (same as v1 + uses mutable window/minStake) ─────────────────

    function test_register_basic() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        (address liq, uint256 stake, uint256 exp, bool exec, bool active) = reg.getRights(borrower);
        assertEq(liq,   alice);
        assertEq(stake, MIN);
        assertGt(exp,   block.timestamp);
        assertFalse(exec);
        assertTrue(active);
    }

    function test_register_respects_updated_min_stake() public {
        vm.prank(owner);
        reg.setMinStake(0.01 ether);

        vm.prank(alice);
        vm.expectRevert(LiquidationRightsRegistryV2.StakeTooLow.selector);
        reg.register{value: MIN}(borrower);   // original MIN now below threshold
    }

    function test_register_uses_updated_window() public {
        vm.prank(owner);
        reg.setWindow(20 minutes);

        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        (, , uint256 exp, ,) = reg.getRights(borrower);
        assertApproxEqAbs(exp, block.timestamp + 20 minutes, 1);
    }

    function test_register_below_min_reverts() public {
        vm.prank(alice);
        vm.expectRevert(LiquidationRightsRegistryV2.StakeTooLow.selector);
        reg.register{value: MIN - 1}(borrower);
    }

    function test_outbid_refunds_previous_holder() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        uint256 aliceBefore = alice.balance;
        vm.prank(bob);
        reg.register{value: MIN * 2}(borrower);

        assertEq(alice.balance, aliceBefore + MIN);
        (address liq,,,,) = reg.getRights(borrower);
        assertEq(liq, bob);
    }

    function test_expired_rights_allow_new_registration() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.warp(block.timestamp + WIN + 1);

        vm.prank(bob);
        reg.register{value: MIN}(borrower);
        (address liq,,,,) = reg.getRights(borrower);
        assertEq(liq, bob);
    }

    // ── recordExecution ───────────────────────────────────────────────────────

    function test_record_execution_returns_stake() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        uint256 aliceBefore = alice.balance;
        vm.prank(alice);
        reg.recordExecution(borrower);

        assertEq(alice.balance, aliceBefore + MIN);
        (,,, bool exec, bool active) = reg.getRights(borrower);
        assertTrue(exec);
        assertFalse(active);
    }

    function test_record_execution_wrong_caller_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.prank(bob);
        vm.expectRevert(LiquidationRightsRegistryV2.NotRightsHolder.selector);
        reg.recordExecution(borrower);
    }

    // ── slash → auto-routes revenue to vault ─────────────────────────────────

    function test_slash_routes_treasury_share_to_vault() public {
        vm.prank(alice);
        reg.register{value: 1 ether}(borrower);

        vm.warp(block.timestamp + WIN + 1);

        uint256 vaultAssetsBefore = vault.totalAssets();
        uint256 charlieBefore     = charlie.balance;

        vm.prank(charlie);
        reg.slash(borrower);

        // Charlie got 50% bounty
        assertEq(charlie.balance, charlieBefore + 0.5 ether);

        // Vault got 50% treasury share — auto-wrapped to WETH, added as yield
        assertEq(vault.totalAssets(), vaultAssetsBefore + 0.5 ether);
    }

    function test_slash_increases_share_price() public {
        // Seed vault so share price is meaningful
        vm.startPrank(owner);
        weth.deposit{value: 1 ether}();
        weth.approve(address(vault), 1 ether);
        uint256 shares = vault.deposit(1 ether, owner);
        vm.stopPrank();

        uint256 priceBefore = vault.convertToAssets(shares);

        // Alice registers, misses window, Charlie slashes
        vm.prank(alice);
        reg.register{value: 1 ether}(borrower);
        vm.warp(block.timestamp + WIN + 1);

        vm.prank(charlie);
        reg.slash(borrower);

        uint256 priceAfter = vault.convertToAssets(shares);
        assertGt(priceAfter, priceBefore);
    }

    function test_slash_before_window_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.prank(charlie);
        vm.expectRevert(LiquidationRightsRegistryV2.WindowNotExpired.selector);
        reg.slash(borrower);
    }

    function test_slash_no_rights_reverts() public {
        vm.prank(charlie);
        vm.expectRevert(LiquidationRightsRegistryV2.NoActiveRights.selector);
        reg.slash(borrower);
    }

    function test_self_slash_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        vm.warp(block.timestamp + WIN + 1);

        vm.prank(alice);
        vm.expectRevert(LiquidationRightsRegistryV2.SelfSlash.selector);
        reg.slash(borrower);
    }

    function test_slash_after_execution_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        vm.prank(alice);
        reg.recordExecution(borrower);
        vm.warp(block.timestamp + WIN + 1);

        vm.prank(charlie);
        vm.expectRevert(LiquidationRightsRegistryV2.AlreadyExecuted.selector);
        reg.slash(borrower);
    }

    // ── treasury update mid-protocol ──────────────────────────────────────────

    function test_treasury_update_affects_next_slash() public {
        address newTreasury = address(0xBEEF1);

        vm.prank(owner);
        reg.setTreasury(newTreasury);

        vm.prank(alice);
        reg.register{value: 1 ether}(borrower);
        vm.warp(block.timestamp + WIN + 1);

        vm.prank(charlie);
        reg.slash(borrower);

        // New treasury (plain address) received the share — not the vault
        assertEq(newTreasury.balance, 0.5 ether);
    }

    // ── full lifecycle with vault integration ─────────────────────────────────

    function test_full_lifecycle_with_vault_yield() public {
        // Seed vault
        vm.startPrank(owner);
        weth.deposit{value: 2 ether}();
        weth.approve(address(vault), 2 ether);
        vault.deposit(2 ether, owner);
        vm.stopPrank();

        uint256 initialShares = vault.balanceOf(owner);

        // Bob registers, misses window
        vm.prank(bob);
        reg.register{value: 1 ether}(borrower);
        vm.warp(block.timestamp + WIN + 1);

        // Charlie slashes — 0.5 ETH goes to charlie, 0.5 ETH routes to vault
        uint256 charlieBefore = charlie.balance;
        vm.prank(charlie);
        reg.slash(borrower);

        assertEq(charlie.balance, charlieBefore + 0.5 ether);
        assertEq(vault.totalAssets(), 2.5 ether);

        // Owner redeems — gets more than deposited
        vm.prank(owner);
        uint256 received = vault.redeem(initialShares, owner, owner);
        assertGt(received, 2 ether);
    }
}
