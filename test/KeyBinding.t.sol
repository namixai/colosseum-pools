// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {Cancel} from "../src/Types.sol";
import {KeyBinder} from "../src/KeyBinder.sol";
import {PoolFactory} from "../src/PoolFactory.sol";
import {Pool} from "../src/Pool.sol";
import {ChallengeAccount} from "../src/ChallengeAccount.sol";
import {RuledAccount} from "../src/RuledAccount.sol";
import {PoolHarness} from "./PoolHarness.sol";

/// The accounts' side of the key binder: only the binder gives a key or takes one, a revoked key is
/// cut as a stop cuts it and asked for again, a stop withdraws a key still on its way, and the
/// factory's operator, who goes to the same owner, hands the role on in two steps.
contract KeyBindingTest is PoolHarness {
    function test_onlyTheBinderGivesAKey_orTakesOne() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        vm.prank(stranger);
        vm.expectRevert(RuledAccount.NotBinder.selector);
        ch.onKeyBound(stranger, KeyBinder.Purpose.Renew);
        vm.prank(stranger);
        vm.expectRevert(RuledAccount.NotBinder.selector);
        ch.onKeyRevoked(key, SALT);
        assertEq(ch.agentKey(), key);
    }

    /// The guardian revokes a running challenge's key: the account cuts it under `pools` as a stop
    /// does, with the guardian's salt, keeps trading state, and asks for a fresh key for its trader,
    /// which a binding gives it.
    function test_revoke_cutsTheAgent_andTheAccountAsksAgain() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        vm.recordLogs();
        vm.prank(guardian);
        binder.revoke(key, SALT);
        (address[] memory from, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1);
        assertEq(from[0], address(ch));
        assertEq(kind[0], 9);
        (address keyless,) = abi.decode(args[0], (address, string));
        assertEq(keyless, _keylessCandidate(address(ch), 0, SALT, 0), "the keyless address the salt picks");
        assertEq(ch.agentKey(), address(0));
        assertEq(ch.cutKey(), key);
        assertEq(uint8(ch.status()), uint8(ChallengeAccount.Status.Active), "the challenge goes on");
        KeyBinder.Request memory r = binder.requestOf(address(ch));
        assertEq(uint8(r.purpose), uint8(KeyBinder.Purpose.Revoked));
        assertEq(r.trader, trader);
        assertEq(ch.keyAskedAt(), block.timestamp);

        address fresh = _bind(address(ch));
        assertEq(ch.agentKey(), fresh);
        assertTrue(binder.isBound(fresh, address(ch), trader));
        assertEq(uint8(binder.bindingOf(key).state), uint8(KeyBinder.State.Retired));
    }

    /// A funded stage's key revoked the same way: cut, asked again, bound again.
    function test_revoke_aFundedStage() public {
        (, address challengeKey) = _passed(_readyPool());
        Pool p = Pool(address(ChallengeAccount(binder.bindingOf(challengeKey).account).pool()));
        address key = p.agentKey();
        vm.prank(operator); // the owner may revoke too
        binder.revoke(key, SALT);
        assertEq(p.agentKey(), address(0));
        assertEq(uint8(p.stage()), uint8(Pool.Stage.Funded));
        assertEq(binder.requestOf(address(p)).trader, trader);
        _bind(address(p));
        assertTrue(binder.isBound(p.agentKey(), address(p), trader));
    }

    /// A stop withdraws a renewal still on its way: the committed key can never be shown.
    function test_aStopWithdrawsAKeyOnItsWay() public {
        ChallengeAccount ch = _started(_readyPool());
        vm.prank(operator);
        binder.setRenewDelay(1 days);
        vm.warp(uint256(ch.agentApprovedAt()) + ch.AGENT_TERM() - ch.RENEW_WINDOW());
        ch.renewAgent();
        address fresh = _newKey();
        binder.commit(address(ch), keccak256(abi.encode(fresh)), _sign(address(ch), fresh));
        (Cancel[] memory c, uint32[] memory a) = _none();
        vm.prank(trader);
        ch.forfeit(c, a, SALT);
        assertEq(ch.keyAskedAt(), 0);
        assertEq(uint8(binder.requestOf(address(ch)).purpose), uint8(KeyBinder.Purpose.None));
        vm.warp(block.timestamp + 1 days);
        vm.expectRevert(KeyBinder.WrongStep.selector);
        binder.reveal(address(ch), fresh);
    }

    /// A rekey asks for its key too, and a second ask while it waits is refused.
    function test_rekey_asksAndWaits() public {
        ChallengeAccount ch = _buy(_readyPool());
        CoreSimulatorLib.nextBlock();
        _activate(ch);
        CoreSimulatorLib.forceSpotBalance(ch.agentKey(), 0, 1e8); // spoiled in the block of its approval
        CoreSimulatorLib.nextBlock();
        vm.prank(trader);
        ch.rekey();
        assertEq(uint8(binder.requestOf(address(ch)).purpose), uint8(KeyBinder.Purpose.Rekey));
        vm.prank(trader);
        vm.expectRevert(RuledAccount.KeyAsked.selector);
        ch.rekey();
        address fresh = _bind(address(ch));
        assertEq(ch.agentKey(), fresh);
        assertEq(ch.rekeys(), 1);
    }

    /// The factory's operator hands the role on in two steps, as the binder's owner does.
    function test_factoryOperator_isTwoStep() public {
        address next = makeAddr("multisig");
        vm.prank(stranger);
        vm.expectRevert(PoolFactory.NotOperator.selector);
        factory.transferOperator(stranger);
        vm.prank(operator);
        factory.transferOperator(next);
        assertEq(factory.operator(), operator, "not until it is accepted");
        assertEq(factory.pendingOperator(), next);
        vm.prank(stranger);
        vm.expectRevert(PoolFactory.NotPendingOperator.selector);
        factory.acceptOperator();
        vm.prank(next);
        factory.acceptOperator();
        assertEq(factory.operator(), next);
        assertEq(factory.pendingOperator(), address(0));
        vm.prank(operator);
        vm.expectRevert(PoolFactory.NotOperator.selector);
        factory.setChallengeFee(0, address(0));
    }

    /// A revoked key's cut can be sent again: HyperCore may drop the replacement, and the revoked key
    /// would keep working. The account's request for a fresh key stays; once one is bound, a live
    /// account is not re-cut by anyone.
    function test_recut_afterARevoke_cutsAgain_andTheRequestStays() public {
        ChallengeAccount ch = _started(_readyPool());
        address key = ch.agentKey();
        vm.prank(guardian);
        binder.revoke(key, SALT);
        CoreSimulatorLib.nextBlock();
        bytes32 again = keccak256("again");
        vm.recordLogs();
        vm.prank(stranger);
        ch.recut(again);
        (, uint24[] memory kind, bytes[] memory args) = _actions(vm.getRecordedLogs());
        assertEq(kind.length, 1);
        assertEq(kind[0], 9);
        (address keyless,) = abi.decode(args[0], (address, string));
        assertEq(keyless, _keylessCandidate(address(ch), 1, again, 0));
        assertEq(uint8(binder.requestOf(address(ch)).purpose), uint8(KeyBinder.Purpose.Revoked), "the request stays");
        assertEq(ch.cutKey(), key);
        _bind(address(ch));
        vm.expectRevert(RuledAccount.NotStopped.selector);
        ch.recut(again);
    }
}
