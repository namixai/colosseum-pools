# Design: a pool of many traders

**Status: built and tested offline. Not deployed on any chain, and not yet read by the audit.**
The pools on the testnet are the ones in [DESIGN.md](DESIGN.md) and
[SHARED-POOL.md](SHARED-POOL.md). This document describes `src/multi/MultiPool.sol` and the two
additions to `src/Pool.sol` it needs; nothing here exists on chain.

## What it is

One owner's capital and many traders. The owner sets a plan at creation: a menu of sizes with
their prices, the exam's rules, the funded account's rules, what the trader's share is, what
happens at the funded account's target, and how much of the pool may be in work at once
([POOL-PARAMETERS.md](POOL-PARAMETERS.md)). The plan cannot be changed afterwards.

A trader buys an exam off the menu. The exam is a paper exam ([PAPER-EXAM.md](PAPER-EXAM.md)):
it is traded on nobody's money, so the pool's capital is not touched by anyone who has not
passed. A trader who passes takes a funded account of the size they bought, on the pool's
capital, by their own call. One who does not gives the size back.

## A size is spoken for from the sale

The size of an exam counts as in work from the moment it is bought, although no money moves:
nothing else is sold against it, so the account is there for the trader who passes.

Two limits hold a sale back.

- **The share.** The sizes spoken for may not pass the plan's share of the pool's value: 80% of
  it by default, the rest being the pool's buffer. With a pool of 100 and two seats of 25 sold,
  30 are left: six seats of 5, three of 10 or one of 25.
- **The money.** A passing trader's account is paid from the pool's balance on HyperCore, so
  the size of every exam still running, and the 1 USDC a seat's new account may cost, has to be
  on that balance. Value that sits elsewhere, in a funded seat or as prices paid on HyperEVM,
  does not sell a seat.

Both are counted in the contract's own storage. A balance read from HyperCore is the start of
the block, so two calls in one block would read the same one; a count in storage they do not.
What the pool has sent in the last minute is counted against its balance for that minute,
whether or not it has left it yet. `room(line)` says how many more exams of a size the pool
would sell now, by all three limits: the share, the money and the number of seats.

An exam that ended without a pass, or whose pass nobody took within the plan's window, gives
its size back: `release(exam)`, by anyone.

## From the pass to the account

- `claim(exam)`, by the exam's trader, once it has passed and within the window. The pool picks
  a seat of that size, sends it the money, and from then on the money is the seat's.
- `open(seat)`, by anyone, once the money has arrived: a few seconds later. It starts the funded
  account for that trader. If the money did not arrive, `rearm(seat)` sends what is missing
  after a minute.

A seat that was just freed may still be sending its money back, so it rests for a minute before
an account is opened on it. The claim does not wait for that: it takes any free seat of the
line, and `open` waits out the rest. `sweep`, which takes back what reached a free seat, can
touch only free seats, and a sweep that finds nothing changes nothing. So nobody can keep a
trader who passed from the account by sweeping the seat, which the first version allowed.

A seat is a `Pool` the MultiPool owns and runs. For that, `Pool` gained one switch:

- `runByOwner(traderMayStop)`, once: the pool sells no challenge of its own from then on;
- `openFundedFor(trader)`, by the owner of such a pool: a funded stage for a trader examined
  elsewhere, in full or not at all;
- on such a pool the trader ends the stage themselves only where the owner said so.

Everything after the opening is the funded stage every pool has: its rules, a breach anyone may
call, the settlement and the trader's share. Pools that nobody hands to their owner behave as
before.

## At the target

The plan says one of three things.

- **The trader stops.** There is no target. The trader ends the stage when they choose and is
  paid their share of what it made.
- **The account closes.** `hitTarget(seat)`, by anyone, ends the stage once equity has reached
  the target with nothing open and no rule broken. The trader is paid their share and the seat
  is free again. Before the target the trader cannot end the stage themselves, so a small profit
  cannot be cashed by stopping early.
- **The account stays.** The same, and after the settlement the seat waits for its trader, who
  may take it again without another exam (`reopen(seat)`) within the plan's window. The plan
  may limit how many times; with no limit set there is none.

A breach ends the account in every mode, and its trader is owed nothing.

**An account nobody trades.** A plan sets how long a funded account may go without a trade:
30 days by default, one day at the least and 90 at the most. There is no plan without the limit,
or a size could sit unused to the end of the term. After that anyone may close the account with
`closeIdle(seat)`, without a breach: the trader is paid their share of what it made, as at the
pool's end, and the seat is free once it settles. A trade is seen as a change in the account's
positions in the plan's assets: the size of each, or the notional it was entered at. Equity is
not looked at, so money landing on an account, its funding or a price moving is no trade, and a
trader who closes and opens the same size again at another price is trading.
`noteActivity(seat)` records a trade, and a keeper calls it, so that a trader who traded between
two looks is not taken for one who did not. An account whose key never worked has no trades and
closes the same way.

`finish(seat)`, by anyone, closes the books on a settled stage: the seat's money goes back to
the pool and its size is no longer in work. A freed seat rests for a minute before it serves
again, so that what it sent back has left it.

## The term

