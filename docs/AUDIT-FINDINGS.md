# Audit findings and the tests that pin them

An internal audit went through these contracts between 25 and 27 September 2026. This file is the
map from each finding to the test that holds it down, and `scripts/findings-check.py` reads it: a
test named here that does not exist in the repository fails the check, so the claim cannot rot
quietly into a sentence nobody verifies.

Three findings have no test, and each says why on its own line. That is the part worth reading:
a row with no test is either a limit of the harness or a limit of the design, and neither is
improved by a test that passes for the wrong reason.

Status is what the repository does, not what anyone intends to do next.

| # | Severity | What it was | Status | Pinned by |
|---|---|---|---|---|
| A-01 | High | A stranger could hold a settlement open for the price of gas — spot dust on a challenge, perp dust on either — and with it the investor's capital, which only leaves in Idle | Closed | `test_settle_isNotHeldOpenByAStrangersDust`, `test_settle_stillWaitsWhileItsOwnReturnHasNotLanded`, `test_settle_spotDust_whenTheOwnReturnIsTiny_noLongerHolds`, `test_settle_challengePerpDust_noLongerHolds`, `test_settleFunded_isNotHeldOpenByAStrangersPerpDust`, `test_settleFunded_stillWaitsForItsOwnProceedsToReachSpot`, `test_sweep_sendsWhatArrivesAfterTheEndBackToThePool` |
| A-02 | High | `graduate` took a key from the registry, so anyone who drained the free list decided whether a trader who had already met the target got their funded stage | Closed | `test_graduate_recordsThePassWithTheRegistryDrained_andTheStageOpensLater`, `test_aSpoiledReservedKeyIsSwappedForALiveOne`, `test_aChallengeNobodyPassesGivesTheReservedKeyBack`, `test_abandonFundedStage_onlyAfterTheWindow_andThenAnyoneMay`, `test_abandonFundedStage_isRefusedWhileAKeyIsThereToBeHad`, `test_a_pool_waiting_for_a_key_gets_its_funded_stage_opened` |
| A-03 | High | The gateway and the keeper shared one node and one per-IP budget, so an outsider's refusable orders could spend the budget the keeper needed to see a breach | Closed on the host, 25 Sep 2026 | No contract test: this is configuration, not code. What was checked on the host, and how, is in `docs/HOSTING.md` |
| A-04 | Medium | A trader's share was paid once nothing was withdrawable — and a resting order keeps its margin out of `withdrawable`, so the share came out of whatever had reached spot, once and for good | Closed | `test_settle_doesNotPayTheShareWhileAnOrderHoldsMargin`, `test_settleFunded_doesNotPayTheShareWhileAnOrderHoldsMargin`, `test_settle_doesNotPayTheShareInTheBlockTheMarginIsReleased`, `test_settleFunded_doesNotPayTheShareInTheBlockTheMarginIsReleased` |
| A-05 | Medium | The target is measured from the account's equity, which rises for any USDC sent in, so a pass can be bought without trading | Open by design | `test_theTargetCanBeReachedByDepositing_notOnlyByTrading` — the test buys a pass. HyperCore offers no way to tell a deposit from a gain, so this is written into `docs/DESIGN.md` under what the design does not do |
| A-06 | Medium | The shared pool's operator could add a seat after people had deposited, with any rules, while the document promised seats are published beforehand | Closed | `test_addSeat_isRefusedOnceAnyoneHasDeposited` |
| A-07 | Low | The seventeenth withdrawal request is refused, and both the contract comment and the document said it "waits for the queue to move" | Closed as a documentation defect | No behaviour test, because no behaviour changed: the cap was always 16 and still is. Both sentences now say refused. Building a queue of sixteen holders to watch the seventeenth bounce would test the constant, not the fix |
| A-08 | Low | `buyChallenge` checks the pool's balance through a precompile, which answers with the start of the block, so a withdrawal earlier in the same block is invisible to it | Closed | `test_buyChallenge_refusesInTheBlockTheOwnerWithdrew` — the sale is refused inside the withdrawal's own block. Pinning the OLD behaviour was the thing this harness could not do: it processes the queued transfer before the withdrawal, so the capital arrived and the silent failure never appeared. Refusing is a revert, which it shows fine |
| A-09 | Info | `docs/DESIGN.md` contradicted the code and itself: a price of zero, the fee as "the only brake", who pays for a spoiled challenge's account | Closed | No test, because nothing in the code was wrong — the document was. Four sentences corrected, including one that called a HyperCore behaviour untested after we had measured it |
| A-10 | Info | The list of "every call the keeper makes" included `graduate`, which the keeper has never called | Closed | No test: the keeper's own header already says why it leaves that call to the trader, and the sentence now matches it |
| A-11 | High | A regression of ours: a one-time flag let a second `settleFunded` in the same block walk past the wait and pay the funded share out of a stale spot balance | Closed | `test_regression_twoStepsInOneBlock_payTheFundedShareFromAStaleSpot` |

## Why a map and not copies of the audit's files

The audit wrote its own tests in its own clone. Copying them here would give two sets of tests for
the same findings, drifting apart from the day they landed, and a suite count that means one thing
in one place and something else in another — which already happened once when a regression test was
given its own file and re-ran fifty-two tests under a second contract name.

So the tests live where the harness lives and this file points at them by name. `findings-check.py`
resolves every name, which is the part copies could not do: a renamed or deleted test breaks the
check instead of quietly leaving a row here pointing at nothing.
