// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.28;

import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";
import {Rules, Units} from "../Types.sol";
import {PaperExam, ExamTerms} from "../exam/PaperExam.sol";
import {SimSize, SimTier, SimLine, SimRules, SimPlan, COVER_PAYOUTS} from "./SimPool.sol";
import {SimPoolFactory} from "./SimPoolFactory.sol";

/// @title SimPoolSetup
/// @notice Copies a simulated pool's plan into the pool's storage when the pool is made, and starts each exam and
///         stage on it. The plan's rules and terms are kept once, the same for every line; a line keeps only its
///         size, price and tier, in one storage slot (`SimLine`), and the tier is put into the rules and terms when
///         an exam or a stage starts. So the cost of opening a pool grows by one word a line, not by two copies of
///         every rule. A library with
///         external functions runs in the pool's own storage (DELEGATECALL). Moved out of `SimPool` only for the
///         size of its code (EIP-170).
library SimPoolSetup {
    /// Copies the plan's menu and tiers, and once the rules and terms both stages start on. Returns the largest
    /// size of the menu.
    function copy(SimPlan calldata p, SimLine[] storage lines, SimRules storage rules)
        external
        returns (uint64 largest)
    {
        for (uint256 i = 0; i < p.menu.length; ++i) {
            SimSize calldata m = p.menu[i];
            SimTier calldata k = p.tiers[i];
            lines.push(SimLine(m.size, m.price, k.maxDrawdownBps, k.targetBps, k.drawdownKind, 0));
            if (m.size > largest) largest = m.size;
        }
        _copyRules(rules.examRules, p.examRules);
        _copyRules(rules.fundedRules, p.fundedRules);
        _copyTerms(rules.exam, p.exam);
        _copyTerms(rules.funded, p.funded);
        rules.fromBook = p.fromBook;
        rules.maxWorseBps = p.maxWorseBps;
    }

    /// Starts an exam (`funded` false) or a stage for `trader` on line `k`, its size and tier: the kept rules and
    /// terms with the tier in them, its loss limit in `maxDrawdownBps`, its target in every step of `targetsBps`,
    /// its kind of loss line, and the line's size as `capital`. What each exam starts on is what the pool kept for
    /// its line before the rules were kept once (`SimPool` up to 16 November 2026). An exam tells the pool when it
    /// passes (`SimPool.examPassed`); a stage does not, its pass is claimed (`SimPool.claimPayout`).
    function start(
        SimRules storage rules,
        bool funded,
        SimLine storage k,
        address trader,
        SimPoolFactory factory
    ) external returns (address a) {
        Rules memory r = funded ? rules.fundedRules : rules.examRules;
        ExamTerms memory t = funded ? rules.funded : rules.exam;
        r.maxDrawdownBps = k.maxDrawdownBps;
        t.capital = k.size;
        for (uint256 j = 0; j < t.targetsBps.length; ++j) {
            t.targetsBps[j] = k.targetBps;
        }
        a = Clones.clone(factory.examImpl());
        PaperExam(a).initializeWithFiller(trader, r, t, factory.filler(), rules.fromBook, rules.maxWorseBps, k.drawdownKind, !funded);
        factory.noteAccount(a);
    }

    /// The largest size among the lines with an exam or a stage not yet done, and the lines of `on` (bit `i` for line
    /// `i`) with `COVER_PAYOUTS` of their payouts in `freeNow`, selling now: what `SimPool.floor` keeps three payouts
    /// of. Zero if none.
    function top(SimLine[] storage lines, uint32 on, uint256 freeNow, SimPoolFactory factory)
        external
        view
        returns (uint64 size)
    {
        uint256 n = lines.length;
        uint256 bps = factory.PAYOUT_BPS();
        for (uint256 i = 0; i < n; ++i) {
            SimLine storage l = lines[i];
            uint64 s = l.size;
            if (s <= size) continue;
            bool sells = on & (1 << i) != 0 && (uint256(s) * bps / Units.BPS) * COVER_PAYOUTS <= freeNow;
            if (l.live != 0 || sells) size = s;
        }
    }

    /// What `SimPool.menu()` returns, ABI-encoded (`tiers` false: each line's size and price), or `SimPool.tiers()`
    /// (each line's tier).
    function shown(SimLine[] storage lines, bool tiers) external view returns (bytes memory) {
        uint256 n = lines.length;
        if (tiers) {
            SimTier[] memory k = new SimTier[](n);
            for (uint256 i = 0; i < n; ++i) {
                SimLine storage l = lines[i];
                k[i] = SimTier(l.maxDrawdownBps, l.targetBps, l.drawdownKind);
            }
            return abi.encode(k);
        }
        SimSize[] memory m = new SimSize[](n);
        for (uint256 i = 0; i < n; ++i) {
            m[i] = SimSize(lines[i].size, lines[i].price);
        }
        return abi.encode(m);
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
