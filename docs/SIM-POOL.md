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
  terms of both stages, how long a pass may wait before its stage starts, and the hash of the
  pool's terms written out in a `TermsBook`. No two lines share a price: a signed purchase names an amount, not a line. The
  factory runs PaperExam's own checks on both stages at the largest size, and checks every asset against its list and every leverage cap against the
  asset's, that both stages end (a deadline and an idle limit), that the stage is one step, and
  that a fill window is 1 to 60 blocks. Anyone may open a pool; the filler, the checker and the
  closer may not.
- **Codes.** `setCode(code, partner, bps)`, by the creator: `code` is the keccak256 of a trading
  group's code, `partner` the address the group's owner gave, `bps` its share, 0 or 40 to 60 %, up
  to 32 codes a pool (`MAX_CODES`). A code is public by nature: its owner hands it to a whole
  channel, so nothing hides it, and whoever buys with it is the owner's buyer. A code can be changed,
  or taken off sale with a zero partner, until a sale names it; from then on its partner and share
  are fixed (`CodeUsed`), so an owner who has brought traders does not depend on the creator.
- **Selling.** `buy(line, code)`, with a code the creator set, or `NoSuchCode`. No seats and no
  share of the pool limit a sale. 0.5 % of the price goes to the platform, the rest to the pool,
  and that rest counts as what the code brought (`codeOf(code).brought`, and `brought` for all
  codes). `ExamSold` names the code and its partner. The buyer gets a paper exam whose orders only the factory's
  filler fills inside their window, at the book when the plan says so: never better for the trader
  than the oracle price and at most the plan's `maxWorseBps` worse (`PaperExam.initializeWithFiller`).
- **Selling by signature.** `buyWithAuthorization(trader, line, code, salt, validAfter,
  validBefore, v, r, s)`: the trader signs a USDC transfer of the line's price to the pool (EIP-3009,
  Circle's FiatToken V2.2 on HyperEVM and its testnet) and needs no gas; anyone sends it, a relayer
  of the platform's or a partner's, and pays the gas. The signed nonce is
  keccak256(abi.encode(code, salt)), so the code is in the trader's signature and whoever sends it
  cannot name another. The token checks the signature against the line's price, the time and the
  nonce, and takes it only from the pool it names. Same price, same fee,
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
  withdraw it, it is no profit for the partners' shares, and `pay` pays no other payout out of it.
  Within those days the trader may object on chain (`objectStop`, the keccak256 of the objection's
  text, `StopObjected`), and the checker lifts a stop that was wrong (`liftStop`): the payout is due
  again, out of the money the stop kept, and paid as any other. After them the stop can no longer be
  lifted; anyone makes it final (`finalizeStop`, `StopFinal`), and its money is the pool's again.
