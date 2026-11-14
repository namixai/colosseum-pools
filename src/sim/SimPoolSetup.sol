// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Rules} from "../Types.sol";
import {ExamTerms} from "../exam/PaperExam.sol";
import {SimSize, SimTier, SimLineRules, SimPlan} from "./SimPool.sol";

/// @title SimPoolSetup
/// @notice Copies a simulated pool's plan into the pool's storage when the pool is made: its menu, each line's tier,
///         and the rules and terms each line's exam and stage start on. A library with an external function runs in
///         the pool's own storage (DELEGATECALL). Moved out of `SimPool` only for the size of its code (EIP-170).
library SimPoolSetup {
    /// Copies the plan's menu and tiers, and for each line the plan's rules and terms with the line's tier in them:
    /// its loss limit in `maxDrawdownBps`, its target in every step of `targetsBps`, its kind of loss line. Returns
    /// the largest size of the menu.
    function copy(
        SimPlan calldata p,
        SimSize[] storage menu,
        SimTier[] storage tiers,
        mapping(uint256 => SimLineRules) storage lineRules
    ) external returns (uint64 largest) {
        for (uint256 i = 0; i < p.menu.length; ++i) {
            menu.push(p.menu[i]);
            if (p.menu[i].size > largest) largest = p.menu[i].size;
            SimTier calldata k = p.tiers[i];
            tiers.push(k);
            SimLineRules storage l = lineRules[i];
            _copyRules(l.examRules, p.examRules, k.maxDrawdownBps);
            _copyRules(l.fundedRules, p.fundedRules, k.maxDrawdownBps);
            // Field by field, as the pool's storage holds them; `capital` is copied and never read.
            _copyTerms(l.exam, p.exam, k.targetBps);
            _copyTerms(l.funded, p.funded, k.targetBps);
            l.drawdownKind = k.drawdownKind;
        }
    }

    function _copyRules(Rules storage to, Rules calldata r, uint16 maxDrawdownBps) private {
        to.dailyLossBps = r.dailyLossBps;
        to.maxDrawdownBps = maxDrawdownBps;
        to.maxLeverageX100 = r.maxLeverageX100;
        to.assets = r.assets;
        to.assetLeverageX100 = r.assetLeverageX100;
    }

    function _copyTerms(ExamTerms storage to, ExamTerms calldata t, uint16 targetBps) private {
        to.capital = t.capital;
        for (uint256 j = 0; j < t.targetsBps.length; ++j) {
            to.targetsBps.push(targetBps);
        }
        to.stepDuration = t.stepDuration;
        to.maxIdle = t.maxIdle;
        to.minTradingDays = t.minTradingDays;
        to.costPpm = t.costPpm;
        to.fillDelay = t.fillDelay;
        to.fillWindow = t.fillWindow;
    }
}
