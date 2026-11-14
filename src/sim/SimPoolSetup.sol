// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Rules} from "../Types.sol";
import {ExamTerms} from "../exam/PaperExam.sol";
import {SimSize, SimPlan} from "./SimPool.sol";

/// @title SimPoolSetup
/// @notice Copies a simulated pool's plan into the pool's storage when the pool is made: its menu, and the rules and
///         terms of its exam and its stage. A library with an external function runs in the pool's own storage
///         (DELEGATECALL). Moved out of `SimPool` only for the size of its code (EIP-170); what it does is what the
///         pool's `initialize` did.
library SimPoolSetup {
    /// Copies the plan's menu and stages; the largest size of the menu.
    function copy(
        SimPlan calldata p,
        SimSize[] storage menu,
        Rules storage examRules,
        ExamTerms storage exam,
        Rules storage fundedRules,
        ExamTerms storage funded
    ) external returns (uint64 largest) {
        for (uint256 i = 0; i < p.menu.length; ++i) {
            menu.push(p.menu[i]);
            if (p.menu[i].size > largest) largest = p.menu[i].size;
        }
        _copyRules(examRules, p.examRules);
        _copyRules(fundedRules, p.fundedRules);
        // Field by field, as the pool's storage holds them; `capital` is copied and never read.
        _copyTerms(exam, p.exam);
        _copyTerms(funded, p.funded);
    }

    function _copyRules(Rules storage to, Rules calldata r) private {
        to.dailyLossBps = r.dailyLossBps;
        to.maxDrawdownBps = r.maxDrawdownBps;
        to.maxLeverageX100 = r.maxLeverageX100;
        to.assets = r.assets;
        to.assetLeverageX100 = r.assetLeverageX100;
    }

    function _copyTerms(ExamTerms storage to, ExamTerms calldata t) private {
        to.capital = t.capital;
        to.targetsBps = t.targetsBps;
        to.stepDuration = t.stepDuration;
        to.maxIdle = t.maxIdle;
        to.minTradingDays = t.minTradingDays;
        to.costPpm = t.costPpm;
        to.fillDelay = t.fillDelay;
        to.fillWindow = t.fillWindow;
    }
}
