#!/usr/bin/env python3
"""Mutation check for the contract tests.

Each mutation breaks one guarantee in `src/`. The run applies it, runs `forge test`, and
requires that every named test goes red (other red tests are listed too). It then
restores the file byte for byte.

    python3 scripts/mutations.py            # all mutations
    python3 scripts/mutations.py M5 M9      # some

Exit 0 only if every mutation applied exactly once and turned every named test red. A
mutation whose text is not found is a failure of this harness, not a pass: otherwise a
refactor would quietly turn it into "survived" or "killed" without anything being tested.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

MUTATIONS = [
    ("M1", "src/RuledAccount.sol",
     "if (eq * bps < base * (bps - int256(uint256(_rules.maxDrawdownBps)))) return Breach.Drawdown;",
     "if (eq * bps <= base * (bps - int256(uint256(_rules.maxDrawdownBps)))) return Breach.Drawdown;",
     ["test_drawdown_boundary"]),
    ("M2", "src/RuledAccount.sol",
     "if (day == _today() && dayStartEquity > 0) {",
     "if (false) {",
     ["test_dailyLoss_boundary", "test_dailyLoss_countsFromTheDaysSnapshot"]),
    ("M3", "src/RuledAccount.sol",
     "if (uint256(m.ntlPos) * 100 > uint256(eq) * _rules.maxLeverageX100) return Breach.Leverage;",
     "if (uint256(m.ntlPos) * 100 >= uint256(eq) * _rules.maxLeverageX100) return Breach.Leverage;",
     ["test_leverage_boundary"]),
    ("M4", "src/RuledAccount.sol",
     "if (!_allowedAsset[a] && CoreOps.positionSize(address(this), a) != 0) return Breach.ForbiddenAsset;",
     "if (CoreOps.positionSize(address(this), a) != 0) return Breach.ForbiddenAsset;",
     ["test_forbiddenAsset_onlyWhenNamed"]),
    ("M5", "src/RuledAccount.sol",
     "if (old != address(0)) factory.registry().retire(old);",
     "",
     ["test_breach_cutsAgent_cancels_closes_thenSettles", "test_fundedBreach_closesAndReturnsToIdle"]),
    ("M6", "src/RuledAccount.sol",
     "CoreOps.setAgent(keyless);",
     "CoreOps.setAgent(old);",
     ["test_breach_cutsAgent_cancels_closes_thenSettles", "test_breach_skipsAKeylessAddressSomeoneActivated"]),
    ("M7", "src/lib/CoreOps.sol",
     "                true,\n                Units.TIF_IOC,",
     "                false,\n                Units.TIF_IOC,",
     ["test_breach_cutsAgent_cancels_closes_thenSettles"]),
    ("M8", "src/lib/CoreOps.sol",
     "if (!up || down == px) return down;",
     "return down;",
     ["test_btc_integerPartOfFiveDigits_dropsTheDecimal", "test_eth_fourIntegerDigits_keepsFiveSignificant",
      "test_roundingUpCanCarryIntoANewDigit", "test_largeIntegerPart_dropsAllDecimals"]),
    ("M9", "src/ChallengeAccount.sol",
     "if (CoreOps.margin(address(this)).ntlPos != 0) revert NotFlat();",
     "",
     ["test_graduate_needsTargetAndFlat"]),
    ("M10", "src/Pool.sol",
     "address key = factory.registry().assign(trader);",
     "address key = trader; // not a fresh key from the registry",
     ["test_graduate_fundsTraderWithANewKey_andPaysTheShare"]),
    ("M11", "src/KeyRegistry.sol",
     "if (!accounts.isAccount(msg.sender)) revert NotAnAccount(msg.sender);",
     "",
     ["test_assign_onlyAccounts_oneKeyEach"]),
    ("M12", "src/KeyRegistry.sol",
     "if (CoreOps.exists(key)) revert KeyExistsOnCore(key);",
     "",
     ["test_publish_refusesZeroDuplicateAndCoreUsers"]),
    ("M13", "src/ChallengeAccount.sol",
     "        if (owed != 0) {\n            if (spot == 0) return;",
     "        if (false) {\n            if (spot == 0) return;",
     ["test_graduate_fundsTraderWithANewKey_andPaysTheShare"]),
    ("M14", "src/Pool.sol",
     "uint64 needed = (_terms.capital + _terms.fundedCapital) * Units.SPOT_PER_PERP;",
     "uint64 needed = _terms.capital * Units.SPOT_PER_PERP;",
     ["test_buyChallenge_needsCapitalForChallengeAndFunding"]),
    ("M15", "src/lib/CoreOps.sol",
     "if (!exists(candidate)) return candidate;",
     "return candidate;",
     ["test_breach_skipsAKeylessAddressSomeoneActivated"]),
    ("M16", "src/Pool.sol",
     "if (challenge != address(0)) revert BadStage(stage);",
     "",
     ["test_noNewChallengeWhileThePassedOneSettles"]),
    ("M17", "src/ChallengeAccount.sol",
     "if (block.timestamp <= createdAt + START_WINDOW) revert TooEarly();",
     "",
     ["test_abort_refundsTheTrader"]),
    ("M18", "src/Pool.sol",
     "        earned += heldPrice;\n        heldPrice = 0;",
     "        heldPrice = 0;",
     ["test_activate_waitsForCapital_thenStarts"]),
]


def failing_tests(output: str) -> set[str]:
    # A failure message can contain brackets of its own ("[1.08e8]"), so take the last
    # "]" on the line: the test name follows it.
    return set(re.findall(r"^\[FAIL.*\]\s+(\w+)\(", output, re.M))


def run(selected: list[str]) -> int:
    problems = 0
    for mid, rel, old, new, expected in sorted(MUTATIONS, key=lambda m: int(m[0][1:])):
        if selected and mid not in selected:
            continue
        path = ROOT / rel
        original = path.read_bytes()
        text = original.decode()
        count = text.count(old)
        if count != 1:
            print(f"{mid}: NOT APPLIED ({count} matches in {rel})")
            problems += 1
            continue
        path.write_text(text.replace(old, new), encoding="utf-8")
        try:
            proc = subprocess.run(["forge", "test"], cwd=ROOT, capture_output=True, text=True)
            out = proc.stdout + proc.stderr
            if "Compiler run failed" in out or "Error (" in out:
                print(f"{mid}: DID NOT COMPILE")
                problems += 1
                continue
            red = failing_tests(out)
            missing = [t for t in expected if t not in red]
            if missing:
                print(f"{mid}: SURVIVED in {missing} (red: {sorted(red)})")
                problems += 1
            else:
                print(f"{mid}: killed by {expected}" + (f" (also red: {sorted(red - set(expected))})" if red - set(expected) else ""))
        finally:
            path.write_bytes(original)
    if problems:
        print(f"mutations: {problems} problem(s)")
        return 1
    print("mutations: every mutation applied and was caught")
    return 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
