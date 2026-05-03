// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import "forge-std/Test.sol";
import "../contracts/LiquidationRightsRegistry.sol";

contract LiquidationRightsRegistryTest is Test {

    LiquidationRightsRegistry reg;

    address treasury  = address(0xFEE5);
    address alice     = address(0xA1CE);  // first liquidator
    address bob       = address(0xB0B0);  // competing liquidator
    address charlie   = address(0xC4A7);  // slasher
    address borrower  = address(0xBEEF);

    uint256 constant MIN  = 0.005 ether;
    uint256 constant WIN  = 10 minutes;

    function setUp() public {
        reg = new LiquidationRightsRegistry(treasury);
        vm.deal(alice,   10 ether);
        vm.deal(bob,     10 ether);
        vm.deal(charlie, 10 ether);
    }

    // ── register ──────────────────────────────────────────────────────────────

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

    function test_register_below_min_reverts() public {
        vm.prank(alice);
        vm.expectRevert(LiquidationRightsRegistry.StakeTooLow.selector);
        reg.register{value: MIN - 1}(borrower);
    }

    function test_register_twice_same_borrower_reverts_without_outbid() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.prank(bob);
        vm.expectRevert(LiquidationRightsRegistry.OutbidRequires2x.selector);
        reg.register{value: MIN}(borrower);   // same amount, not 2x
    }

    function test_expired_rights_allow_new_registration() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.warp(block.timestamp + WIN + 1);  // expire alice's window

        vm.prank(bob);
        reg.register{value: MIN}(borrower);  // should succeed

        (address liq,,,,) = reg.getRights(borrower);
        assertEq(liq, bob);
    }

    // ── outbid ────────────────────────────────────────────────────────────────

    function test_outbid_refunds_previous_holder() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        uint256 aliceBefore = alice.balance;

        vm.prank(bob);
        reg.register{value: MIN * 2}(borrower);   // 2x to outbid

        assertEq(alice.balance, aliceBefore + MIN);  // refunded

        (address liq,,,,) = reg.getRights(borrower);
        assertEq(liq, bob);
    }

    function test_outbid_exact_2x_succeeds() public {
        vm.prank(alice);
        reg.register{value: 1 ether}(borrower);

        vm.prank(bob);
        reg.register{value: 2 ether}(borrower);  // exactly 2x

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

        (,, , bool exec, bool active) = reg.getRights(borrower);
        assertTrue(exec);
        assertFalse(active);
    }

    function test_record_execution_wrong_caller_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.prank(bob);
        vm.expectRevert(LiquidationRightsRegistry.NotRightsHolder.selector);
        reg.recordExecution(borrower);
    }

    function test_record_execution_double_call_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.prank(alice);
        reg.recordExecution(borrower);

        vm.prank(alice);
        vm.expectRevert(LiquidationRightsRegistry.AlreadyExecuted.selector);
        reg.recordExecution(borrower);
    }

    function test_record_execution_after_window_still_works() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.warp(block.timestamp + WIN + 1);  // window expired but not slashed yet

        uint256 aliceBefore = alice.balance;
        vm.prank(alice);
        reg.recordExecution(borrower);  // late confirmation — stake returned

        assertEq(alice.balance, aliceBefore + MIN);
    }

    // ── slash ─────────────────────────────────────────────────────────────────

    function test_slash_distributes_correctly() public {
        vm.prank(alice);
        reg.register{value: 1 ether}(borrower);

        vm.warp(block.timestamp + WIN + 1);

        uint256 charlieBefore  = charlie.balance;
        uint256 treasuryBefore = treasury.balance;

        vm.prank(charlie);
        reg.slash(borrower);

        assertEq(charlie.balance,  charlieBefore  + 0.5 ether);  // 50% bounty
        assertEq(treasury.balance, treasuryBefore + 0.5 ether);  // 50% treasury

        (address liq,,,,) = reg.getRights(borrower);
        assertEq(liq, address(0));
    }

    function test_slash_before_window_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.prank(charlie);
        vm.expectRevert(LiquidationRightsRegistry.WindowNotExpired.selector);
        reg.slash(borrower);
    }

    function test_slash_no_rights_reverts() public {
        vm.prank(charlie);
        vm.expectRevert(LiquidationRightsRegistry.NoActiveRights.selector);
        reg.slash(borrower);
    }

    function test_self_slash_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);

        vm.warp(block.timestamp + WIN + 1);

        vm.prank(alice);
        vm.expectRevert(LiquidationRightsRegistry.SelfSlash.selector);
        reg.slash(borrower);
    }

    function test_slash_after_execution_reverts() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        vm.prank(alice);
        reg.recordExecution(borrower);

        vm.warp(block.timestamp + WIN + 1);

        vm.prank(charlie);
        vm.expectRevert(LiquidationRightsRegistry.AlreadyExecuted.selector);
        reg.slash(borrower);
    }

    // ── hasActiveRights ───────────────────────────────────────────────────────

    function test_has_active_rights_true_for_holder() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        assertTrue(reg.hasActiveRights(borrower, alice));
    }

    function test_has_active_rights_false_for_non_holder() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        assertFalse(reg.hasActiveRights(borrower, bob));
    }

    function test_has_active_rights_false_after_expiry() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        vm.warp(block.timestamp + WIN + 1);
        assertFalse(reg.hasActiveRights(borrower, alice));
    }

    function test_has_active_rights_false_after_execution() public {
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        vm.prank(alice);
        reg.recordExecution(borrower);
        assertFalse(reg.hasActiveRights(borrower, alice));
    }

    // ── full flow ─────────────────────────────────────────────────────────────

    function test_full_happy_path() public {
        // Alice registers
        vm.prank(alice);
        reg.register{value: MIN}(borrower);
        assertTrue(reg.hasActiveRights(borrower, alice));
        assertFalse(reg.hasActiveRights(borrower, bob));

        // Alice executes the liquidation (off-chain), then records it
        uint256 aliceStart = alice.balance;
        vm.prank(alice);
        reg.recordExecution(borrower);

        // Stake returned, slot is free
        assertEq(alice.balance, aliceStart + MIN);
        assertFalse(reg.hasActiveRights(borrower, alice));

        // Bob can now register on the same borrower
        vm.prank(bob);
        reg.register{value: MIN}(borrower);
        assertTrue(reg.hasActiveRights(borrower, bob));
    }

    function test_full_slash_path() public {
        vm.prank(alice);
        reg.register{value: 2 ether}(borrower);

        // Alice misses her window
        vm.warp(block.timestamp + WIN + 1);

        uint256 charlieBefore = charlie.balance;
        vm.prank(charlie);
        reg.slash(borrower);

        // Charlie got the bounty, treasury got the rest
        assertEq(charlie.balance, charlieBefore + 1 ether);
        assertEq(treasury.balance, 1 ether);

        // Slot is free — bob can register
        vm.prank(bob);
        reg.register{value: MIN}(borrower);
        assertTrue(reg.hasActiveRights(borrower, bob));
    }
}
