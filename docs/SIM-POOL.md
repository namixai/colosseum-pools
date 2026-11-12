# Design: the simulated pool

`src/sim/SimPool.sol` and `src/sim/SimPoolFactory.sol`. Not deployed yet; no external audit.

## Why

A pool of many traders (`MultiPool`) gives whoever passes its exam a funded account on its owner's
capital. A simulated pool has no capital. Both stages are paper exams (`PaperExam`): the exam, and
then the stage on the same size. Whoever passes the stage is paid out of the pool's USDC, which is
the prices traders paid for exams and what the pool's creator locked when opening it. Most exams
fail, and their prices pay for the ones that pass.

## What it does

- **Opening.** `SimPoolFactory.create(plan)` with 1 HYPE sent along, which goes to the platform, and
  the USDC for two payouts of the largest size on the menu, taken from the creator. A menu of
  10 000 locks 1 600. The plan sets the menu (up to eight sizes, each with its price), the rules and
  terms of both stages, how long a pass may wait before its stage starts, an optional trading
  group's owner (the partner) and its share, and the hash of the pool's terms written out in a
  `TermsBook`. No two lines share a price: a signed purchase names an amount, not a line. The
  factory runs PaperExam's own checks on both stages at the largest size, and checks every asset against its list and every leverage cap against the
  asset's, that both stages end (a deadline and an idle limit), that the stage is one step, and
  that a fill window is 1 to 60 blocks. Anyone may open a pool; the filler and the checker may not.
- **Selling.** `buy(line)`. No seats and no share of the pool limit a sale. 0.5 % of the price goes
  to the platform, the rest to the pool. The buyer gets a paper exam whose orders only the factory's
  filler fills inside their window, at the book when the plan says so: never better for the trader
  than the oracle price and at most the plan's `maxWorseBps` worse (`PaperExam.initializeWithFiller`).
- **Selling by signature.** `buyWithAuthorization(trader, line, validAfter, validBefore, nonce, v,
  r, s)`: the trader signs a USDC transfer of the line's price to the pool (EIP-3009, Circle's
  FiatToken V2.2 on HyperEVM and its testnet) and needs no gas; anyone sends it, a relayer of the
  platform's or a partner's, and pays the gas. The token checks the signature against the line's
  price, the time and the nonce, and takes it only from the pool it names. Same price, same fee,
  `ExamSold` with `bySignature`. A token without EIP-3009 refuses with `NoAuthorization`; `buy`
  works on any.
- **The stage.** `fund(exam)`: the trader, within the plan's window after the pass, starts a second
  paper exam on the same size. Nothing moves.
