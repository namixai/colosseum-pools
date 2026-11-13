# Design: the paper exam

**Status: built and tested offline. Not deployed on any chain, not used by any pool, and not yet
read by the audit.** The pools on the testnet still run their challenge on a real Hyperliquid
account funded by the pool ([DESIGN.md](DESIGN.md), [SHARED-POOL.md](SHARED-POOL.md)). This
document describes `src/exam/PaperExam.sol` and nothing that exists on chain.

## Why

A challenge today trades the pool's own money: the depositors carry the result of an audition
before anyone knows whether the trader can trade. The paper exam takes the audition off their
money. It is traded on no exchange and on nobody's capital: the trader's orders are transactions,
the position is a number in the contract, and every price is the oracle price the chain itself
shows, which the validators take from the exchanges' prices outside (deep audit N6).

What it keeps is the thing the real challenge was for: nobody writes the result. A third party
can recompute an exam from the chain, order by order, and no key of ours appears in it.

## What it does

An exam is one contract per trader, a clone started by whoever sells it (the pool). It starts
with a balance, `capital`, that is a number and not money.

- **An order.** The trader, or a second key the trader names, calls `place(asset, isBuy, size)`.
  One order waits at a time.
- **A fill.** `fill()` executes the waiting order at the oracle price of the block that fills it,
  not of the block that placed it. It can be called from `fillDelay` blocks after the order, for
  `fillWindow` blocks, by anyone but the trader and their second key: whoever placed the order
  does not pick the block it fills in, and the keeper fills it as soon as it can (deep audit N6).
  What is left (the pools team, 18 October 2026): a third address of the trader's may still fill, and so
  pick a block within the window; at the oracle price the choice is worth little. After that the
  order has lapsed and the next one replaces it.
