// SPDX-License-Identifier: MIT
pragma solidity ^0.8.10;

import "forge-std/Test.sol";
import "../contracts/SlashRevenueVault.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/**
 * Unit tests for SlashRevenueVault.
 * Runs against a local fork: all WETH interactions use a mock.
 */
contract MockWETH is ERC20 {
    constructor() ERC20("Wrapped Ether", "WETH") {}
    function deposit() external payable { _mint(msg.sender, msg.value); }
    function withdraw(uint256 amount) external { _burn(msg.sender, amount); payable(msg.sender).transfer(amount); }
    receive() external payable {}
}

contract SlashRevenueVaultTest is Test {

    SlashRevenueVault vault;
    MockWETH          weth;

    address owner   = address(0x0A00);
    address alice   = address(0xA1CE);
    address bob     = address(0xB0B0);
    address charlie = address(0xC4A7);

    uint256 constant ONE  = 1 ether;
    uint256 constant HALF = 0.5 ether;

    function setUp() public {
        weth  = new MockWETH();
        vault = new SlashRevenueVault(address(weth), owner);

        vm.deal(alice,   10 ether);
        vm.deal(bob,     10 ether);
        vm.deal(charlie, 10 ether);
        vm.deal(owner,   10 ether);

        // Mint WETH for depositors
        vm.prank(alice);   weth.deposit{value: 5 ether}();
        vm.prank(bob);     weth.deposit{value: 5 ether}();
        vm.prank(charlie); weth.deposit{value: 5 ether}();
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    function _approve(address user, uint256 amount) internal {
        vm.prank(user);
        weth.approve(address(vault), amount);
    }

    function _deposit(address user, uint256 amount) internal returns (uint256 shares) {
        _approve(user, amount);
        vm.prank(user);
        shares = vault.deposit(amount, user);
    }

    // ── basic deposit / withdraw ──────────────────────────────────────────────

    function test_deposit_mints_shares() public {
        uint256 shares = _deposit(alice, ONE);
        assertGt(shares, 0);
        assertEq(vault.balanceOf(alice), shares);
        assertEq(vault.totalAssets(), ONE);
    }

    function test_redeem_returns_assets() public {
        uint256 shares = _deposit(alice, ONE);
        uint256 wethBefore = weth.balanceOf(alice);

        vm.prank(alice);
        uint256 assets = vault.redeem(shares, alice, alice);

        assertEq(assets, ONE);
        assertEq(weth.balanceOf(alice), wethBefore + ONE);
        assertEq(vault.balanceOf(alice), 0);
    }

    function test_two_depositors_equal_shares() public {
        uint256 aShares = _deposit(alice, ONE);
        uint256 bShares = _deposit(bob,   ONE);
        // Same deposit at same price → same shares
        assertEq(aShares, bShares);
    }

    // ── addRevenue ────────────────────────────────────────────────────────────

    function test_add_revenue_increases_total_assets() public {
        _deposit(alice, ONE);
        uint256 before = vault.totalAssets();

        vm.prank(owner);
        vault.addRevenue{value: HALF}();

        assertEq(vault.totalAssets(), before + HALF);
    }

    function test_add_revenue_increases_share_price() public {
        uint256 shares = _deposit(alice, ONE);

        vm.prank(owner);
        vault.addRevenue{value: ONE}();  // double the assets

        // Alice's shares now redeem for 2x her deposit
        uint256 redeemable = vault.previewRedeem(shares);
        assertGt(redeemable, ONE);
        // With 1 depositor and 1 ETH revenue: should be close to 2 ETH
        assertApproxEqAbs(redeemable, 2 * ONE, 1e6);
    }

    function test_add_revenue_proportional_to_stake() public {
        // Alice deposits 1 ETH, Bob deposits 3 ETH → 1:3 split
        uint256 aShares = _deposit(alice, ONE);
        uint256 bShares = _deposit(bob,   3 * ONE);

        // Owner adds 4 ETH revenue → total assets = 8 ETH
        vm.prank(owner);
        vault.addRevenue{value: 4 * ONE}();

        uint256 aRedeemable = vault.previewRedeem(aShares);
        uint256 bRedeemable = vault.previewRedeem(bShares);

        // Alice had 25% of shares → should get 25% of 8 ETH = 2 ETH
        // Bob   had 75% of shares → should get 75% of 8 ETH = 6 ETH
        assertApproxEqAbs(aRedeemable, 2 * ONE, 1e6);
        assertApproxEqAbs(bRedeemable, 6 * ONE, 1e6);
    }

    function test_add_revenue_emits_event() public {
        _deposit(alice, ONE);

        vm.prank(owner);
        vm.expectEmit(true, false, false, true);
        emit SlashRevenueVault.RevenueAdded(HALF, owner, ONE + HALF);
        vault.addRevenue{value: HALF}();
    }

    function test_add_revenue_zero_reverts() public {
        vm.prank(owner);
        vm.expectRevert(SlashRevenueVault.ZeroValue.selector);
        vault.addRevenue{value: 0}();
    }

    function test_add_revenue_non_owner_reverts() public {
        vm.prank(alice);
        vm.expectRevert();
        vault.addRevenue{value: HALF}();
    }

    // ── receive ETH ───────────────────────────────────────────────────────────

    function test_receive_eth_does_not_auto_add_to_pool() public {
        _deposit(alice, ONE);
        uint256 before = vault.totalAssets();

        // Send ETH directly — should NOT change totalAssets (just sits in contract)
        vm.prank(charlie);
        (bool ok,) = address(vault).call{value: HALF}("");
        assertTrue(ok);

        // totalAssets unchanged — ETH sitting in contract is NOT WETH
        assertEq(vault.totalAssets(), before);
    }

    // ── late depositor gets no past revenue ───────────────────────────────────

    function test_late_depositor_does_not_get_past_revenue() public {
        // Alice deposits, revenue is added, Bob deposits after
        uint256 aShares = _deposit(alice, ONE);

        vm.prank(owner);
        vault.addRevenue{value: ONE}();   // share price is now 2x

        // Bob deposits the same 1 ETH but at the new higher price — fewer shares
        uint256 bShares = _deposit(bob, ONE);

        assertLt(bShares, aShares);   // Bob gets fewer shares at higher price

        // Bob can only redeem what he put in
        assertApproxEqAbs(vault.previewRedeem(bShares), ONE, 1e6);
    }

    // ── full lifecycle ────────────────────────────────────────────────────────

    function test_full_lifecycle() public {
        // 1. Alice deposits 1 ETH
        uint256 aShares = _deposit(alice, ONE);
        assertEq(vault.totalAssets(), ONE);

        // 2. A registration is slashed → 0.0025 ETH bounty to owner (simulated)
        //    Owner forwards slash revenue to vault
        vm.prank(owner);
        vault.addRevenue{value: 0.0025 ether}();
        assertEq(vault.totalAssets(), ONE + 0.0025 ether);

        // 3. Bob deposits 1 ETH at the slightly higher price
        uint256 bShares = _deposit(bob, ONE);
        assertLt(bShares, aShares);   // Bob gets slightly fewer shares — correct

        // 4. Another slash event adds 0.5 ETH
        vm.prank(owner);
        vault.addRevenue{value: HALF}();

        // 5. Alice redeems — gets more than she deposited
        uint256 wethBefore = weth.balanceOf(alice);
        vm.prank(alice);
        uint256 aOut = vault.redeem(aShares, alice, alice);
        assertGt(aOut, ONE);   // earned yield
        assertGt(weth.balanceOf(alice), wethBefore);

        // 6. Bob redeems — also earned yield
        vm.prank(bob);
        uint256 bOut = vault.redeem(bShares, bob, bob);
        assertGt(bOut, ONE);

        // 7. Vault should be empty (or near-empty) after all redemptions
        assertEq(vault.totalSupply(), 0);
    }

    // ── ownership ─────────────────────────────────────────────────────────────

    function test_owner_is_set_correctly() public {
        assertEq(vault.owner(), owner);
    }

    function test_transfer_ownership() public {
        vm.prank(owner);
        vault.transferOwnership(alice);
        assertEq(vault.owner(), alice);

        // Alice can now add revenue
        vm.prank(alice);
        vault.addRevenue{value: HALF}();
    }

    // ── share token metadata ──────────────────────────────────────────────────

    function test_share_token_metadata() public {
        assertEq(vault.name(),   "Slash Revenue ETH");
        assertEq(vault.symbol(), "srvETH");
        assertEq(vault.asset(),  address(weth));
    }
}
