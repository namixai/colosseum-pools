# Design: what the creator of a pool sets

**Status: a design, built in part and deployed nowhere.** The exam's rows are built
([PAPER-EXAM.md](PAPER-EXAM.md)), and so is a pool of one owner that takes the menu, the share
in work, the funded account's settings and the term ([MULTI-POOL.md](MULTI-POOL.md)). A pool of
several investors is not written yet. The pools on the testnet are the ones in
[DESIGN.md](DESIGN.md) and [SHARED-POOL.md](SHARED-POOL.md), with their terms fixed the way
those documents say.

The idea is one rule for every choice below: the platform does not pick the terms of a pool. The
address that creates the pool sets them once, at creation, they are public before anyone
deposits or buys, and they do not change afterwards. A different set of terms is a different
pool.

A default is what the creation form starts with; the creator changes it. The contracts take
every value explicitly. Where the column says "none", there is no value to start from: it has
to be chosen.

## The pool

| Setting | Type | Bounds | Default | Built |
|---|---|---|---|---|
| Investors | one, or several | — | none; it is the first thing the creator chooses | one: yes; several: no |
| Sizes on sale | list of amounts | 1 to 8, each above 0 and larger than the one before | 5, 10 and 25 | yes |
| Share of the pool in work | bps | above 0, at most 10 000 | 8 000: at most 80% in work, the rest stays as a buffer | yes |
| Sizes spoken for at once | count | 1 to 16 | none | yes |
| Time to take the funded account after a pass | time | 1 hour to 30 days | none | yes |
| Term | time | 6 to 24 months; the two bounds are the deployment's, not the plan's, and `productBounds()` says whether they are the product's | none within the bounds | yes |
| Last stretch, with no exam sold | time | the whole time an exam may take and the time to take its account, and at least 14 days | follows from the exam | yes |
| Extending the term | by the creator, before it ends, by 6 to 24 months | — | not extended: at the end every position is closed, profit is shared and the pool stops | yes |
| The owner's money | locked for the term | out at the end, or with an extension in the last 14 days; the contract holds that window to 14 days and to a twelfth of the shortest term, whatever the deployment names | — | yes |

The sizes are amounts, not shares of the pool. A pool three times larger sells the same 5, 10
and 25, and more of them.

A seat is taken from the moment an exam on it is bought, and what can still be sold is whatever
fits under the share in work. With a pool of 100 and two seats of 25 sold, 30 are left: six
seats of 5, three of 10 or one of 25.

When the creator extends, they choose what stays. A pool that started with 100 and holds 115 at
the end of its term can be extended on 100 with the 15 withdrawn. In a pool of several
investors the creator extends and each investor decides whether to stay or take their part;
that rule is not built, and is the one most likely to change before it is.

## The exam

Sold for a price, traded on no one's money, judged by the contract.

| Setting | Type | Bounds | Default | Built |
|---|---|---|---|---|
| Price | USDC | above 0 | none | yes |
| Steps and their targets | list of bps | 1 to 4 steps, each 1 to 10 000 | one step of 1 000 (10%) | yes |
| Drawdown | bps | 1 to 9 999 | 600 (6%) | yes |
| Daily loss | bps | 1 to 9 999 | 300 (3%) | yes |
| Leverage | times 100 | 100 to 1 000: 1x to 10x | 500 (5x) | yes |
| Assets | list | 1 to 16 from the platform's list | none | yes |
| Time for a step | seconds | any; 0 is no deadline, which a pool does not take | 30 days | yes |
| Time a step may go without a fill | seconds | any; 0 is no limit | 14 days | yes |
| Trading days a step needs | count | 0, or what fits the step | 0 | yes |
| Cost of a fill | millionths of the notional | 0 to 100 000 (10%) | 950 (0.095%): the exchange's 0.045% and the builder fee of 0.05% a funded account pays | yes |
| Blocks before a fill, and its window | blocks | 1 to 600 each | none | yes |

The contract takes a price for every size. The creation form asks for one percentage of the
size and fills the prices from it.

## The funded account

Opened by the trader after a pass, on the pool's money, the same size as the exam.

| Setting | Type | Bounds | Default | Built |
|---|---|---|---|---|
| Size | — | the exam's size | the exam's size | yes |
| Target | bps | 1 to 10 000; none where the trader stops the account themselves | 1 000 (10%) | yes |
| Drawdown, daily loss, leverage, assets | as for the exam | as for the exam | the exam's: 6%, 3% a day, 5x | yes |
| Trader's share of the profit | bps | 0 to 10 000 | 8 000 (80%) | yes |
| Time a funded account may go without a trade, then anyone may close it | seconds | 1 to 90 days; there is no plan without it | 30 days | yes |
| After the target | account closes, account stays, or the trader stops it themselves | — | none | yes |
| Times a trader may take the seat again, where the account stays | count | any; 0 is no limit | 0 | yes |

## Against what prop firms offer

Compared with the public rule pages of nine prop firms, five traditional and four trading
crypto, read on 13 October 2026. They are not named: their pages change, and this is a list of
settings, not a statement about any firm.

**Covered by the exam contract.**

- An exam on a simulated balance.
- One step or two (10%, or 8% then 5%, or 10% then 5%); up to four.
- Every step starting on the starting balance, with the exam's profit not carried over.
- A static drawdown from the starting balance, and a daily loss limit.
- A time limit, or none.
- A minimum of trading days in a step.
- A target that counts only with every position closed.
- A pass declared by rule, at once, with no review by the firm.
- A breach that is final.
- Expiry after a time without a trade, on the exam and on the funded account.

**Covered by the pool's settings above.**

- A menu of sizes, each with its price.
- A funded account of the exam's size.
- The trader's share of the profit.
- A funded account with a target or without one, and what happens at the target.

**Not covered: no setting for it.**

- A trailing drawdown, by the balance at the end of the day or by the highest equity.
- A daily limit that pauses the account for the day and does not end it.
- Profitable days as a condition, as opposed to days traded.
- A consistency rule: the best day no more than a share of the whole profit.
- No positions overnight or over the weekend.
- The exam's price returned with the first payout.
- A retry or a reset at a discount, or a limited number of them.
- Payout cycles with different shares, a smallest payout, a cap on payouts.
- Growth of the account: a larger one after a number of months, payouts or targets.
- A monthly price, and a fee to activate the funded account.
- A cap on what one trader may hold across accounts.
- A check of the trader's identity before the funded account. This one is left out on purpose:
  nothing here is decided by us.

One difference is not a setting. At seven of the nine, the funded account is described as
simulated as well, and the firm pays the payouts. Here the funded account is the pool's own
capital on a real exchange account, and the trader's share is a share of what it actually made.

## What the estimate covers

The creation form is meant to show the creator, next to the settings, an estimate of what the
pool may return over its term. That estimate exists for these values and no others:

- an exam of one step of 10% or of 8%, or of two steps, 10% then 5% or 8% then 5%;
- a funded drawdown of 3, 4, 5, 6, 8 or 10%, and a funded target of 5, 8, 10, 15 or 20%;
- a trader's share, and a share of the pool in work, from 50% to 90%;
- a term of 6 to 24 months;
- each of the three things that can follow the target.

The contracts take wider bounds than that, as the tables above say. For a value outside this
list the form has no estimate to show, and says so.
