#!/usr/bin/env python3
"""Mutation check for the contract tests.

Each mutation breaks one guarantee in `src/`. The run applies it, runs `forge test`, and
requires that every named test goes red (other red tests are listed too). It then
restores the file byte for byte.

    python3 scripts/mutations.py            # all (M contracts, G gateway, A agent client, K keeper, J app)
    python3 scripts/mutations.py M5 G2      # some

Exit 0 only if every mutation applied exactly once and turned every named test red. A
mutation whose text is not found is a failure of this harness, not a pass: otherwise a
refactor would quietly turn it into "survived" or "killed" without anything being tested.
Each suite first runs unmutated, and a named test that is already red there fails the run:
a test that was red before the mutation proves nothing about it.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tempfile

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
     "if not nonces.claim(trader, req.nonce, req.expires_at, now_ms):",
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
    ("G11", "gateway/checks.py",
     "while self._by_expiry and self._by_expiry[0][0] <= now_ms:",
     "while False:",
     ["test_forgets_only_expired_entries", "test_is_bounded"]),
    ("G12", "gateway/checks.py",
     "if len(self._seen) >= self._limit:",
     "if False:",
     ["test_is_bounded"]),
    ("G13", "gateway/server.py",
     "timeout = request_timeout  # applies to every read and write on the connection",
     "timeout = None",
     ["test_a_silent_client_is_dropped"]),
    ("G14", "gateway/server.py",
     "if not self._slots.acquire(blocking=False):",
     "if False:",
     ["test_busy_server_answers_503_and_frees_the_slot"]),
    ("G15", "gateway/server.py",
     "        if signature is None:\n",
     "        if False:\n",
     ["test_malformed_enclave_signature_is_never_submitted"]),
    ("G16", "gateway/server.py",
     "except Exception as e:  # a chain read or the Signer failed; nothing was submitted",
     "except ZeroDivisionError as e:",
     ["test_upstream_failure_is_an_answer_not_a_crash"]),
    ("G17", "gateway/server.py",
     '"signer": signer_reason(signed.body),',
     '"signer": signed.body,',
     ["test_enclave_refusal_passes_on_only_the_reason"]),
    ("G18", "gateway/server.py",
     "            except ValueError:\n                length = -1",
     "            except ValueError:\n                raise",
     ["test_bad_content_length"]),
    ("G19", "gateway/server.py",
     "            venue = self.submit(action, req.nonce, signature)\n        except Exception as e:",
     "            venue = self.submit(action, req.nonce, signature)\n        except ZeroDivisionError as e:",
     ["test_unreachable_venue_is_reported_with_the_receipt"]),
    ("G20", "gateway/server.py",
     "    if type(v) is not int or v not in (27, 28):",
     "    if v not in (27, 28):",
     ["test_malformed_enclave_signature_is_never_submitted"]),
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
    ("A6", "agents/client.py",
     "    if parts.scheme != \"https\" and not local:",
     "    if False:",
     ["test_https_unless_the_gateway_runs_here"]),
    ("A7", "agents/ai_trader.py",
     "            if notional > self.limits.max_notional:",
     "            if False:",
     ["test_minimum_and_per_order_cap"]),
    ("A8", "agents/ai_trader.py",
     "            if open_notional + notional > ceiling:",
     "            if False:",
     ["test_headroom_under_the_leverage_rule"]),
    ("A9", "agents/ai_trader.py",
     "        if self.orders_left <= 0:\n            raise ToolError(\"no orders left in this session\")",
     "        if False:\n            raise ToolError(\"no orders left in this session\")",
     ["test_orders_per_session"]),
    ("A10", "agents/ai_trader.py",
     "        if coin not in self.perps:",
     "        if False:",
     ["test_only_perps_on_the_accounts_list"]),
    ("A11", "agents/ai_trader.py",
     "            if spent >= budget_usd:",
     "            if False:",
     ["test_budget_stops_before_the_turns_tools_run"]),
    ("A12", "agents/ai_trader.py",
     "        if not self.send:\n            return {\"status\": \"not_sent\", \"order\": order}",
     "        if False:\n            return {\"status\": \"not_sent\", \"order\": order}",
     ["test_no_orders_mode_sends_nothing"]),
    ("A13", "agents/ai_trader.py",
     "        if offer[\"price_usdc\"] > self.max_price:",
     "        if False:",
     ["test_buy_needs_the_listing_price_and_the_cap"]),
    ("A14", "agents/ai_trader.py",
     "        if self.purchases_left <= 0:",
     "        if False:",
     ["test_one_purchase_approving_the_exact_price"]),
    ("A15", "agents/ai_trader.py",
     "    if fallbacks and model in FALLBACK_MODELS:",
     "    if False:",
     ["test_opus_asks_for_adaptive_thinking_and_default_fallbacks"]),
    ("A16", "agents/ai_trader.py",
     "        if notional < MIN_ORDER_USDC:",
     "        if False:",
     ["test_minimum_and_per_order_cap"]),
    ("A17", "agents/ai_trader.py",
     "        if message.stop_reason == \"refusal\":",
     "        if False:",
     ["test_refusal_and_truncation_run_no_tools"]),
    ("A18", "agents/ai_trader.py",
     "            if spot < (capital + funded) * 100:",
     "            if False:",
     ["test_listing_shows_only_pools_that_can_sell_now"]),
    ("A19", "agents/ai_trader.py",
     "        if self.graduations_left <= 0:",
     "        if False:",
     ["test_graduation_once_with_a_readable_refusal"]),
    # ── keeper (Python unittest) ──
    ("K1", "ops/keeper.py",
     'if entry["address"].lower() != factory.lower() or entry["topics"][0] != CHALLENGE_CREATED:',
     'if entry["topics"][0] != CHALLENGE_CREATED:',
     ["test_an_rpc_that_ignores_the_address_filter_changes_nothing"]),
    ("K2", "ops/keeper.py",
     '"address": factory, "topics": [CHALLENGE_CREATED],',
     '"topics": [CHALLENGE_CREATED],',
     ["test_follows_pools_named_by_the_factorys_challenge_events_only"]),
    ("K3", "ops/keeper.py",
     'entry["topics"][2][-40:]',
     'entry["topics"][1][-40:]',
     ["test_follows_pools_named_by_the_factorys_challenge_events_only"]),
    ("K4", "ops/keeper.py",
     "return not (stage == IDLE and challenge == ZERO)",
     "return True",
     ["test_idle_pool_is_dropped_and_comes_back_with_its_next_challenge"]),
    ("K5", "ops/keeper.py",
     '            stop(wallet, ch, "breach", cancels, extra, dry)',
     "            pass",
     ["test_breach_names_open_orders_and_positions_outside_the_rules"]),
    ("K6", "ops/keeper.py",
     "STOPPED = (BREACHED, EXPIRED, FORFEITED, PASSED, ABORTED)",
     "STOPPED = (BREACHED, EXPIRED, FORFEITED, PASSED)",
     ["test_stopped_states_match_is_stopped", "test_aborted_is_one_of_the_stopped_states"]),
    ("K7", "ops/keeper.py",
     "hi = min(lo + window - 1, end)",
     "hi = end",
     ["test_logs_are_read_in_windows_and_the_state_resumes"]),
    ("K8", "ops/keeper.py",
     "return now % 86400 < CHECKPOINT_WINDOW",
     "return True",
     ["test_checkpoint_only_just_after_midnight_and_once_a_day"]),
    ("K9", "ops/keeper.py",
     "CREATED, ACTIVE, BREACHED, EXPIRED, FORFEITED, PASSED, ABORTED, SETTLED = range(1, 9)",
     "CREATED, ACTIVE, BREACHED, EXPIRED, FORFEITED, PASSED, ABORTED, SETTLED = range(0, 8)",
     ["test_status_and_stage_numbers"]),
    ("K10", "ops/keeper.py",
     "            self.next_block = end + 1",
     "            self.next_block = end",
     ["test_logs_are_read_in_windows_and_the_state_resumes"]),
    ("K11", "ops/keeper.py",
     'elif now > view(ch, "createdAt()", "uint64") + START_WINDOW:',
     "elif True:",
     ["test_aborts_only_after_the_start_window"]),
    ("K12", "ops/keeper.py",
     'if to_checksum_address(state["factory"]) != self.factory:',
     "if False:",
     ["test_state_of_another_factory_is_refused"]),
    ("K13", "ops/keeper.py",
     "names[p[\"position\"][\"coin\"]] not in allowed",
     "names[p[\"position\"][\"coin\"]] in allowed",
     ["test_breach_names_open_orders_and_positions_outside_the_rules"]),
    ("K14", "ops/keeper.py",
     '    if dry:\n        log("would_send"',
     '    if False:\n        log("would_send"',
     ["test_dry_run_sends_nothing"]),
    ("K15", "ops/keeper.py",
     "except Exception as exc:  # one failed call must not stop the pass",
     "except ZeroDivisionError as exc:",
     ["test_one_failed_call_does_not_stop_the_pass"]),
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
    "K": (["spike/.venv/bin/python", "-m", "unittest", "discover", "-s", "ops/tests", "-t", "."],
          r"^(?:FAIL|ERROR): (\w+) \("),
    "J": (["node", "--test", "--test-reporter=tap", "app/tests/"], r"^\s*not ok \d+ - (.+?)\s*$"),
}

# A mutation can turn a bounded loop into an endless one; that has to end the run, not hang it.
RUN_TIMEOUT_S = 900


def run_suite(cmd: list[str]) -> subprocess.CompletedProcess:
    """One test run with its own empty bytecode cache. Python trusts a cached .pyc when the
    source's size and modification second match, and a mutation of the same length restored
    within the same second would otherwise keep running as the mutant afterwards."""
    with tempfile.TemporaryDirectory(prefix="mutations-pyc-") as cache:
        env = {**os.environ, "PYTHONPYCACHEPREFIX": cache}
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=RUN_TIMEOUT_S, env=env)


def failing_tests(output: str, pattern: str) -> set[str]:
    # Forge: a failure message can contain brackets of its own ("[1.08e8]"), so the pattern
    # takes the last "]" on the line; the test name follows it.
    return set(re.findall(pattern, output, re.M))


def red_tests(cmd: list[str], pattern: str) -> set[str] | None:
    """Names of the red tests, or None if the suite did not run to the end."""
    try:
        proc = run_suite(cmd)
    except subprocess.TimeoutExpired:
        return None
    out = proc.stdout + proc.stderr
    if "Compiler run failed" in out or "Error (" in out or "SyntaxError" in out:
        return None
    return failing_tests(out, pattern)


def baseline(chosen) -> int:
    problems = 0
    for prefix in sorted({m[0][0] for m in chosen}):
        cmd, pattern = RUNNERS[prefix]
        red = red_tests(cmd, pattern)
        named = {t for m in chosen if m[0][0] == prefix for t in m[4]}
        if red is None:
            print(f"baseline {prefix}: the suite did not run")
            problems += 1
        elif red & named:
            print(f"baseline {prefix}: already red before any mutation: {sorted(red & named)}")
            problems += 1
    return problems


def run(selected: list[str]) -> int:
    unknown = sorted(set(selected) - {m[0] for m in MUTATIONS})
    if unknown:
        print(f"mutations: no such mutation: {', '.join(unknown)}")
        return 1
    chosen = [m for m in MUTATIONS if not selected or m[0] in selected]
    problems = baseline(chosen)
    if problems:
        print(f"mutations: {problems} problem(s) before mutating; nothing was mutated")
        return 1
    for mid, rel, old, new, expected in sorted(chosen, key=lambda m: (m[0][0] != "M", int(m[0][1:]))):
        cmd, pattern = RUNNERS[mid[0]]
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
                proc = run_suite(cmd)
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
