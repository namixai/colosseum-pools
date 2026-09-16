#!/usr/bin/env python3
"""Mutation check for the contract tests.

Each mutation breaks one guarantee in `src/`. The run applies it, runs `forge test`, and
requires that every named test goes red (other red tests are listed too). It then
restores the file byte for byte.

    python3 scripts/mutations.py            # all (M: contracts, G: gateway, A: agent client, J: app)
    python3 scripts/mutations.py M5 G2      # some

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
     "if (dayStartEquity > 0) {",
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
     "CoreWriterLib.placeLimitOrder(perp, isBuy, px, size1e8, true, Units.TIF_IOC, 0);",
     "CoreWriterLib.placeLimitOrder(perp, isBuy, px, size1e8, false, Units.TIF_IOC, 0);",
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
     "        if (payoutOwed != 0 && !payoutDone) {\n            if (spot == 0) return;",
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
    ("M19", "src/RuledAccount.sol",
     "        _cancelAll(cancels);\n        open = _closeAll(extra);",
     "        open = _closeAll(extra);",
     ["test_settle_cancelsNamedOrdersEveryCall"]),
    ("M20", "src/lib/CoreOps.sol",
     "            if (!exists(candidate)) return candidate;\n        }\n    }",
     "            if (!exists(candidate)) return candidate;\n        }\n        revert(\"exhausted\");\n    }",
     ["test_breach_goesThroughEvenIfEveryCandidateWasFunded"]),
    ("M21", "src/ChallengeAccount.sol",
     "if (capitalArrived()) revert CapitalArrived();",
     "",
     ["test_abort_refusedOnceTheCapitalArrived"]),
    ("M22", "src/ChallengeAccount.sol",
     "            return; // the payout hasn't landed yet",
     "",
     ["test_payoutIsSentOnce"]),
    ("M23", "src/ChallengeAccount.sol",
     "            payoutDone = true;\n",
     "",
     ["test_payoutIsSentOnce"]),
    ("M24", "src/RuledAccount.sol",
     "if (block.timestamp % 1 days >= CHECKPOINT_WINDOW) revert OutsideCheckpointWindow();",
     "",
     ["test_checkpoint_onlyJustAfterMidnight"]),
    ("M25", "src/RuledAccount.sol",
     "        if (dayStartEquity > 0) {\n            if (eq * bps < int256(dayStartEquity)",
     "        if (dayStartEquity > 0 && day == _today()) {\n            if (eq * bps < int256(dayStartEquity)",
     ["test_dailyLoss_carriesTheLastSnapshot"]),
    ("M26", "src/RuledAccount.sol",
     "if (!isStopped()) revert NotStopped();",
     "",
     ["test_breach_goesThroughEvenIfEveryCandidateWasFunded"]),
    ("M27", "src/lib/CoreOps.sol",
     "if (szDecimals > 6) revert UnsupportedSizeDecimals(perp, szDecimals);",
     "",
     ["test_close_refusesAnAssetWithTooManySizeDecimals"]),
    ("M28", "src/Pool.sol",
     "            return; // the payout hasn't landed yet",
     "",
     ["test_poolStaysClosingUntilTheFundedPayoutLands"]),
    # ── gateway (Python unittest) ──
    ("G1", "gateway/server.py",
     "if recovered.lower() != cleared.key.lower():",
     "if False:",
     ["test_wrong_signing_key_is_never_submitted"]),
    ("G2", "gateway/checks.py",
     "if not reader.is_bound(key, req.account, trader):",
     "if False:",
     ["test_someone_elses_signature", "test_changed_field_is_not_the_traders",
      "test_order_signature_is_not_a_cancel_signature"]),
    ("G3", "gateway/checks.py",
     "if not nonces.claim(trader, req.nonce):",
     "if False:",
     ["test_replay"]),
    ("G4", "gateway/checks.py",
     "if not now_ms < req.expires_at <= now_ms + MAX_EXPIRY_MS:",
     "if False:",
     ["test_expiry_window"]),
    ("G5", "gateway/checks.py",
     "if req.asset not in reader.allowed_assets(req.account):",
     "if False:",
     ["test_asset_outside_the_rules"]),
    ("G6", "gateway/checks.py",
     "if set(fields) != expected:",
     "if False:",
     ["test_shape"]),
    ("G7", "gateway/hl.py",
     "return action_hash(action, None, nonce, None)",
     "return action_hash(action, None, nonce + 1, None)",
     ["test_action_hash_matches_the_sdk_production_vector"]),
    ("G8", "gateway/checks.py",
     'DECIMAL = re.compile(r"^(0|[1-9][0-9]{0,15})(\\.[0-9]{0,9}[1-9])?$")',
     'DECIMAL = re.compile(r"^[0-9.]+$")',
     ["test_shape"]),
    ("G10", "app/lib/gateway.js",
     '    { name: "limitPx", type: "string" },\n    { name: "size", type: "string" },',
     '    { name: "size", type: "string" },\n    { name: "limitPx", type: "string" },',
     ["test_types_and_domain_match"]),
    ("G9", "gateway/checks.py",
     '"a": m["asset"], "b": m["isBuy"], "p": m["limitPx"], "s": m["size"],\n                    "r": m["reduceOnly"],',
     '"a": m["asset"], "b": m["isBuy"], "s": m["size"], "p": m["limitPx"],\n                    "r": m["reduceOnly"],',
     ["test_built_action_is_the_sdk_vector"]),
    # ── agent client (Python unittest) ──
    ("A1", "agents/client.py",
     "nonce = max(int(time.time() * 1000), self._last_nonce + 1)",
     "nonce = int(time.time() * 1000)",
     ["test_nonces_never_repeat_within_a_millisecond"]),
    ("A2", "agents/client.py",
     "size = int(sz * factor + 1e-9) / factor",
     "size = round(sz * factor) / factor",
     ["test_sizes"]),
    ("A3", "agents/client.py",
     "return str(math.floor(px + 0.5))",
     "return str(round(px))",
     ["test_prices"]),
    ("A4", "agents/client.py",
     '"reduceOnly": reduce_only, "tif": tif,',
     '"reduceOnly": False, "tif": tif,',
     ["test_reduce_only_reaches_the_action"]),
    ("A5", "agents/client.py",
     '"signature": auth.sign(self.wallet, kind, fields)}',
     '"signature": auth.sign(self.wallet, "order", fields)}',
     ["test_cancel_clears_every_gateway_check"]),
    # ── app (node --test) ──
    ("J1", "app/lib/cbor.js",
     '    if (++depth > MAX_DEPTH) throw new Error("CBOR nesting too deep");',
     "    ++depth;",
     ["cbor: hostile input fails fast"]),
    ("J2", "app/lib/cbor.js",
     "  function itemAt() {\n    need(1);",
     "  function itemAt() {",
     ["cbor: hostile input fails fast"]),
    ("J3", "app/lib/cbor.js",
     "  const atBreak = () => {\n    need(1);\n",
     "  const atBreak = () => {\n",
     ["cbor: hostile input fails fast"]),
    ("J4", "app/lib/cbor.js",
     'throw new Error(i > bytes.length ? "CBOR input ends early" : "trailing bytes after the CBOR item");',
     'throw new Error("trailing bytes after the CBOR item");',
     ["cbor: hostile input fails fast"]),
    ("J5", "app/lib/cbor.js",
     "if (info === 24) { need(1); return bytes[i++]; }",
     "if (info === 24) { return bytes[i++]; }",
     ["cbor: hostile input fails fast"]),
    ("J6", "app/lib/cbor.js",
     "if (info === 25) { need(2); const v",
     "if (info === 25) { const v",
     ["cbor: hostile input fails fast"]),
    ("J7", "app/lib/gateway.js",
     'if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {',
     "if (false) {",
     ["the gateway is reached over https unless it runs on this machine"]),
]

RUNNERS = {
    "M": (["forge", "test"], r"^\[FAIL.*\]\s+(\w+)\("),
    "G": (["spike/.venv/bin/python", "-m", "unittest", "discover", "-s", "gateway/tests", "-t", "."],
          r"^(?:FAIL|ERROR): (\w+) \("),
    "A": (["spike/.venv/bin/python", "-m", "unittest", "discover", "-s", "agents/tests", "-t", "."],
          r"^(?:FAIL|ERROR): (\w+) \("),
    "J": (["node", "--test", "--test-reporter=tap", "app/tests/"], r"^\s*not ok \d+ - (.+?)\s*$"),
}

# A mutation can turn a bounded loop into an endless one; that has to end the run, not hang it.
RUN_TIMEOUT_S = 900


def failing_tests(output: str, pattern: str) -> set[str]:
    # Forge: a failure message can contain brackets of its own ("[1.08e8]"), so the pattern
    # takes the last "]" on the line; the test name follows it.
    return set(re.findall(pattern, output, re.M))


def run(selected: list[str]) -> int:
    problems = 0
    for mid, rel, old, new, expected in sorted(MUTATIONS, key=lambda m: (m[0][0] != "M", int(m[0][1:]))):
        cmd, pattern = RUNNERS[mid[0]]
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
            try:
                proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                print(f"{mid}: TIMED OUT after {RUN_TIMEOUT_S}s")
                problems += 1
                continue
            out = proc.stdout + proc.stderr
            if "Compiler run failed" in out or "Error (" in out or "SyntaxError" in out:
                print(f"{mid}: DID NOT COMPILE")
                problems += 1
                continue
            red = failing_tests(out, pattern)
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
