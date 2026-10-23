// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {console2} from "forge-std/console2.sol";
import {Vm} from "forge-std/Vm.sol";
import {CoreSimulatorLib} from "@hyper-evm-lib/test/simulation/CoreSimulatorLib.sol";
import {PrecompileLib} from "@hyper-evm-lib/src/PrecompileLib.sol";
import {HLConstants} from "@hyper-evm-lib/src/common/HLConstants.sol";

import {Rules, Cancel, Breach} from "../../src/Types.sol";
import {Pool} from "../../src/Pool.sol";
import {ChallengeAccount} from "../../src/ChallengeAccount.sol";
import {StopGasProbe} from "../../src/spike/StopGasProbe.sol";
import {PoolHarness} from "../PoolHarness.sol";

/// The worst stop (audit window 2, В6-3): sixteen assets in the rules, each capped and open, sixteen
/// extra assets open outside them, thirty-two cancels. Here, on the simulator, the stop is measured
/// whole and the probe beside it. The probe makes the same precompile reads and the same CoreWriter
/// actions; the stop's gas less the probe's is the account's own storage, events and the registry's
/// retire. spike/run.py stopgas measures the probe on HyperEVM itself, and the two added give the
/// worst stop there.
contract StopGasTest is PoolHarness {
    uint32 constant FIRST = 100;
    uint256 constant N = 16;

    function _asset(uint256 i) internal pure returns (uint32) {
        return FIRST + uint32(i);
    }

    function _register(uint256 n) internal {
        for (uint256 i = 0; i < n; ++i) {
            hyperCore.registerPerpAssetInfo(
                _asset(i),
                PrecompileLib.PerpAssetInfo({
                    coin: string.concat("A", vm.toString(i)), marginTableId: 1, szDecimals: 2, maxLeverage: 10,
                    onlyIsolated: false
                })
            );
            CoreSimulatorLib.setMarkPx(_asset(i), 1_000_000); // 10000.0000
        }
    }

    function _open(address account, uint256 n) internal {
        for (uint256 i = 0; i < n; ++i) {
            vm.mockCall(
                HLConstants.POSITION2_PRECOMPILE_ADDRESS,
                abi.encode(account, _asset(i)),
                abi.encode(PrecompileLib.Position({szi: 1, entryNtl: 1e6, isolatedRawUsd: 0, leverage: 5, isIsolated: false}))
            );
        }
        vm.mockCall(
            HLConstants.ACCOUNT_MARGIN_SUMMARY_PRECOMPILE_ADDRESS,
            abi.encode(uint32(0), account),
            abi.encode(PrecompileLib.AccountMarginSummary({accountValue: 1000e6, marginUsed: 10e6, ntlPos: 32e6, rawUsd: 0}))
        );
    }

    function _count(Vm.Log[] memory logs) internal pure returns (uint256 n) {
        (, uint24[] memory kind,) = _actions(logs);
        n = kind.length;
    }

    /// A challenge on sixteen capped assets, every one of the thirty-two open, and the extra list.
    function _worst() internal returns (ChallengeAccount ch, uint32[] memory rulesAssets, uint32[] memory extra) {
        _register(2 * N);
        rulesAssets = new uint32[](N);
        extra = new uint32[](N);
        uint32[] memory caps = new uint32[](N);
        for (uint256 i = 0; i < N; ++i) {
            rulesAssets[i] = _asset(i);
            extra[i] = _asset(N + i);
            caps[i] = 500;
        }
        string[] memory names = _names(rulesAssets);
        vm.prank(operator);
        factory.setPlatformAssets(rulesAssets, names, true);
        Rules memory r = Rules({dailyLossBps: 500, maxDrawdownBps: 1000, maxLeverageX100: 500, assets: rulesAssets,
                                assetLeverageX100: caps});
        vm.prank(investor);
        Pool p = Pool(factory.createPool(r, _terms()));
        CoreSimulatorLib.forceSpotBalance(address(p), 0, 1000e8);
        p.prepareAccount();
        ch = _started(p);
        _open(address(ch), 2 * N);
    }

    function _thirtyTwoCancels() internal pure returns (Cancel[] memory cancels) {
        cancels = new Cancel[](32);
        for (uint256 i = 0; i < 32; ++i) {
            cancels[i] = Cancel({asset: _asset(i % N), oid: uint64(i + 1)});
        }
    }

    function test_theWorstStop_andTheProbeMakeTheSameActions() public {
        (ChallengeAccount ch, uint32[] memory rulesAssets, uint32[] memory extra) = _worst();
        assertEq(uint8(ch.violation(extra)), uint8(Breach.ForbiddenAsset), "every cap read, then an extra one");
        Cancel[] memory cancels = _thirtyTwoCancels();
        vm.recordLogs();
        uint256 g = gasleft();
        ch.breach(cancels, extra, SALT);
        uint256 stop = g - gasleft();
        uint256 stopActions = _count(vm.getRecordedLogs());

        StopGasProbe probe = new StopGasProbe();
        _open(address(probe), 2 * N);
        vm.recordLogs();
        uint256 probeTotal = probe.run(address(probe), 0, rulesAssets, extra, 32).total;
        uint256 probeActions = _count(vm.getRecordedLogs());

        assertEq(stopActions, 1 + 32 + 2 * N, "the agent's cut, the cancels and a closing order for each position");
        assertEq(probeActions, stopActions, "the probe sends what the stop sends");
        console2.log("worst stop on the simulator, gas", stop);
        console2.log("the probe on the simulator, gas", probeTotal);
        console2.log("the stop's own work, gas", stop - probeTotal);
        assertGt(stop, probeTotal);
    }
}