- **The payout.** `claimPayout(stage)` once the stage has passed, by anyone, and by the keeper in its next
  slow pass (every 30 seconds): 8 % of the size, due 72 hours after the claim. The keeper sends `pay` in its
  first pass after that, so a trader is paid 72 hours and about a minute after passing, or later, in order, if
  the pool does not hold enough then. `pay(max)`, by anyone, pays what is due in the order it came due while the pool's USDC
  covers the next; a payout the pool cannot cover yet waits (`PayoutQueued`) and is paid first from
  the prices that come in. A payout the token refuses to send (say the trader's address was blocked)
  is set aside for the trader (`PayoutHeld`), who takes it to any address with `claimHeld`, also
  after the pool has finished; the queue moves on. One stage, one payout: the stage has one step and its pass ends it.
- **A stop.** Until a payout is due, the factory's checker may stop it once, with a reason: 1,
  opposite positions with another account of the factory; 2, that and wallets linked. The call
  names the other account, the transactions of the trades (at least two) and, for reason 2, the
  transactions that link the wallets. A stopped payout's money stays in the pool, set aside for 14
  days after the stop (`STOP_RESERVE`, `reserved`) as a held payout's is: the creator does not
  withdraw it, it is no profit for the partner's share, and `pay` pays no other payout out of it.
  Within those days the trader may object on chain (`objectStop`, the keccak256 of the objection's
  text, `StopObjected`), and the checker lifts a stop that was wrong (`liftStop`): the payout is due
  again, out of the money the stop kept, and paid as any other. After them the stop can no longer be
  lifted; anyone makes it final (`finalizeStop`, `StopFinal`), and its money is the pool's again.
- **The creator.** `withdraw(amount)`: only what stays above three payouts of the largest size and
  above every payout owed, held or kept for a recent stop. 0.5 % goes to the platform. The partner's
  share is of the pool's money above what the creator put in, after what the pool owes, holds for
  traders and keeps for recent stops: a
  withdrawal takes that profit first, and the partner gets its share of it. `addToPot` puts in more
  principal, never shared, which comes back once the profit above it has been taken, or at `finish`. `setPartnerShare` moves the share within 40 to 60 %, for
  later withdrawals. `close` stops sales; `finish`, once nothing runs, nothing is owed and no stop
  can still be lifted, sends what is left, the principal first.

## The platform's numbers

Constants of the factory, the same for every pool: 1 HYPE to open (`CREATION_FEE`); 0.5 % of each
exam's price and of each withdrawal (`FEE_BPS`); a payout of 8 % of the size (`PAYOUT_BPS`), with no
fee on it; two payouts of the largest size locked at opening (`LOCKED_PAYOUTS`); three kept while
the pool is open (`FLOOR_PAYOUTS`); 72 hours before a payout is paid (`PAYOUT_DELAY`); 14 days a
stopped payout's money stays set aside and the stop can be lifted (`STOP_RESERVE`); a fill
window of 1 to 60 blocks; a partner's share of 40 to 60 % (`MIN_PARTNER_BPS`, `MAX_PARTNER_BPS`).

## What anyone can check

Every exam and stage is a paper exam: each order, fill and price is in the chain's history, and
each fill says how many blocks after the first possible one it came. The page of a pool reads
`PayoutPaid` (the transaction of each payout is its proof), `PayoutStopped` with its reason,
account and transactions, and `StopLifted`; each of the last two carries the pool's counts of
stops, lifts and payouts. A trader's objection to a stop is `StopObjected`, with the hash of its
text, and a stop that became final is `StopFinal`.

## Weak points we know of

- **The platform is the filler, the checker, and in its first pools the creator.** The filler picks
  the block inside the window; the checker stops payouts whose money, once the stop is final 14 days later,
  stays with the creator. Neither
  is held by the contract, only by what shows: `late` on every fill, the reason and transactions on
  every stop, the counts, and the open checking script (`ops/sim_check.py`), which anyone can run on
  the same blocks while a payout still waits; once stopped, the stop's own record carries its pair,
  trades and links. A creator is never the filler or the checker.
- **The checker's evidence is not checked by the contract.** It checks that the other account is
  one of the factory's and that there are transactions; whether they show what the reason says is
  for anyone to read.
- **A pass comes faster than prices.** Payouts then wait in order. A trading group that trades one
  signal passes together; its creator can lock more with `addToPot`.
- **No depth, no funding, no resting orders**: those of the paper exam (`docs/PAPER-EXAM.md`).

## Tests

`test/sim/SimPool.t.sol`, 49 tests on the hyper-evm-lib simulator (EIP-3009 on a stand-in with
FiatToken V2.2's interface and words), and 107 mutations (`SM1`–`SM107` in `scripts/mutations.py`),
each caught by a named test:

```
forge test --match-path 'test/sim/*'
python3 scripts/mutations.py $(seq -f 'SM%g' 1 107)
```

`test/sim/SimPoolInvariant.t.sol` holds the pool's money to its books whatever is called in whatever order: a
handler buys, passes and fails exams and stages, claims, stops, lifts and pays payouts, has the trader object to a
stop and anyone make it final after its 14 days, has the token refuse a
trader, claims what was held, withdraws and adds for the creator, closes and finishes (48 runs of 120 calls in CI,
with `fail-on-revert`: a handler call the pool refuses unexpectedly fails the run instead of passing unseen).
Nine invariants: every dollar in and out accounted for; what is held always there and equal to the refused
payouts less the claims, `reserved` the stopped payouts, and `pay` spending neither; `paidTotal` the sum of the
payments, and `pay` sends exactly those; a withdrawal never below three payouts above what is owed, held and
reserved, nor past `withdrawable`; one purchase, at most one payout; `owed` the due payouts; stops less lifts are
those standing and those made final; nothing due behind the head of the queue; `live` the accounts not done, a
pool finished only with nothing owed, running or reserved, and keeping only what is held. Not an invariant, by design: that the pool
covers every payout owed. Passes can come faster than prices, and a payout waits in order (`PayoutQueued`).

## Running it

- **The keeper**, `ops/sim_keeper.py` (the host's `multi_keeper` unit hands it a record with a
  `SimPoolFactory`). A fast loop reads, every block once the factory has a pool, about 3 600
  `eth_getLogs` an hour, the live accounts' `Placed` and the pools' new exams and stages with their
  orders, so an order placed seconds after its purchase is seen. It sends each order's `fill` from the
  filler's key for the block its window opens; a fill the exam refuses (the book past the ceiling, say)
  is sent again for the next block, once a block, while the window lasts. A fill waits for its receipt,
  so two orders in one block go one after the other. A slow pass
  every 30 seconds finds pools, exams and stages and makes their calls: the day's snapshot, `breach`,
  `expire`, `release`, `claimPayout`, `pay`. `--measure EXAM --trader-wallet K` places orders on the
  trader's own exam and writes how many blocks late each fill landed, p50 and p99: the number the
  plan's fill window is set from.
- **The relayer**, `ops/sim_relay.py`: a signed purchase in, `buyWithAuthorization` out, from a key
  that pays the gas. It refuses before spending any: another factory's pool, no such line, a signature
  ending within 30 seconds, a used nonce, a call the pool would refuse. `--serve PORT` listens on
  127.0.0.1 for `POST /buy`.
- **The deployment**, `ops/deploy_sim.py`: `deploy` puts PaperExam, SimPool, a TermsBook and the
  factory on testnet with Circle's USDC, names the filler, the checker and the recipient, lists BTC,
  ETH and SOL with their caps (no HYPE), and writes `deployments/testnet-<label>.json` with demo3's
  keeper nodes. `pool --plan` publishes the terms, approves the locked USDC and opens the pool with 1
  HYPE. Without `--send` both print what they would do and read no key. The first pool's plan is
  `deployments/plans/sim-first-pool.json`: 5 000 for 45 and 10 000 for 85, 1 600 locked, thirty days
  a stage with no minimum of trading days, seven days to start the stage after a pass, 0.045 % a fill
  (Hyperliquid's taker fee; no builder fee, since nothing trades on the exchange), at the book with a
  ceiling of 150 bp, a fill window of 10 blocks until `--measure` says otherwise. The factory needs an
  idle limit; the plan sets it to the stage's thirty days, where it never ends a stage before its
  deadline, so the terms name no idle rule.

- **The checker**, `ops/sim_check.py`: the open script behind every stop. For each payout still
  waiting it matches every fill of the paid stage with the fills of every other exam and stage of the
  factory: the same asset, opposite sides, at most 10 minutes apart, sizes within 20 %. A match is
  reason 1; with the two traders funded by one address, or one paying the other (USDC transfers since
  the deployment), reason 2. It stops a payout at least 5 minutes before it is due, once, from the
  factory's checker key, and lifts its own stop with `--lift POOL:ID`, within 14 days of the stop.
  Without `--wallet` it only
  reports. Anyone can run it on the same blocks and get the same pairs. It reads this factory's
  accounts only, not the MultiPool's or the other deployments'.

- **The host**: `bootstrap.sh --sim-instance` makes the instance without a gateway, and `install.sh` enables
  `colosseum-multi-keeper@<name>` with the filler's wallet, after checking it and the checker's against the
  record (docs/HOSTING.md, "A simulated pool's keeper"). No gateway, no instance keeper, no nginx route.

## Measuring the filler's window

The fill window of a plan is fixed at creation, so it is measured first, on a pool of its own
(`deployments/plans/sim-measure-pool.json`: one line of 1 000 for 9, the widest window, 60 blocks, so a late
fill is measured and not lapsed), with the host's keeper running and filling from the filler's key.

    spike/.venv/bin/python -m ops.deploy_sim buy --deployment <label> --pool <measuring pool> --line 0 --trader trader --send
    spike/.venv/bin/python -m ops.sim_keeper --deployment <label> --measure <exam> --trader-wallet trader --orders 100

`--measure` sends the trader's own transactions (testnet only, by hand; it has no dry run). It places the orders
one at a time, alternating buy and sell, 12 size units of BTC each (0.00012 BTC, over the 10 USDC of a trade),
and waits up to 120 seconds for each to be filled or to lapse.

**What it counts.** Each fill's `late`, from its `Filled` event: the block it landed in minus the first block the
order could fill in (the order's block plus `fillDelay`); 0 is the first possible block. And its `source`: the
book, or the oracle when the book was empty, crossed or unreadable. Lapsed orders have no `late` and are not in
the numbers: `ops/state/sim-measure.json` says `orders` and `filled`, and a row for each order. p50 and p99 are
`sorted[int(p * n)]`; up to 100 orders p99 is the largest value.

**The window.** An order fills in blocks `from` to `from + fillWindow - 1`, so a fill `late` L needs a window of
at least L + 1. A measure counts only if every order filled (`filled` equals `orders`): a lapse on the widest
window means the keeper stalled, and that is found out before any window is set. Then

    fill_window = max_late + 1 + max(2, ceil((max_late + 1) / 2)),   within 1 to 60

which leaves half again for a slower hour: a largest `late` of 3 gives 6 blocks, of 6 gives 11. Above a
largest `late` of 39 the formula passes 60 and the window stays 60, with less headroom; a filler that late is
itself the finding, and it is looked into before a pool is opened on such a window. A longer window
lets the filler choose among more blocks, which the terms promise it does not; a shorter one lapses orders when
the node is slow.

**The price ceiling.** At the book, a fill worse than `maxWorseBps` from the oracle reverts, and the keeper
sends it again for the next block of the window: a ceiling under the book's distance from the oracle shows up in
the measure as lateness, or as lapses. So the ceiling is set from the book, read beside the measure: every block's best bid and
offer against the oracle, on the trader's side (the ask above it for a buy, the bid below it for a sell). The
ceiling is the largest distance seen plus 30 bp, and at most `MAX_WORSE_BPS` (500). On 11 November 2026
testnet's books stood 77 to 118 bp from the oracle, so the plans say 150. On mainnet, where BTC's and ETH's
spread is under 1 bp, it is measured there again, and so is the window.

## Not done yet

- The checker's unit on the host. The instance keeps the checker's wallet (docs/HOSTING.md, "A simulated
  pool's keeper"), and `ops/sim_check.py --wallet checker` runs by hand with it until a unit runs it.
