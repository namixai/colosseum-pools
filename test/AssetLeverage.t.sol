// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";

import {Rules, Cancel, Breach} from "../src/Types.sol";
import {RuledAccount} from "../src/RuledAccount.sol";
import {Pool} from "../src/Pool.sol";
import {ChallengeAccount} from "../src/ChallengeAccount.sol";
import {PoolFactory} from "../src/PoolFactory.sol";
import {PoolHarness} from "./PoolHarness.sol";

/// Each asset's own leverage cap (the CTO, 21 October 2026): `Rules.assetLeverageX100`, one for each
/// asset or none, from 1x to the account's cap; a position past its asset's cap is a Leverage breach,
/// though the account as a whole is under its own.
contract AssetLeverageTest is PoolHarness {
    uint64 constant BTC_PX = 1_000_000; // 100000.0, one decimal (szDecimals 5)
    uint64 constant ETH_PX = 250_000; // 2500.00, two decimals (szDecimals 4)

    /// BTC and ETH under 5x for the account, BTC at most 5x of it and ETH at most 2x.
    function _capped() internal pure returns (Rules memory r) {
        uint32[] memory assets = new uint32[](2);
        assets[0] = BTC;
        assets[1] = ETH;
        uint32[] memory caps = new uint32[](2);
        caps[0] = 500;
        caps[1] = 200;
        r = Rules({dailyLossBps: 500, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: assets, assetLeverageX100: caps});
    }

    function _create(Rules memory r) internal returns (Pool p) {
        vm.prank(investor);
        p = Pool(factory.createPool(r, _terms()));
    }

    function _refused(Rules memory r) internal {
        vm.prank(investor);
        vm.expectRevert(PoolFactory.BadRules.selector);
        factory.createPool(r, _terms());
    }

    function test_assetCaps_none_orOneForEachAsset_from1xToTheAccountsCap() public {
        Rules memory r = _capped();
        r.assetLeverageX100 = new uint32[](1);
        r.assetLeverageX100[0] = 200;
        _refused(r); // one for two assets
        r = _capped();
        r.assetLeverageX100 = new uint32[](3);
        r.assetLeverageX100[0] = 200;
        r.assetLeverageX100[1] = 200;
        r.assetLeverageX100[2] = 200;
        _refused(r); // three for two
        r = _capped();
        r.assetLeverageX100[1] = 99;
        _refused(r);
        r = _capped();
        r.assetLeverageX100[0] = 501;
        _refused(r); // over the account's cap
        r = _capped();
        r.assetLeverageX100[0] = 500;
        r.assetLeverageX100[1] = 100;
        Pool p = _create(r); // both ends
        assertEq(keccak256(abi.encode(p.rules().assetLeverageX100)), keccak256(abi.encode(r.assetLeverageX100)), "the pool keeps them");
        r.assetLeverageX100 = new uint32[](0);
        _create(r); // none: the account's cap alone
    }

    /// The caps go with the pool's rules into each challenge it sells.
    function _startedCapped() internal returns (ChallengeAccount ch) {
        Pool p = _create(_capped());
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        ch = _started(p);
        assertEq(keccak256(abi.encode(ch.rules().assetLeverageX100)), keccak256(abi.encode(_capped().assetLeverageX100)));
        CoreSimulatorLib.setMarkPx(BTC, BTC_PX);
        CoreSimulatorLib.setMarkPx(ETH, ETH_PX);
    }

    function _hold(ChallengeAccount ch, uint32 asset, int64 szi, uint64 ntl) internal {
        vm.mockCall(
            HLConstants.POSITION2_PRECOMPILE_ADDRESS,
            abi.encode(address(ch), asset),
            abi.encode(PrecompileLib.Position({szi: szi, entryNtl: ntl, isolatedRawUsd: 0, leverage: 5, isIsolated: false}))
        );
    }

    function _account(ChallengeAccount ch, uint64 ntl) internal {
        vm.mockCall(
            HLConstants.ACCOUNT_MARGIN_SUMMARY_PRECOMPILE_ADDRESS,
            abi.encode(uint32(0), address(ch)),
            abi.encode(PrecompileLib.AccountMarginSummary({accountValue: 100e6, marginUsed: 10e6, ntlPos: ntl, rawUsd: 100e6}))
        );
    }

    /// ETH past 2x is a Leverage breach with the account at 2.25x of its 5x; at 2x it is not; a short
    /// counts the same; BTC at 3x under its 5x is not.
    function test_assetCaps_aPositionPastItsAssetsCap_isALeverageBreach() public {
        ChallengeAccount ch = _startedCapped();
        _hold(ch, ETH, 800, 200e6); // 0.08 ETH at 2500.00: 200 USDC, 2x
        _account(ch, 200e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None), "at its cap");
        _hold(ch, ETH, 900, 225e6); // 2.25x
        _account(ch, 225e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Leverage), "past its cap");
        _hold(ch, ETH, -900, 225e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Leverage), "a short the same");
        _hold(ch, ETH, 0, 0);
        _hold(ch, BTC, 300, 300e6); // 0.003 BTC at 100000.0: 300 USDC, 3x of BTC's 5x
        _account(ch, 300e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None), "each asset its own cap");
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.expectRevert(RuledAccount.NoBreach.selector);
        ch.breach(c, a, SALT);
    }
}
