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
  `TermsBook`. The factory checks every asset against its list and every leverage cap against the
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
- **The payout.** `claimPayout(stage)` once the stage has passed, by anyone: 8 % of the size, due 48
  hours later. `pay(max)`, by anyone, pays what is due in the order it came due while the pool's USDC
  covers the next; a payout the pool cannot cover yet waits (`PayoutQueued`) and is paid first from
  the prices that come in. One stage, one payout: the stage has one step and its pass ends it.
- **A stop.** Until a payout is due, the factory's checker may stop it once, with a reason: 1,
  opposite positions with another account of the factory; 2, that and wallets linked. The call
  names the other account, the transactions of the trades (at least two) and, for reason 2, the
  transactions that link the wallets. A stopped payout's money stays in the pool. The checker lifts
  a stop that was wrong (`liftStop`), and the payout is paid as any other.
- **The creator.** `withdraw(amount)`: only what stays above three payouts of the largest size and
  above every payout owed. 0.5 % goes to the platform; of the rest, what is above the creator's
  principal is profit, and the partner gets its share of it. `addToPot` puts in more principal,
  which comes back first without the share. `setPartnerShare` moves the share within 40 to 60 %, for
  later withdrawals. `close` stops sales; `finish`, once nothing runs and nothing is owed, sends
  what is left, the principal first.

## The platform's numbers

Constants of the factory, the same for every pool: 1 HYPE to open (`CREATION_FEE`); 0.5 % of each
exam's price and of each withdrawal (`FEE_BPS`); a payout of 8 % of the size (`PAYOUT_BPS`), with no
fee on it; two payouts of the largest size locked at opening (`LOCKED_PAYOUTS`); three kept while
the pool is open (`FLOOR_PAYOUTS`); 48 hours before a payout is paid (`PAYOUT_DELAY`); a fill
window of 1 to 60 blocks; a partner's share of 40 to 60 % (`MIN_PARTNER_BPS`, `MAX_PARTNER_BPS`).

## What anyone can check

Every exam and stage is a paper exam: each order, fill and price is in the chain's history, and
each fill says how many blocks after the first possible one it came. The page of a pool reads
`PayoutPaid` (the transaction of each payout is its proof), `PayoutStopped` with its reason,
account and transactions, and `StopLifted`; each of the last two carries the pool's counts of
stops, lifts and payouts.

## Weak points we know of

- **The platform is the filler, the checker, and in its first pools the creator.** The filler picks
  the block inside the window; the checker stops payouts whose money stays with the creator. Neither
  is held by the contract, only by what shows: `late` on every fill, the reason and transactions on
  every stop, the counts, and a public checking script. A creator is never the filler or the checker.
- **The checker's evidence is not checked by the contract.** It checks that the other account is
  one of the factory's and that there are transactions; whether they show what the reason says is
  for anyone to read.
- **A pass comes faster than prices.** Payouts then wait in order. A trading group that trades one
  signal passes together; its creator can lock more with `addToPot`.
- **No depth, no funding, no resting orders**: those of the paper exam (`docs/PAPER-EXAM.md`).

## Tests

`test/sim/SimPool.t.sol`, 36 tests on the hyper-evm-lib simulator (EIP-3009 on a stand-in with
FiatToken V2.2's interface and words), and 78 mutations (`SM1`–`SM78` in `scripts/mutations.py`),
each caught by a named test:

```
forge test --match-path 'test/sim/*'
python3 scripts/mutations.py $(seq -f 'SM%g' 1 78)
```

## Running it

- **The keeper**, `ops/sim_keeper.py` (the host's `multi_keeper` unit hands it a record with a
  `SimPoolFactory`). A fast loop reads `Placed` every block while anything is live, about 3 600
  `eth_getLogs` an hour, and sends each order's `fill` from the filler's key for the block its window
  opens; a fill waits for its receipt, so two orders in one block go one after the other. A slow pass
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
  `deployments/plans/sim-first-pool.json`: 5 000 for 45 and 10 000 for 85, 1 600 locked, at the book
  with a ceiling of 150 bp, a fill window of 10 blocks until `--measure` says otherwise.

- **The checker**, `ops/sim_check.py`: the open script behind every stop. For each payout still
  waiting it matches every fill of the paid stage with the fills of every other exam and stage of the
  factory: the same asset, opposite sides, at most 10 minutes apart, sizes within 20 %. A match is
  reason 1; with the two traders funded by one address, or one paying the other (USDC transfers since
  the deployment), reason 2. It stops a payout at least 5 minutes before it is due, once, from the
  factory's checker key, and lifts its own stop with `--lift POOL:ID`. Without `--wallet` it only
  reports. Anyone can run it on the same blocks and get the same pairs. It reads this factory's
  accounts only, not the MultiPool's or the other deployments'.

## Not done yet

- The host unit's trigger on `SimPoolFactory` in `ops/host/install.sh`, once the unit is in main.