- **The creator.** A withdrawal waits a week. `requestWithdraw(amount)` asks for at most what
  `withdrawable` allows then and moves nothing (`WithdrawRequested` with when it is ready and when
  it lapses); one request stands at a time, and a new one replaces it and starts the week again.
  `withdraw()` carries it out from `WITHDRAW_DELAY` (7 days) on, for `WITHDRAW_WINDOW` (3 days),
  then it lapses (`RequestLapsed`): a request cannot be left standing to make the week nothing. It
  pays the less of what was asked and what `withdrawable` allows when it is carried out: never more
  than was asked, and only what the pool can spare then, so payouts that came due in the week come
  first. Money that came in during the week counts toward what the pool can spare, within the
  amount asked; what is above the request waits for another one. The
  creator or the factory's closer may cancel it (`cancelWithdraw`, `WithdrawCancelled` with who);
  the closer only cancels: it changes neither the amount nor where the money goes. There is no
  other way for the creator to take money out before `finish`. `withdrawable` is only what stays
  above three payouts of the largest size and above every payout owed, held or kept for a recent
  stop. 0.5 % goes to the platform. Profit is the
  pool's money above what the creator put in, after what the pool owes, holds and keeps for recent
  stops; a withdrawal takes it first. Of the profit a withdrawal takes, less the platform's part,
  each code's partner is sent its `bps` in the part its code brought of all the pool took:
  profit × (the code's `brought` / `brought`) × `bps`. A code with no share leaves its part to the
  creator. A partner whose address the token refuses has its part set aside, `heldOf` and
  `claimHeld` as for a refused payout (`PartnerPaid` says which). `addToPot` puts in more principal,
  never shared, which comes back once the profit above it has been taken, or at `finish`. `close` stops sales; `finish`, once nothing runs, nothing is owed and no stop
  can still be lifted, and a week after the close (`closedAt` + `WITHDRAW_DELAY`, `NotReady` before),
  sends what is left, the principal first, and ends a standing request. The week keeps `close` and
  `finish` from being a withdrawal of everything in one block.
- **The term.** A pool runs at least 180 days from its opening (`MIN_TERM`, `lockedUntil`). Before
  then the creator can neither `close` it (`Locked`), so it sells all that time, nor take back what
  it put in: `withdraw` gives the profit only, never the principal, the locked payouts or what
  `addToPot` added. Only the factory's closer, a platform key the operator names (`setCloser`), may
  close a pool sooner (`closedEarly`, `ClosedEarly`). That changes nothing about where the money
  goes: `finish`, by the creator or the closer, still waits until nothing runs, nothing is owed and
  no stop can be lifted, leaves what is held for its traders and partners, sends each code's partner
  its share and the creator the rest; the closer gets none of it. From `lockedUntil` on, the creator closes and finishes as before.

## The platform's numbers

Constants of the factory, the same for every pool: 1 HYPE to open (`CREATION_FEE`); 0.5 % of each
exam's price and of each withdrawal (`FEE_BPS`); a payout of 8 % of the size (`PAYOUT_BPS`), with no
fee on it; two payouts of the largest size locked at opening (`LOCKED_PAYOUTS`); three kept while
the pool is open (`FLOOR_PAYOUTS`); 72 hours before a payout is paid (`PAYOUT_DELAY`); 14 days a
stopped payout's money stays set aside and the stop can be lifted (`STOP_RESERVE`); 180 days a
pool runs before its creator may close it or take back its principal (`MIN_TERM`); 7 days from a
request to withdraw to its money, and from a close to `finish` (`WITHDRAW_DELAY`), and 3 days to
carry out a ready request (`WITHDRAW_WINDOW`); a fill
window of 1 to 60 blocks; a code's share of 0 or 40 to 60 % (`MIN_PARTNER_BPS`, `MAX_PARTNER_BPS`)
and up to 32 codes a pool (`MAX_CODES`).

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

`test/sim/SimPool.t.sol`, 69 tests on the hyper-evm-lib simulator (EIP-3009 on a stand-in with
FiatToken V2.2's interface and words), and every `SM` mutation of `scripts/mutations.py` (SM1 to
SM149 on this branch: the delay, the window, the less of the two and finish's week among them), each
caught by a named test. The second command lists them from the file, so a new one is never left out:

```
forge test --match-path 'test/sim/*'
python3 scripts/mutations.py $(grep -oE "[\"']SM[0-9]+[\"']" scripts/mutations.py | tr -d "\"'")
```

`test/sim/SimPoolInvariant.t.sol` holds the pool's money to its books whatever is called in whatever order: a
handler buys with codes, directly and by signature, sets codes for three groups (40 %, 60 % and no share) and
tries to set them again after a sale, passes and fails exams and stages, claims, stops, lifts and pays payouts, has
the trader object to a stop and anyone make it final after its 14 days, has the token refuse a trader or a group's
owner, claims what was held, asks to withdraw, withdraws (at once now and then, which must be refused, otherwise
from the week on to past its window), cancels a request (the creator or the closer; a stranger refused) and adds
for the creator, closes (the creator from the term on, refused
before it; the closer early) and finishes (by the creator or the closer), jumps past the 180 days now and then
(48 runs of 120 calls in CI,
with `fail-on-revert`: a handler call the pool refuses unexpectedly fails the run instead of passing unseen).
Fourteen invariants: every dollar in and out accounted for; what is held always there and equal to the refused
payouts and partners' parts less the claims, `reserved` the stopped payouts, and `pay` spending neither; `paidTotal` the sum of the
payments, and `pay` sends exactly those; a withdrawal never below three payouts above what is owed, held and
reserved, nor past `withdrawable`, and before the term not past the profit; one purchase, at most one payout; `owed` the due payouts; stops less lifts are
those standing and those made final; nothing due behind the head of the queue; `live` the accounts not done, a
pool finished only with nothing owed, running or reserved, and keeping only what is held; `brought` each code's
sales and all of them together, and a code a sale has named keeping its partner and share; and each withdrawal's
shares, worked out apart from the pool: the platform's 0.5 %, each partner's part of the profit less that 0.5 % by
its code's share and what its code brought (sent, or set aside if the token refuses), the creator the rest, and the
partners together never above 60 % of it; before the term, until the pool is finished, `principal` all the creator
put in (no path gives any of it back), only the closer closing it (`closedEarly`), and no stranger closing or
finishing it; and, closed early or not, finished by the creator or the closer, the closer getting none of the
money and the platform only its 0.5 % of each price and each withdrawal; and no money leaving the pool for the
creator but by `withdraw()` of its own request a week old and not lapsed, paying the less of the request and
`withdrawable`, or by `finish` a week after the close, with a request, a cancel or a close moving nothing. Not an
invariant, by design: that the pool
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
  that pays the gas. It refuses before spending any: another factory's pool, no such line, a code the
  pool has not set, a signature ending within 30 seconds, a used nonce, a call the pool would refuse. `--serve PORT` listens on
  127.0.0.1 for `POST /buy`.
- **The deployment**, `ops/deploy_sim.py`: `deploy` puts PaperExam, SimPool, a TermsBook and the
  factory on testnet with Circle's USDC, names the filler, the checker, the closer (four keys with
  the operator, none the same) and the recipient, lists BTC,
  ETH and SOL with their caps (no HYPE), and writes `deployments/testnet-<label>.json` with demo3's
  keeper nodes. `pool --plan` publishes the terms, approves the locked USDC, opens the pool with 1
  HYPE and sets the plan's codes; `codes --plan` sets them on a pool already open, skipping one that
  is on chain as listed and refusing, before anything is sent, one a sale has fixed otherwise.
  `buy --code` buys with a code's text. `close --key` and `finish --key` read the pool first and say as
  whom it would take the call or why it would refuse (`Locked` before the term unless the key is the
  closer; for `finish`, each thing still running, owed or reserved, and with `--exam` until when that
  exam runs, and the week after the close). `request --amount`, `withdraw` and `cancel` do the same for
  the creator's withdrawal: what it asks and from when to when it can be carried out, what it pays
  now, or `NotReady`, `RequestLapsed`, `NoRequest` with the times. Without `--send` they print what
  they would do and read no key. The first pool's plan is
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

- **The review of a payout**, `ops/payout_review.py`: what whoever signs a payout through, or stops it,
  reads first. It reads the chain only, sends nothing and holds no key. For each payout still waiting
  it names the trader, the stage, the exam, the amount and when it is due, and then:
  - the pairs of the stage and of the exam, by the checker's rule, with both fills' transactions;
  - every other buyer of the factory this trader shares a sender of USDC with, or exchanged USDC with.
    The pools are left out, since they pay every trader. A sender that funded more than five buyers
    is named but not counted, as an exchange's wallet would be;
  - how late the filler put this trader's fills (`late`) against everyone else's;
  - the code the exam was bought under: its sales, its wallets, the longest run of sales in a row
    under it each from a new wallet, and how many of its buyers never traded or stand in a pair;
  - the pool's exams and stages still able to pass, the payouts they would be if all passed, and
    whether the money free of what is owed, held back and kept for stops covers them;
  - mirrored accounts: positions held against positions, not fills against fills. Each account's net
    position per asset is a step in time. A moment counts when another account of the factory holds
    the opposite position on the same asset, at least half as large. An account mirrored for 60 % of
    the time the stage (or the exam) held anything, and an hour at least, is named, with the share
    and the fills that opened each mirrored stretch. A pair built in pieces, an hour apart, or at 60 %
    of the size is caught this way; two traders in the same trend, or in different assets, are not.
    An account's positions stop counting when it ends;
  - the opposite side spread over many accounts: the same measure against the sum of the positions of
    several accounts, each too small to be a mirror alone. A sum over everyone would catch a busy market,
    where some account is always on the other side by chance. So only accounts given to it count: each
    spent 60 % of its own time with a position against this one, an hour at least. A trader in the market
    is mostly elsewhere in time. Found when at least two of them hold opposite positions adding up to half
    the stage's, at the same time, for 60 % of its time. The finding lists each account's part and the
    senders of USDC that funded several of them. It is never a stop on its own. It cannot tell apart
    accounts that each happened to trade once, against this one and nowhere else: a market of one-trade
    accounts would look the same;
  - the result counted again from the account's own events, as PaperExam counts it:
    - each step's cash from its capital and its fills, which `StepPassed` and `Passed` must carry
      exactly (a step passes only flat);
    - each day's snapshot from that cash and the oracle prices of its block;
    - each fill against the oracle price of its block: at it, for a fill at the oracle; never better
      for the trader, at the book;
    - each fill's cost against the terms;
    - the oracle price of a fill's block against the blocks on each side, where one price far from
      both, which agree, is an outlier;
    - given `--venue-prices`, the oracle against the venue's price of that minute. That file is
      Hyperliquid's own candles (info API, `candleSnapshot`, interval 1m), read from disk and never
      fetched by the script.

    The oracle prices are read from the chain at their blocks. Where the node keeps no state that far
    back, `--prices` stands in. A pass in three fills or two days at most on the largest size is said
    as a profile, not counted as a reason.

  - how often the pool's exams pass, against the model. For the pool and the factory, over all time and over
    the last 30 days of sales, it counts exams sold and passed, stages started, and payouts reached. It also
    splits them by code, by asset, by the hour and weekday of the pass, and counts passes in very few fills.
    A rule of the exam that is wrong still counts up on chain; it shows only as more passing than the
    model said. Under 30 sales a share is "few", not judged. Over that, it is "over" only when its exact
    (Clopper-Pearson) lower bound, 99 % sure, is above the model times the alarm. The shares (16 in 100
    exams, 2.7 in 100 payouts) and the alarm (x1.5) are a draft until the CFO's file (`--thresholds`). A
    pool over the alarm marks every payout of the pool while it holds. The trader is not to blame, so the
    approval stays and the choice is the signer's;
  - the timing of the fills, judged against chance:
    - the filler's choice: how often a fill landed in the best block of its window for the trader, against
      how often best blocks occur in the window;
    - the lead on the feed: how often the oracle moved the trader's way in the block right after the fill,
      against a coin.

    Each is flagged over 8 fills at least, when chance gives as much with probability under 1 %. The oracle
    of every block of each order's window is read for it.

  Its verdict is "clean", or "suspicious" with the reasons, with the payout's three times: its claim,
  72 hours after it, and the ceiling 7 days after it, past which it is paid unapproved. The call to
  sign is written out as text with its calldata:
  - `stopPayout`, reason 1 or 2, when there is a pair or a mirror with two opposite fills, since the
    pool takes no such stop without two fills;
  - `approvePayouts` otherwise.

  A result that does not count up comes with a draft of a stop of reason 3, a result disputed. The
  draft names the fills and leaves out the hash of the analysis, which a person writes. Such a payout
  is offered nothing to approve: its `action` is `hold` and its call is empty. The operator's page
  shows no Approve button for it, only "Approve anyway" behind a warning the operator confirms
  (`approveAnywayCall`). Something else suspicious, with the result counting up, is `decide`: the
  approval is offered, and the choice is the signer's. The approval is
  the design of 13 November and not on main yet. The review cannot see:
  - a pair on another factory or another venue;
  - accounts under wallets nothing links;
  - USDC that arrived before the deployment's block;
  - a flaw in the exam's own rules, until enough exams have passed to show it in the pass rate;
  - a filler or a trader with an edge too small or too rare to stand out from chance over the fills there
    are.

- **The host**: `bootstrap.sh --sim-instance` makes the instance without a gateway, and `install.sh` enables
  `colosseum-multi-keeper@<name>` with the filler's wallet, after checking it and the checker's against the
  record (docs/HOSTING.md, "A simulated pool's keeper"). No gateway, no instance keeper, no nginx route.

- **The page's check**: `test_page_recordedChain` (test/sim/SimPool.t.sol) runs a pool on the simulator and
  records every event of it a page reads, as the node gives them, into `app/tests/fixtures/sim-page-chain.json`,
  and fails when the file is not what the contract emits (`SIM_PAGE_FIXTURE=write` rewrites it).
  `app/tests/simpool-chain.test.mjs` decodes those logs by the app's ABI lines, draws the page with
  `app/lib/simpool.js` and checks it the way a stranger would: the total paid is the sum of the payment events,
  each with its transaction, and the contract's own `paidTotal`; the stopped and the held payouts are apart; and each
  of two groups' codes shows what its sales brought and what its owner was sent or, refused by the token, had set
  aside, worked out again from the events and the share; the chain ends with the platform's closer closing the
  pool long before its 180 days, and the page shows `lockedUntil` (the creation plus 180 days) and `closedEarly`.
- **Counts for a points formula**: `activityCounts` (app/lib/simpool.js) gives, per trader and per code, what the
  chain says each did, with no points and no weights: purchases, what they cost and brought, exams and stages
  passed, breached or expired, steps passed, fills and the UTC days with a fill, and the payouts written, paid,
  held, stopped, lifted, objected to and refused. Exams and stages keep their own events, so it reads them in a
  second pass over the logs of each account the pool sold or started (`accountsToRead`); an account not read is
  listed in `unread` and its trader's and code's counts are marked incomplete. The recorded chain carries those
  logs too (`examEvents`), and `app/tests/simpool-chain.test.mjs` holds the counts to it.

- **The operator's page**, `app/lib/operator.js`: reads handed in and calls built, with no reads and no keys of its
  own. It gives:
  - the payouts waiting (`awaitingApproval`): each with its claim, its 72 hours, its 7-day ceiling, whether it is
    approved and when it will be paid as things stand, the most urgent first;
  - the creator's standing request to withdraw (`withdrawRequest`): its amount, when it can be carried out and
    when it lapses;
  - the pool's state (`poolState`): the money free of what is owed, held back and kept for stops, what the creator
    may take now, the live exams and stages, and the term in `termNote`'s words.

  Each call is built with its calldata, checked as the pool would check it, and names who signs it:
  - `approvePayouts`: the approver, a Ledger key, with one signature;
  - `requestWithdraw`, `withdraw`, and `close` after the term: the creator's Safe, with two signatures in the Safe's
    own app;
  - `stopPayout`, `cancelWithdraw`, and an early `close`: the platform's checker and closer.

  A payout's review is the JSON `ops/payout_review.py --out` writes, schema `payout-review/1`, one file per payout.
  The page shows a review only where it is of this very payout and names the call the page itself would build
  (`reviewFor`). It never signs what a review hands it. The approval and the request to withdraw are designs not on
  main yet; the names are theirs.

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