A pool runs for a term the plan sets, 6 to 24 months, and then stops. Those two bounds, and the
fourteen days below, are not in the plan: whoever deploys the pool sets them, and the pool holds
its plan and every extension to them. A pool deployed to watch a whole term in an afternoon has
shorter ones, and says so (`minTerm()`, `maxTerm()`, `notice()`).

How long the money is locked does not rest on the deployer's honesty. The contract itself holds
the notice to fourteen days and to a twelfth of the shortest term the deployment allows, whatever
it names. Every term, an extension too, is at least that long, so the money is locked for eleven
twelfths of each at the least: on a six-month term it can leave only in the last fourteen days,
and a testnet deployment whose shortest term is three hours can still have a notice of fifteen
minutes. `productBounds()` is true only for a pool with the
product's bounds (6 and 24 months, fourteen days), so a page can tell the pools it offers from
the ones deployed to test.

- **The last stretch.** The pool stops selling exams when less time is left than an exam may
  take, all its steps together, and its trader then has to take the account; and never later
  than fourteen days before the end. So no exam is cut short by the pool's end, none has to be
  refunded, and a pass always has its whole window before the pool stops. Exams already running
  go on. `closing()` says the pool is in this
  stretch, `saleLead()` how long it is, and `endsAt()` when the pool stops; that is what a
  notice to traders is written from. A plan whose exam has no deadline, or whose steps take the
  whole term, is refused at creation.
- **Extending.** The owner may move the end further on, by 6 to 24 months counted from the old
  end, at any time before the pool has stopped. Sales resume at once. In the last fourteen days
  (the deployment's `notice`) the same call may take money out: with 100 put in and 115 there
  at the end of a term, the owner extends and takes the 15, under the two conditions of any
  withdrawal. Earlier the call moves the end and nothing else, however long the stretch without
  sales is: an exam that takes the whole term does not open the money for the whole term.
- **The end.** After `endsAt` nothing is sold, taken or opened. `endSeat(seat)`, by anyone,
  ends each funded stage without a breach, so its trader is paid their share of what it made,
  target or no target. Every size comes back: an exam still running or passed and not taken is
  released, a seat whose money was sent and never opened is emptied, and a seat waiting for its
  trader stops waiting. With nothing in work, all of the pool is the owner's to withdraw.

A pool that has stopped cannot be extended.

## The owner's money

The owner puts capital in by sending USDC to the pool's address on HyperCore. It is USDC and
nothing else: the pool counts no other token and sends no other out.

What is in is locked for the term. Nothing leaves before the pool has stopped, except in its
last fourteen days and only together with an extension. What the owner has just been sent no
longer counts as the pool's, though it may still show in its balance for a moment, so two
withdrawals in one block cannot take it under its share. After the end `withdraw` sends from
the HyperCore balance and `withdrawEvm` the prices paid for exams, which sit on HyperEVM. Every
time, what stays has to keep the sizes still spoken for inside the plan's share, and the money
promised to exams nobody has released where it is.

The owner is a single address. That is an investor on their own, or `PoolShares`, the contract
of a pool of several investors, which holds their shares and their rule for who stays and who
leaves when the term is extended ([POOL-SHARES.md](POOL-SHARES.md)).

## What anyone can check

The plan is public from the first block. Every sale, pass, claim and freed seat is an event of
this contract or of the exam and the seat, and `value()`, `inWork()` and `room()` can be called
by anyone. Whether a trader passed is the exam's own record, not this contract's and not ours.

## Weak points we know of

- **No stop on the exchange, for now.** A funded account of this pool is a real account on the
  exchange, and the rule is that its stop and its take are placed on the exchange itself, by
  the gateway. The gateway places them with the account's agent key, and the keys of this
  pool's registry are not on the gateway's host yet. Until they are, a funded account here has
  no stop on the exchange and no keeper calling a breach: its rules are kept only by whoever
  calls `breach`.

- **A minute of caution.** What the pool sent is counted against its balance for a minute even
  after it has left it. A pool that sends something every minute keeps counting, and can refuse
  a withdrawal or a sale it could have afforded. It errs on the side of the trader who was
  promised an account.
- **A seat needs a caller.** `open`, `finish`, `release` and `hitTarget` are anyone's to call,
  and somebody has to. A keeper for this pool is not written.
- **An exam holds its size for as long as it runs.** Its deadline bounds that, and an idle
  limit, where the plan sets one, ends an exam nobody trades sooner.
- **The exam's price can be small and the account real.** A trader who passes by luck trades the
  pool's capital. What bounds the loss is the funded account's drawdown line and its stop on the
  exchange, as on every pool.
- **The plan is fixed.** A mistake in it is a new pool.

## Tests

`test/multi/`, 64 tests on the hyper-evm-lib simulator: 11 for the pool handed to its owner, 53
for the pool of many traders. 237 mutations (`Y1`–`Y237` in `scripts/mutations.py`), each
caught by a named test:

```
forge test --match-path 'test/multi/*'
python3 scripts/mutations.py $(seq -f 'Y%g' 1 237)
```

## Not done yet

- The notice itself: the contract says when the pool stops; telling traders is not written.
- A keeper and the app's pages.
- A run on the testnet. The script that deploys is `ops/deploy_multi.py`, and
  `ops/multi_run.py` makes one step of a run at a time; neither has been run against a chain.
- The audit's reading.
