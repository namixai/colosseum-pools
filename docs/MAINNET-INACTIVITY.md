# An inactivity rule, for mainnet

**Status: a specification, not a thing that exists.** Nothing in the contracts implements this, and
nothing here has been built or measured. It is written so the decisions can be made before the code
is, and it is deliberately one page. Alex asked for the rule on 3 October 2026 — thirty days, the
way the market has it. Everything below is about how to get there from what the chain already
holds.

## The one hard fact: the chain cannot see trading

It sees two things about an account, through the HyperCore precompiles: its **equity**, and whether
it **holds a position**. It never sees an order, a cancel or a fill — those go to the venue through
the gateway, which is our software and not a source a contract can read. So a rule phrased "the
trader did not trade for thirty days" is not checkable on chain as written, and any implementation
is a proxy for it. Saying which proxy, and where it is blind, is most of this page.

## What is already there to build on

- `day` and `dayStartEquity` (`src/RuledAccount.sol:40-41`), written by `checkpoint()` once per UTC
  day inside a **15-minute window** after midnight (`CHECKPOINT_WINDOW`), with a `DaySnapshot`
  event. The keeper calls it. One instant a day, not a record of the day.
- `violation(uint32[])` reads the margin summary now, including `ntlPos`, so "holds a position" is
  answerable at any moment a transaction asks.
- Two precedents for ending an account on **time**, both callable by anyone: `expire` on a challenge
  past its `deadline` (`ChallengeAccount.sol:202-208`), and `abandonFundedStage` after
  `AWAIT_KEY_WINDOW` of 7 days (`Pool.sol:94`). Neither needs the owner, which is the property that
  matters: a rule only the counterparty can invoke is a rule the counterparty can decline.

## The proxy, and the trap in it

**Definition.** An account is inactive when, over thirty days, it was never seen holding a position
and its equity never moved.

The obvious implementation — count daily snapshots that look idle — **must not be used**, and the
reason is our own finding A-14. Snapshots are written by a keeper, and a keeper can fail: on 25
September one followed zero pools for the life of a deployment while reporting success. If the rule
counts snapshots, a keeper outage manufactures an inactivity end against a trader who was trading.
That is the worst failure this rule can have.

So: **a missing observation must prevent the end, never cause it.** Concretely, the account keeps a
mark — the last time it was *seen* active — and the end requires both that the mark is thirty days
old and that the days in between were actually observed:

- `lastSeenActive`: a timestamp written by `checkpoint()` whenever that call finds a position open
  or an equity different from the previous snapshot. Written at the funded start too, so a stage
  that never trades still has a beginning to count from.
- `daysObserved`: how many checkpoints landed since the mark. The end requires at least, say, 28 of
  the 30 — a figure to pick, not one to assume — so two missed days are tolerated and a dead keeper
  blocks the rule instead of firing it.

**Where this is blind, stated plainly.** A snapshot is one instant. A trader who opens a position
after the window and closes it before the next one reads as idle at every snapshot, and a trader
doing that for thirty days would be ended by this rule although they traded every day. Equity
catches most of it — fees move equity, and a round trip pays two — so "equity never moved" is the
part doing the real work, and a trader would have to end thirty consecutive days at exactly the same
equity to hide. It is not impossible; it is implausible and cheap to detect off chain. If that is
not good enough, the alternative is to have the **gateway** write an activity mark on chain, which
makes our software a party to the rule and is a worse trade.

## Who calls it

Anyone, like `expire`. The investor has the motive — their capital is idle — and the trader has
none, so a permissioned version would simply never be called on the one account that needs it. The
keeper should call it as part of its pass, because that is the actor already awake, but it must not
be the only one who can.

It refuses while a position is open, as the stops do: an account is not ended with margin held.
The same drain-and-settle path every other ending uses applies here; nothing new is needed for the
money to come home.

## What goes back, and to whom

**The challenge.** Capital returns to the pool, the price stays earned by the pool, the trader gets
nothing. This is `Expired` with a different name on the reason, and the existing path already does
it: a challenge that runs out of time with nothing broken ends `Status.Expired`, `Breach.None`. An
inactivity rule on a challenge is arguably redundant — the challenge has a deadline, and on the live
run that deadline is 90 days — so the rule earns its place on the **funded stage**, which has no
clock at all.

**The funded stage.** Capital returns to the pool. The trader's share is on profit, and an account
that did nothing has none, so in the ordinary case nothing is owed and the question looks academic.
It is not: a stage can go idle while **up**, and then the rule decides who keeps the gain.

🔴 **This is the decision, and it is Alex's, not a technical one.** Two shapes:

1. **A clean end that pays.** Reason recorded as `None`, so `fundedPayoutOwed` pays the trader their
   80% of what the stage actually earned (`Pool.sol:408-410` as it stands today), and the rest goes
   to the pool with the capital. Inactivity is housekeeping, not rule-breaking: the trader earned
   what they earned, and the rule's purpose is to free the investor's capital rather than to
   confiscate.
2. **An ending that voids the share.** A new reason, so the payout does not fire and the whole
   balance returns to the pool.

**Recommended: the first.** Beyond fairness, the second creates an incentive we should not want: if
going quiet voids the payout, the platform and the investor are better off when a *winning* trader
goes quiet, and nobody is obliged to remind them. A rule that pays what was earned has no such
pull. It also keeps `fundedEndReason` meaning what it means today — a rule was broken — rather than
turning it into a mixed bag of breaches and clerical endings, which is the distinction `EVIDENCE.md`
spends a section defending.

## What must be decided before any code

1. Pays or voids (above). Recommendation: pays.
2. Thirty days of what — the mark's age, with a minimum of observed days. The tolerance (28 of 30 is
   a guess) needs picking.
3. Whether the challenge gets the rule at all, given it already has a deadline.
4. Whether `lastSeenActive` is worth the storage on the challenge, which is cloned per purchase.

## What this page does not claim

It is not implemented, not tested and not measured. The blind spot in the snapshot proxy is argued,
not quantified: nobody has checked how far equity can drift from zero on a day of round trips at
mainnet fee levels, and that number would sharpen the "equity never moved" test from a judgement
into a threshold. Doing that is a measurement, and it belongs with the code rather than here.