- **A filler.** An exam started with `initializeWithFiller` names the one address that fills inside
  the window (the pools team, 11 November 2026, for the simulated pool, where a pass is paid out of
  the other traders' prices). The trader then has nobody to send in their place: no third address
  fills, and the filler is never the trader's agent, so there is no block to pick and no fill to
  skip. An order the filler missed lapses at no
  cost. If the filler stops, an order that only makes a position smaller can be filled by anyone,
  the trader too, `FALLBACK_BLOCKS` (60) after its window, so a stalled filler never keeps a trader
  in a position. Every fill says how many blocks after `placedBlock + fillDelay` it came (`late` in
  `Filled`). An exam started with `initialize` has no filler and works as before.
- **A fill at the book.** Started with a filler and `fromBook`, an exam fills a buy at the best ask
  and a sell at the best bid, from the chain's own best bid and offer (`PrecompileLib.bbo`), so a
  fill pays up to half the spread as on the exchange (the pools team, 11 November 2026). Never
  better for the trader than the oracle price: a buy no cheaper, a sell no dearer, so a thin book,
  one the trader moves with orders of their own, or one that stands off the oracle gives nothing
  under it. Nor more than `maxWorseBps` (1 to 500) worse than the oracle: past that the fill is
  refused in that block (`TooFarFromOracle`) and the order waits for a later one in its window, so
  a book that stands off the market, as testnet's did by 77 to 118 bp on 11 November 2026, does not
  fill a percent worse than it. A book that does not answer, is empty on a side or is crossed
  fills at the oracle. `Filled.source` says which: 0 the oracle, 1 the book, 2 the oracle because
  the book would not do. The order that only makes a position smaller after `FALLBACK_BLOCKS`
  takes the same rule. Equity, the day's snapshot and every rule stay on the oracle price: the
  book is what a fill costs, the oracle what a position is worth, and a book is the thing a trader
  can push.
- **Equity** is the balance plus every position at the current oracle price. There is nothing else to
  read: no margin, no funding, no resting orders.
- **The rules** are the four a real account has, with the same arithmetic as `RuledAccount`: a
  static drawdown from the starting balance, a daily loss from the snapshot taken in the first
  15 minutes of the UTC day (`checkpoint()`, anyone), leverage as notional over equity, and a
  list of assets. `breach()` ends the exam when a rule is broken at that moment; anyone may call
  it.
- **Leverage at the fill.** An order that would take the account over its leverage rule is
  refused. An order that only reduces a position is never refused, so there is always a way out.
- **Steps.** `targetsBps` is a list. `graduate()`, by the trader or their agent and nobody
  else, passes the current step when no rule is broken,
  nothing is open, equity has reached the step's target, the deadline has not passed and the
  step has its trading days. The last step passes the exam. Any other starts the next step on a
  fresh balance: profit made in one step is not carried into the next.
- **The deadline, and a step nobody trades.** Each step has `stepDuration` seconds, or no
  deadline when that is 0. A step may also go no longer than `maxIdle` seconds without a fill,
  counted from its start or from its latest fill; 0 turns that off. An order placed and never
  filled is not a fill. Past either limit nothing can be placed, filled or passed, and
  `expire()` ends the exam, so an exam that was bought and left alone gives its place back.
  `expire()` reads no price, so a price that does not answer cannot hold an exam past its time.

The pool reads `result()`: the status, the trader, the size and the time of the pass. Before it
sells anything it can ask `check()` whether an exam would start on its rules and terms.

## Parameters

| Name | Type | Bounds | Meaning |
|---|---|---|---|
| `capital` | uint64, 1e6 = 1 USDC | above 0 | the balance every step starts with |
| `targetsBps` | uint16[] | 1 to 4 steps, each 1 to 10 000 | the profit target of each step |
| `stepDuration` | uint32, seconds | any; 0 is no deadline | how long one step may run |
| `maxIdle` | uint32, seconds | any; 0 is no limit | how long a step may go without a fill |
| `minTradingDays` | uint16 | at most the UTC days the step can touch; any number with no deadline | days with a fill a step needs |
| `costPpm` | uint32, millionths | 0 to 100 000 (10%) | what a fill costs, of its notional; 950 is 0.095% |
| `fillDelay` | uint16, blocks | 1 to 600 | blocks between an order and its first fill |
| `fillWindow` | uint16, blocks | 1 to 600 | blocks an order can be filled for |
| `dailyLossBps` | uint16 | 1 to 9 999 | the day's limit, from the day's snapshot |
| `maxDrawdownBps` | uint16 | 1 to 9 999 | the limit from the starting balance |
| `maxLeverageX100` | uint32 | 100 to 1 000: 1x to 10x | notional over equity, times 100 |
| `assets` | uint32[] | 1 to 16, none twice, size decimals at most 6 | what may be traded |

The last four are the `Rules` struct every pool already has. The contract takes every value
explicitly; it has no defaults of its own.

## What anyone can check

Everything the result depends on is in the chain's history: each order and each fill is a
transaction, each price is the one the chain's own price read returned in that block, and the
arithmetic is the contract's. The events `Placed`, `Filled`, `DaySnapshot`, `StepPassed`,
`Passed` and `Stopped` are enough to rebuild an exam without calling it; `Filled` also says how
many blocks after the first possible one each fill came.

Nothing is on our word about the result. What is a choice of ours, and public, is the model: a
fill at the mark, a flat cost, and the things listed next.

## What it does not model

- **Depth.** A fill takes the mark, or the top of the book, at any size. For the sizes an exam is
  sold in that is close to what a liquid market gives; it is not a statement about large orders.
- **Funding.** A position held for days pays and receives nothing.
- **Resting orders.** There is no limit order and no stop. A trader who wants out has to place
  the order that takes them out.

## Weak points we know of

- **A faster feed.** A mark read by a contract is the start of the block. A trader who sees the
  market sooner knows where the next mark will be. That is why an order is never filled in its
  own block, and why anyone may fill it at the first block that takes it: a keeper that does so
  every time leaves the trader no choice. While nobody else fills, the trader still decides
  whether to, for `fillWindow` blocks, and `costPpm` is what has to make that worth less than it
  costs. The two numbers and the keeper are the defence; the contract alone does not close it.
- **The day's snapshot is taken by whoever calls it.** Without a keeper that is the trader, who
  can leave it after a good day and take it after a bad one, and so pick the base the day's
  limit is measured from. A real account under `RuledAccount` has the same rule; a keeper that
  takes the snapshot at midnight is what closes it.
- **The filler picks the block.** Anyone may fill a matured order, at any block of its window.
  Somebody who wants the trader to fail -- the pool's owner, say -- can fill at the worst block
  of up to `fillWindow` of them. A short window is the defence; a cancel by the trader would
  hand back the choice the delay takes away, so there is none.
  With a filler that is one known address, and where the pool's creator is the platform that also
  runs the filler, the platform picks the block for its own pool. Nothing in the contract stops
  that; what holds it is that it shows: `late` on every fill, counted by anyone over a pool.
- **A dip nobody caught.** The rules are judged against the marks of the moment someone asks.
  A price that crossed a line and came back before `breach()` was called is not a breach. A real
  account has a stop on the exchange for this; an exam has only whoever is watching.
- **A thin market.** The mark of a thinly traded asset can be moved by whoever wants to: one
  account trading against another of its own on a book nobody else rests on. The exam reads the
  oracle price, which that does not move, and a funded account trades only on the assets the
  factory lists as deep enough (`PoolFactory.isFundedAsset`, the operator's word on the book's
  depth; deep audit N6). On a testnet, where every market is thin, an exam shows that the
  mechanism works and nothing about skill.
- **A price that does not answer.** While the price read of a held asset fails or returns zero,
  nobody can fill, judge or pass that exam. It still expires on time.
- **No limit, no end.** An exam with neither a deadline nor an idle limit, that neither passes
  nor breaks a rule, stays open. Whatever sells it has to bound that itself.
- **The exam costs a cheat nothing.** On the pool's own money, buying a pass by trading against
  a second account of one's own costs that account what the challenge gained, and the gain
  stays in the pool. A paper exam has no counterparty to pay. It measures trading against the
  mark, and the depositors' protection is what the funded account does next: its drawdown line,
  its stop on the exchange and its list of assets.

## Tests

`test/exam/PaperExam.t.sol`, 67 tests on the hyper-evm-lib simulator, and 124 mutations of the
contract (`X1`–`X124` in `scripts/mutations.py`), each caught by a named test:

```
forge test --match-path 'test/exam/*'
python3 scripts/mutations.py $(seq -f 'X%g' 1 124)
```

## Not done yet

- A pool that sells an exam, reads its result and opens a funded account on it. The parameters
  it is being built to are in [POOL-PARAMETERS.md](POOL-PARAMETERS.md).
- A keeper of exams of their own, outside a MultiPool: `ops/multi_keeper.py` fills a MultiPool's
  exams' orders as soon as they can be filled, takes the day's snapshot and calls `breach`.
- The trader's screen. Its library is there, `app/lib/exam.js`: the account as the contract counts it
  with its lines, its target and its time, the calls behind the buttons (`place`, `graduate`, `setAgent`),
  an order worked out before it is sent, the contract's refusals in plain words, and Hyperliquid's open
  candles and book for the chart. The page itself is not built: until it is, orders are sent by scripts.
- A run on the testnet, and the audit's reading.
