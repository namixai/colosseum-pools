// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Rules} from "../Types.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {SimSize, SimTier, SimRules, SimPlan} from "./SimPool.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";

/// @title SimPoolSetup
/// @notice Copies a simulated pool's plan into the pool's storage when the pool is made, and starts each exam and
///         stage on it. The plan's rules and terms are kept once, the same for every line; a line keeps only its
///         size, price and tier, and the tier is put into the rules and terms when an exam or a stage starts. So
///         the cost of opening a pool grows by two words a line, not by two copies of every rule. A library with
///         external functions runs in the pool's own storage (DELEGATECALL). Moved out of `SimPool` only for the
///         size of its code (EIP-170).
library SimPoolSetup {
    /// Copies the plan's menu and tiers, and once the rules and terms both stages start on. Returns the largest
    /// size of the menu.
    function copy(SimPlan calldata p, SimSize[] storage menu, SimTier[] storage tiers, SimRules storage rules)
        external
        returns (uint64 largest)
    {
        for (uint256 i = 0; i < p.menu.length; ++i) {
            menu.push(p.menu[i]);
            if (p.menu[i].size > largest) largest = p.menu[i].size;
            tiers.push(p.tiers[i]);
        }
        _copyRules(rules.examRules, p.examRules);
        _copyRules(rules.fundedRules, p.fundedRules);
        _copyTerms(rules.exam, p.exam);
        _copyTerms(rules.funded, p.funded);
        rules.fromBook = p.fromBook;
        rules.maxWorseBps = p.maxWorseBps;
    }

    /// Starts an exam (`funded` false) or a stage for `trader` on a line of `size` and tier `k`: the kept rules and
    /// terms with the tier in them, its loss limit in `maxDrawdownBps`, its target in every step of `targetsBps`,
    /// its kind of loss line, and the line's size as `capital`. What each exam starts on is what the pool kept for
    /// its line before the rules were kept once (`SimPool` up to 16 November 2026).
    function start(
        SimRules storage rules,
        bool funded,
        SimTier storage k,
        uint64 size,
        address trader,
        SimPoolFactory factory
    ) external returns (address a) {
        Rules memory r = funded ? rules.fundedRules : rules.examRules;
        ExamTerms memory t = funded ? rules.funded : rules.exam;
        r.maxDrawdownBps = k.maxDrawdownBps;
        t.capital = size;
        for (uint256 j = 0; j < t.targetsBps.length; ++j) {
            t.targetsBps[j] = k.targetBps;
        }
        a = Clones.clone(factory.examImpl());
        PaperExam(a).initializeWithFiller(trader, r, t, factory.filler(), rules.fromBook, rules.maxWorseBps, k.drawdownKind);
        factory.noteAccount(a);
    }

    function _copyRules(Rules storage to, Rules calldata r) private {
        to.dailyLossBps = r.dailyLossBps;
        to.maxLeverageX100 = r.maxLeverageX100;
        to.assets = r.assets;
        to.assetLeverageX100 = r.assetLeverageX100;
    }

    // Field by field, as the pool's storage holds them; neither `capital` nor the values of `targetsBps` are kept:
    // the line's size and tier take their place at the start, and only how many steps there are is read.
    function _copyTerms(ExamTerms storage to, ExamTerms calldata t) private {
        for (uint256 j = 0; j < t.targetsBps.length; ++j) {
            to.targetsBps.push();
        }
        to.stepDuration = t.stepDuration;
        to.maxIdle = t.maxIdle;
        to.minTradingDays = t.minTradingDays;
        to.costPpm = t.costPpm;
        to.fillDelay = t.fillDelay;
        to.fillWindow = t.fillWindow;
    }
}
