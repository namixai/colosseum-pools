// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";

import {Rules, Terms, Cancel, Breach} from "../src/Types.sol";
import {Pool} from "../src/Pool.sol";
import {ChallengeAccount} from "../src/ChallengeAccount.sol";
import {RuledAccount} from "../src/RuledAccount.sol";
import {KeyRegistry} from "../src/KeyRegistry.sol";
import {PoolFactory} from "../src/PoolFactory.sol";
import {CoreOps} from "../src/lib/CoreOps.sol";
import {PoolHarness} from "./PoolHarness.sol";

/// HIP-3: markets on a builder-deployed perp dex (the CTO, 21 October 2026). КОЛИЗЕЙ-2's spike q9 on
/// testnet measured what these tests hold to:
/// - a contract moves its capital to the dex with sendAsset (action 13) from spot, and
///   accountMarginSummary(dex, account) reads it;
/// - CoreWriter's order and cancel take the asset's order id, 100000 + dex * 10000 + index;
/// - the precompiles take the id less 100000, and a wrong one reads another dex's market.
/// The simulator has dex 1 with USDC as its collateral; its first market here is "xyz:XYZ100".
contract Hip3Test is PoolHarness {
    uint32 constant DEX = 1;
    uint32 constant XYZ = 110_000; // the order id: 100000 + 1 * 10000 + 0
    uint32 constant XYZ_PX = 10_000; // what the precompiles take
    uint64 constant XYZ_MARK = 29_333_00; // 29,333.00 with 6 - szDecimals = 2 decimals

    function setUp() public override {
        super.setUp();
        hyperCore.registerPerpAssetInfo(
            XYZ_PX,
            PrecompileLib.PerpAssetInfo({
                coin: "xyz:XYZ100", marginTableId: 30, szDecimals: 4, maxLeverage: 30, onlyIsolated: false
            })
        );
        CoreSimulatorLib.setMarkPx(XYZ_PX, XYZ_MARK);
    }

    /// Lists xyz:XYZ100 on the factory, as each test that trades it does first: a listing that
    /// fails then fails that test, by its name.
    function _listXyz() internal {
        uint32[] memory listed = new uint32[](1);
        listed[0] = XYZ;
        string[] memory names = new string[](1);
        names[0] = "xyz:XYZ100";
        vm.prank(operator);
        factory.setPlatformAssets(listed, names, true);
    }

    function _xyzRules() internal pure returns (Rules memory r) {
        r = _rules();
        r.assets[0] = XYZ;
    }

    function _xyzPool() internal returns (Pool p) {
        _listXyz();
        vm.prank(investor);
        p = Pool(factory.createPool(_xyzRules(), _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
    }

    /// A challenge on the dex started: the first activate() sends its capital, the second, a block
    /// later, starts it (audit window 2, В4-2).
    function _startedOnDex() internal returns (ChallengeAccount ch) {
        ch = _buy(_xyzPool());
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
    }

    function _owner(Pool p) internal {
        vm.prank(investor);
        p.runByOwner(false);
    }

    function _has(uint24[] memory kind, uint24 k) internal pure returns (bool) {
        for (uint256 i = 0; i < kind.length; ++i) {
            if (kind[i] == k) return true;
        }
        return false;
    }

    function test_hip3_theIdsAndTheDex() public pure {
        assertEq(CoreOps.pxIndex(3), 3, "the main dex: one number");
        assertEq(CoreOps.pxIndex(99_999), 99_999);
        assertEq(CoreOps.pxIndex(100_000), 0);
        assertEq(CoreOps.pxIndex(750_000), 650_000, "xyz's first market, as q9 read it");
        assertEq(CoreOps.dexOf(3), 0);
        assertEq(CoreOps.dexOf(750_000), 65);
        assertEq(CoreOps.dexOf(759_999), 65);
        assertEq(CoreOps.dexOf(760_000), 66);
    }

    /// The account's dex is its markets', and its capital goes there by sendAsset. The first
    /// activate() only sends it: no agent, no start. A later block that shows it on the dex starts
    /// the challenge, and its equity is the main dex's and that dex's.
    function test_hip3_aChallengesCapitalGoesToItsDex_andItStartsOnceTheDexShowsIt() public {
        Pool p = _xyzPool();
        assertEq(p.dex(), DEX);
        ChallengeAccount ch = _buy(p);
        assertEq(ch.dex(), DEX);
        CoreSimulatorLib.nextBlock();
        vm.recordLogs();
        ch.activate();
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        bool sent;
        for (uint256 i = 0; i < kind.length; ++i) {
            assertTrue(kind[i] != 7, "no class transfer to the main dex");
            if (kind[i] == 13) {
                assertEq(args[i], abi.encode(address(ch), address(0), HLConstants.SPOT_DEX, DEX, uint64(0), uint64(100e8)));
                sent = true;
            }
        }
        assertTrue(sent, "the capital is sent to the dex");
        assertFalse(_has(kind, 9), "no agent before the capital is there");
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Created), "not started on a transfer in flight");
        assertEq(ch.sentToDexAt(), block.timestamp);
        assertEq(ch.sentToDex(), 100e6);
        vm.expectRevert(RuledAccount.NotOnDex.selector);
        ch.activate(); // the same block: a precompile reads its start

        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.accountMarginSummary(DEX, address(ch)).accountValue, 100e6);
        vm.recordLogs();
        ch.activate();
        (, kind,) = _actions(vm.getRecordedLogs());
        assertTrue(_has(kind, 9), "the trader's key is approved now");
        assertFalse(_has(kind, 13), "nothing sent twice");
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
        assertEq(ch.sentToDexAt(), 0);
        assertEq(ch.dayStartEquity(), 100e6);
        assertEq(ch.accountEquity(), 100e6, "the main dex's and the dex's");
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
    }

    /// The start reads the dex on a later block than the send, never in the send's own: a block's
    /// read is its start, which cannot show the transfer, so value already there must not stand in
    /// for it. And what is there besides the capital goes back to spot before the start.
    function test_hip3_theStartReadsTheDexOnALaterBlockOnly() public {
        ChallengeAccount ch = _buy(_xyzPool());
        CoreSimulatorLib.forcePerpBalance(address(ch), DEX, 100e6); // somebody's, before the start
        CoreSimulatorLib.nextBlock();
        ch.activate();
        assertEq(ch.accountEquity(), 100e6, "the dex already shows as much as the capital");
        vm.expectRevert(RuledAccount.NotOnDex.selector);
        ch.activate();
        CoreSimulatorLib.nextBlock();
        ch.activate(); // 200 there: the 100 over the capital goes back first
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Created));
        CoreSimulatorLib.nextBlock();
        ch.activate();
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
        assertEq(ch.accountEquity(), 100e6);
    }

    function _sentBack(Vm.Log[] memory logs, address account) internal pure returns (uint64 sum) {
        (, uint24[] memory kind, bytes[] memory args) = _actions(logs);
        for (uint256 i = 0; i < kind.length; ++i) {
            if (kind[i] != 13) continue;
            (address dest,, uint32 from, uint32 to,, uint64 wei_) =
                abi.decode(args[i], (address, address, uint32, uint32, uint64, uint64));
            if (dest == account && from == DEX && to == HLConstants.SPOT_DEX) sum += wei_;
        }
    }

    /// The audit's case, В5-1: the first transfer only late, by more than RESEND_WAIT, so it lands
    /// with its repeat. The surplus goes back to spot, and the stage opens on its capital once the
    /// dex shows it alone: the pool's money is not the trader's gain, nor more to trade with.
    function test_hip3_aLateTransferAndItsRepeat_bothLand_andTheSurplusGoesBackFirst() public {
        Pool p = _xyzPool();
        _owner(p);
        vm.prank(investor);
        p.openFundedFor(trader); // 200, still in HyperCore's queue
        vm.warp(block.timestamp + p.RESEND_WAIT() + 1);
        p.openFundedStage(); // the dex shows nothing: sent again
        CoreSimulatorLib.nextBlock(); // both land
        assertEq(p.accountEquity(), 400e6, "twice the capital on the dex");
        vm.recordLogs();
        p.openFundedStage();
        assertEq(_sentBack(vm.getRecordedLogs(), address(p)), 200e8, "the surplus back to spot");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey), "and no stage on it");
        vm.expectRevert(RuledAccount.NotOnDex.selector);
        p.openFundedStage(); // the same block still reads 400: the surplus is not sent twice
        CoreSimulatorLib.nextBlock();
        p.openFundedStage();
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.fundedStart(), 200e6);
        assertEq(p.accountEquity(), 200e6, "the stage trades its capital, no more");

        ChallengeAccount ch = _buy(_xyzPool());
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 200e8); // enough on spot for both
        CoreSimulatorLib.nextBlock();
        ch.activate();
        vm.warp(block.timestamp + ch.RESEND_WAIT() + 1);
        ch.activate();
        CoreSimulatorLib.nextBlock();
        vm.recordLogs();
        ch.activate();
        assertEq(_sentBack(vm.getRecordedLogs(), address(ch)), 100e8);
        CoreSimulatorLib.nextBlock();
        ch.activate();
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
        assertEq(ch.accountEquity(), 100e6);
    }

    /// The audit's case, В4-2: HyperCore drops the transfer to the dex. The challenge does not
    /// start, so nothing reads the capital still on spot as a loss; a minute on, the next call sends
    /// it again, and the challenge starts once it lands.
    function test_hip3_aDroppedTransferToTheDex_isNotALoss_andIsSentAgain() public {
        ChallengeAccount ch = _buy(_xyzPool());
        CoreSimulatorLib.nextBlock();
        ch.activate();
        // Dropped: the spot balance is not there when HyperCore runs the transfer, and is after.
        CoreSimulatorLib.setRevertOnFailure(false);
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 0);
        CoreSimulatorLib.nextBlock();
        CoreSimulatorLib.forceSpotBalance(address(ch), 0, 100e8);
        assertEq(PrecompileLib.accountMarginSummary(DEX, address(ch)).accountValue, 0, "nothing on the dex");

        vm.expectRevert(RuledAccount.NotOnDex.selector);
        ch.activate();
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.expectRevert(abi.encodeWithSelector(ChallengeAccount.BadStatus.selector, ChallengeAccount.Status.Created));
        ch.breach(c, a, SALT);

        vm.warp(ch.sentToDexAt() + ch.RESEND_WAIT());
        vm.expectRevert(RuledAccount.NotOnDex.selector);
        ch.activate(); // not yet a minute

        vm.warp(block.timestamp + 1);
        vm.recordLogs();
        ch.activate();
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1, "the transfer again, and nothing else");
        assertEq(args[0], abi.encode(address(ch), address(0), HLConstants.SPOT_DEX, DEX, uint64(0), uint64(100e8)));
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Created));
        assertEq(ch.sentToDexAt(), block.timestamp, "its clock starts again");

        CoreSimulatorLib.nextBlock();
        ch.activate();
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active));
        assertEq(ch.accountEquity(), 100e6);
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None));
    }

    /// After the start window, a challenge whose capital the dex never showed can be aborted; one
    /// whose capital is there cannot, though its second call has not been made.
    function test_hip3_abortWaitsOnTheDexNotOnSpot() public {
        Pool p = _xyzPool();
        ChallengeAccount there = _buy(p);
        CoreSimulatorLib.nextBlock();
        there.activate();
        CoreSimulatorLib.nextBlock();
        vm.warp(there.createdAt() + there.START_WINDOW() + 1);
        assertFalse(there.capitalArrived(), "spot is empty: the capital is on the dex");
        vm.expectRevert(ChallengeAccount.CapitalArrived.selector);
        there.abort();
        there.activate();
        assertEq(uint8(there.status()), uint8(ChallengeAccount.Status.Active), "late, and started");

        ChallengeAccount dropped = _buy(_xyzPool());
        CoreSimulatorLib.nextBlock();
        dropped.activate();
        CoreSimulatorLib.setRevertOnFailure(false);
        CoreSimulatorLib.forceSpotBalance(address(dropped), 0, 0);
        CoreSimulatorLib.nextBlock();
        CoreSimulatorLib.forceSpotBalance(address(dropped), 0, 100e8);
        vm.warp(dropped.createdAt() + dropped.START_WINDOW() + 1);
        dropped.abort();
        assertEq(uint8(dropped.status()), uint8(ChallengeAccount.Status.Aborted));
    }

    /// A key spoiled between the two calls stops the start; the challenge is aborted at once and its
    /// capital comes back from the dex at the settlement.
    function test_hip3_aKeySpoiledBetweenTheCalls_abortsAndTheDexGivesTheCapitalBack() public {
        ChallengeAccount ch = _buy(_xyzPool());
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
        CoreSimulatorLib.forceAccountActivation(ch.agentKey());
        vm.expectRevert(ChallengeAccount.KeySpoiled.selector);
        ch.activate();
        ch.abort();
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Aborted));
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.recordLogs();
        ch.settle(c, a);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        bool back;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (kind[i] == 13) {
                assertEq(args[i], abi.encode(address(ch), address(0), DEX, HLConstants.SPOT_DEX, uint64(0), uint64(100e8)));
                back = true;
            }
        }
        assertTrue(back, "the dex's value goes back to spot");
    }

    /// A funded stage on the dex: the owner's call sends the capital there and the pool waits as
    /// one whose trader passed; a later block that shows it there opens the stage, its start the
    /// dex's equity.
    function test_hip3_aFundedStageOnTheDexOpensOnceItsCapitalIsThere() public {
        Pool p = _xyzPool();
        _owner(p);
        vm.recordLogs();
        vm.prank(investor);
        p.openFundedFor(trader);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        bool sent;
        for (uint256 i = 0; i < kind.length; ++i) {
            assertTrue(kind[i] != 7, "no class transfer to the main dex");
            if (kind[i] == 13) {
                assertEq(args[i], abi.encode(address(p), address(0), HLConstants.SPOT_DEX, DEX, uint64(0), uint64(200e8)));
                sent = true;
            }
        }
        assertTrue(sent);
        assertFalse(_has(kind, 9), "no agent before the capital is there");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey));
        assertEq(p.fundedStart(), 200e6);
        address key = p.reservedKey();
        assertTrue(key != address(0), "the key is taken, and waits");
        vm.expectRevert(RuledAccount.NotOnDex.selector);
        p.openFundedStage();

        CoreSimulatorLib.nextBlock();
        vm.prank(stranger);
        p.openFundedStage(); // anyone; the keeper does
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.agentKey(), key);
        assertEq(p.reservedKey(), address(0));
        assertEq(p.fundedTrader(), trader);
        assertEq(p.dayStartEquity(), 200e6);
        assertEq(p.accountEquity(), 200e6);
        assertEq(uint8(p.violation(new uint32[](0))), uint8(Breach.None));
    }

    /// After a pass on the dex, the first openFundedStage() sends the stage's capital with the key
    /// the sale reserved waiting, and a later one opens the stage on it.
    function test_hip3_aPassOnTheDex_opensItsFundedStageOnceTheCapitalIsThere() public {
        ChallengeAccount ch = _startedOnDex();
        Pool p = Pool(address(ch.pool()));
        address key = p.reservedKey();
        vm.mockCall(
            HLConstants.ACCOUNT_MARGIN_SUMMARY_PRECOMPILE_ADDRESS,
            abi.encode(DEX, address(ch)),
            abi.encode(PrecompileLib.AccountMarginSummary({accountValue: 110e6, marginUsed: 0, ntlPos: 0, rawUsd: 110e6}))
        );
        ch.graduate(SALT);
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey));

        vm.recordLogs();
        vm.prank(stranger);
        p.openFundedStage();
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1, "the transfer, and no agent yet");
        assertEq(args[0], abi.encode(address(p), address(0), HLConstants.SPOT_DEX, DEX, uint64(0), uint64(200e8)));
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey));
        assertEq(p.reservedKey(), key, "the sale's key waits");
        assertEq(p.fundedStart(), 200e6);

        CoreSimulatorLib.nextBlock();
        vm.recordLogs();
        vm.expectEmit(address(p));
        emit Pool.TraderFunded(trader, key, 200e6);
        p.openFundedStage();
        (, kind,) = _actions(vm.getRecordedLogs());
        assertTrue(_has(kind, 9) && !_has(kind, 13), "the key now, nothing sent twice");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.agentKey(), key);
        assertEq(p.dayStartEquity(), 200e6);
        assertEq(p.accountEquity(), 200e6);
    }

    /// The same for a dropped transfer of a funded stage's capital: no stage on capital that is not
    /// there, the transfer again a minute on, and the stage once it lands.
    function test_hip3_aFundedStagesDroppedTransferIsSentAgain() public {
        Pool p = _xyzPool();
        _owner(p);
        vm.prank(investor);
        p.openFundedFor(trader);
        CoreSimulatorLib.setRevertOnFailure(false);
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 0);
        CoreSimulatorLib.nextBlock();
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        vm.expectRevert(RuledAccount.NotOnDex.selector);
        p.openFundedStage();
        vm.warp(p.sentToDexAt() + p.RESEND_WAIT() + 1);
        vm.recordLogs();
        p.openFundedStage();
        (, uint24[] memory kind,) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1);
        assertEq(kind[0], 13, "the transfer again");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.PassedAwaitingKey));
        CoreSimulatorLib.nextBlock();
        p.openFundedStage();
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(p.dayStartEquity(), 200e6);
        assertEq(p.accountEquity(), 200e6);
    }

    /// A pool released while its stage's capital is on the dex sends that capital back to spot,
    /// where its owner can take it.
    function test_hip3_anAbandonedStageBringsItsCapitalBackFromTheDex() public {
        Pool p = _xyzPool();
        _owner(p);
        uint256 calledAt = block.timestamp;
        vm.prank(investor);
        p.openFundedFor(trader);
        assertEq(p.passedAt(), calledAt, "the wait runs from the owner's call");
        CoreSimulatorLib.nextBlock();
        for (uint256 i = 0; i < keys.length; ++i) {
            if (registry.bindingOf(keys[i]).state == KeyRegistry.State.Free) {
                CoreSimulatorLib.forceAccountActivation(keys[i]);
            }
        }
        CoreSimulatorLib.forceAccountActivation(p.reservedKey());
        registry.purgeSpoiled(keys.length);
        vm.warp(calledAt + p.AWAIT_KEY_WINDOW());
        vm.expectRevert(Pool.TooEarly.selector);
        p.abandonFundedStage(); // the window runs from the owner's call
        vm.warp(block.timestamp + 1);
        vm.recordLogs();
        p.abandonFundedStage();
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        bool back;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (kind[i] == 13) {
                assertEq(args[i], abi.encode(address(p), address(0), DEX, HLConstants.SPOT_DEX, uint64(0), uint64(200e8)));
                back = true;
            }
        }
        assertTrue(back, "the dex's value goes back to spot");
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Idle));
        assertEq(p.sentToDexAt(), 0);
    }

    /// A position on the dex is read by the index and closed by the order id.
    function test_hip3_aPositionIsReadByTheIndex_andClosedByTheOrderId() public {
        ChallengeAccount ch = _startedOnDex();
        vm.mockCall(
            HLConstants.POSITION2_PRECOMPILE_ADDRESS,
            abi.encode(address(ch), XYZ_PX),
            abi.encode(PrecompileLib.Position({szi: 30, entryNtl: 88e6, isolatedRawUsd: 0, leverage: 20, isIsolated: false}))
        );
        vm.mockCall(
            HLConstants.ACCOUNT_MARGIN_SUMMARY_PRECOMPILE_ADDRESS,
            abi.encode(DEX, address(ch)),
            abi.encode(PrecompileLib.AccountMarginSummary({accountValue: 100e6, marginUsed: 5e6, ntlPos: 88e6, rawUsd: 12e6}))
        );
        assertEq(ch.openNotional(), 88e6, "the dex's notional counts");
        assertEq(ch.accountEquity(), 100e6);
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.recordLogs();
        vm.prank(trader);
        ch.forfeit(c, a, SALT);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        bool closed;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (kind[i] != 1) continue;
            (uint32 asset, bool isBuy, uint64 px, uint64 sz, bool reduceOnly,,) =
                abi.decode(args[i], (uint32, bool, uint64, uint64, bool, uint8, uint128));
            assertEq(asset, XYZ, "the order names the order id");
            assertFalse(isBuy);
            // The dex's mark less 5 %, 27,866.35, five integer digits so rounded to 27,866, in 1e8.
            assertEq(px, 2_786_600_000_000, "priced from the index's mark");
            assertEq(sz, 30 * 1e4, "0.0030 in 1e8, at the market's four size decimals");
            assertTrue(reduceOnly);
            closed = true;
        }
        assertTrue(closed);
    }

    /// Each asset's own cap reads a builder dex's position and mark at the index the precompiles take.
    function test_hip3_anAssetsOwnCapReadsTheDexAtItsIndex() public {
        _listXyz();
        Rules memory r = _xyzRules();
        r.assetLeverageX100 = new uint32[](1);
        r.assetLeverageX100[0] = 200;
        vm.prank(investor);
        Pool p = Pool(factory.createPool(r, _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        ChallengeAccount ch = _buy(p);
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
        ch.activate();
        CoreSimulatorLib.nextBlock();
        vm.mockCall(
            HLConstants.ACCOUNT_MARGIN_SUMMARY_PRECOMPILE_ADDRESS,
            abi.encode(DEX, address(ch)),
            abi.encode(PrecompileLib.AccountMarginSummary({accountValue: 100e6, marginUsed: 10e6, ntlPos: 253e6, rawUsd: 0}))
        );
        vm.mockCall(
            HLConstants.POSITION2_PRECOMPILE_ADDRESS,
            abi.encode(address(ch), XYZ_PX),
            abi.encode(PrecompileLib.Position({szi: 68, entryNtl: 199e6, isolatedRawUsd: 0, leverage: 5, isIsolated: false}))
        );
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.None), "0.0068 at 29,333.00: 199.46 on 100");
        vm.mockCall(
            HLConstants.POSITION2_PRECOMPILE_ADDRESS,
            abi.encode(address(ch), XYZ_PX),
            abi.encode(PrecompileLib.Position({szi: 86, entryNtl: 252e6, isolatedRawUsd: 0, leverage: 5, isIsolated: false}))
        );
        assertEq(uint8(ch.violation(new uint32[](0))), uint8(Breach.Leverage), "0.0086: 252.26 on 100, past 2x");
    }

    /// Settling moves the dex's whole value back to spot by sendAsset, and nothing counts as off the
    /// perp side until the dex is empty.
    function test_hip3_settlingMovesTheDexsValueBackToSpot() public {
        ChallengeAccount ch = _startedOnDex();
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        ch.forfeit(c, a, SALT);
        vm.recordLogs();
        ch.settle(c, a);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        bool back;
        for (uint256 i = 0; i < kind.length; ++i) {
            if (kind[i] == 13) {
                assertEq(args[i], abi.encode(address(ch), address(0), DEX, HLConstants.SPOT_DEX, uint64(0), uint64(100e8)));
                back = true;
            }
        }
        assertTrue(back, "the dex's value is sent back to spot");
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Forfeited), "not settled while the dex holds it");
        CoreSimulatorLib.nextBlock();
        assertEq(PrecompileLib.accountMarginSummary(DEX, address(ch)).accountValue, 0);
    }

    /// Listing checks the market's name: one id off reads another market, and is refused.
    function test_hip3_listingChecksTheMarketsName() public {
        uint32[] memory one = new uint32[](1);
        one[0] = XYZ;
        string[] memory wrong = new string[](1);
        wrong[0] = "xyz:TSLA";
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(PoolFactory.NotThatMarket.selector, XYZ, "xyz:XYZ100"));
        factory.setFundedAssets(one, wrong, true);
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(PoolFactory.NotThatMarket.selector, XYZ, "xyz:XYZ100"));
        factory.setPlatformAssets(one, wrong, true);
        one[0] = XYZ + 1; // nothing registered there in the simulator: no name
        string[] memory right = new string[](1);
        right[0] = "xyz:XYZ100";
        vm.prank(operator);
        vm.expectRevert(abi.encodeWithSelector(PoolFactory.NotThatMarket.selector, XYZ + 1, ""));
        factory.setFundedAssets(one, right, true);
        one[0] = XYZ;
        vm.prank(operator);
        factory.setFundedAssets(one, right, true);
        assertTrue(factory.isFundedAsset(XYZ));
    }

    /// An id from 100000 to 109999 is dex 0 by dexOf, and the precompiles read the main dex's market
    /// under it; listed by that market's name, orders would carry an id HyperCore does not give it.
    /// Refused in both lists, at both ends of the range (audit window 2, В4-4).
    function test_hip3_aDexZeroAliasIsRefused() public {
        uint32[] memory one = new uint32[](1);
        string[] memory name = new string[](1);
        uint32[3] memory aliases = [uint32(100_000 + BTC), uint32(100_000), uint32(109_999)];
        for (uint256 i = 0; i < aliases.length; ++i) {
            one[0] = aliases[i];
            name[0] = PrecompileLib.perpAssetInfo(CoreOps.pxIndex(aliases[i])).coin;
            vm.prank(operator);
            vm.expectRevert(abi.encodeWithSelector(PoolFactory.NotAnOrderId.selector, aliases[i]));
            factory.setPlatformAssets(one, name, true);
            vm.prank(operator);
            vm.expectRevert(abi.encodeWithSelector(PoolFactory.NotAnOrderId.selector, aliases[i]));
            factory.setFundedAssets(one, name, true);
        }
        assertFalse(factory.isPlatformAsset(100_000 + BTC));
        _listXyz(); // the first id of dex 1 still lists
        assertTrue(factory.isPlatformAsset(XYZ));
    }

    /// One dex an account: rules naming markets on two dexes are refused.
    function test_hip3_rulesOnTwoDexesAreRefused() public {
        _listXyz();
        Rules memory r = _rules();
        uint32[] memory both = new uint32[](2);
        both[0] = BTC;
        both[1] = XYZ;
        r.assets = both;
        Terms memory t = _terms();
        vm.prank(investor);
        vm.expectRevert(PoolFactory.BadRules.selector);
        factory.createPool(r, t);
    }
}
