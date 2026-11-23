# Design: the simulated pool

`src/sim/SimPool.sol` and `src/sim/SimPoolFactory.sol`, with three libraries the pool calls,
`src/sim/SimPoolStops.sol`, `src/sim/SimPoolSetup.sol` and `src/sim/SimPoolCodes.sol`. Not deployed yet; no
external audit.

**Why libraries.** A contract's code may be at most 24 576 bytes (EIP-170, in force on HyperEVM's
Cancun rules), and tests run without that limit. With its daily payout limit the pool came to 24 790. The
checks and events of the four stop functions (`stopPayout`, `liftStop`, `objectStop`, `finalizeStop`)
and the copy of the plan at creation (`initialize`) now run in libraries with external functions, which
the pool calls by DELEGATECALL: in its own storage, with its own caller, emitting its own events. The pool
keeps its interface, its counters and its money (`owed`, `reserved`, `stoppedCount`, `liftedCount`,
`head`, the principal); what the code does is what it did. The codes (setting one, a sale by one, what each
accrued and its claim) run in `SimPoolCodes` the same way since codes have no limit (16 November). `SimPool`
23 805 bytes, `SimPoolCodes` 2 997, `SimPoolSetup` 5 577, `SimPoolStops` 3 845 (`forge build --sizes`, 16
November). `scripts/size-check.py` holds every contract and library of `src/` to the limit.

## Why

A pool of many traders (`MultiPool`) gives whoever passes its exam a funded account on its owner's
capital. A simulated pool has no capital. Both stages are paper exams (`PaperExam`): the exam, and
then the stage on the same size. Whoever passes the stage is paid out of the pool's USDC, which is
the prices traders paid for exams and what the pool's creator locked when opening it. Most exams
fail, and their prices pay for the ones that pass.

## What it does

- **Opening.** `SimPoolFactory.create(plan)` with exactly the factory's fee sent along (`creationFee`: 1 HYPE on
  mainnet at the deploy, 0.1 HYPE on testnet by default; read it from the factory before opening), which goes to the
  platform, and
  the plan's `locked` USDC, taken from the creator: what the creator chooses, at least two payouts of the
  smallest line. The plan's `on` says which lines are switched on at the start, bit `i` for line `i`: only
  lines whose two payouts the lock covers; the others start off. The first pool locks 4 000: 5 000, 10 000 and
  25 000 on, 50 000, 100 000 and 250 000 off. The plan sets the menu (up to 24 lines, each a size and its price), each line's
  tier (`tiers`, one for each line: the loss limit, the target and the kind of loss line, from the start or
  trailing to it, of both its stages), the rules and terms both stages share otherwise, how many payouts the
  pool settles at most in a day (`payoutsPerDay`, 1 to 8), how long a pass may wait before its stage starts,
  and the hash of the pool's terms written out in a `TermsBook`. Two lines may share a price: a signed
  purchase names its line. A line's exam and stage start on the plan's rules and terms with its tier in them
  (`maxDrawdownBps`, every step's target, `drawdownKind`) and its size as `capital`. The pool keeps the plan's
  rules and terms once, and for each line only its size, price and tier, in one word of storage with the count
  of its exams and stages not yet done; `SimPoolSetup.start` puts the tier in when an exam or a stage starts.
  `tiers()` reads them back, in the order of `menu()`. Opening a pool so costs one word of storage a line: the
  first pool's plan of 24 lines (six sizes on four tiers), BTC, ETH and SOL, takes 1 694 229 gas as a
  transaction, base and calldata counted, and 24 lines with an exam of four steps 1 701 648, both under
  two thirds of a small HyperEVM block of 3 000 000 gas, which a creator's ordinary transaction goes to
  (`test_gas_createOf…FitsASmallBlock`, the gate in CI; "Fast block duration is set to 1 seconds with a 3M gas
  limit", https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/dual-block-architecture,
  read 16 November 2026). Before the rules were kept once, twelve lines took 6 972 609. The
  factory runs PaperExam's own checks on both stages once, on the first line's size and tier, and on every
  line PaperExam's bounds on what the line sets (a size not zero, a loss limit under 100 %, a target of at most
  100 %), the same answer as PaperExam's at that line's start (a fuzz test), and checks every asset against its list and every leverage cap against the
  asset's, that both stages end (a deadline and an idle limit), that the stage is one step, and
  that a fill window is 1 to 60 blocks. Anyone may open a pool; the filler, the checker and the
  closer may not.
- **Codes.** `setCode(code, partner, bps)`, by the creator: `code` is the keccak256 of a trading
  group's code, `partner` the address the group's owner gave, `bps` its share, 0 or 40 to 60 %. A pool
  has as many codes as its creator sets: there is no limit (Alex, 16 November: a partner with a network
  of a thousand authors gets a code for each), and no call goes through all the codes. The pool keeps no
  list of them; every code is in a `CodeSet` event. A code is public by nature: its owner hands it to a whole
  channel, so nothing hides it, and whoever buys with it is the owner's buyer. A code can be changed,
  or taken off sale with a zero partner, until a sale names it; from then on its partner is fixed
  and its share can only be raised, within the bounds (`CodeUsed` for anything else), so an owner
  who has brought traders does not depend on the creator. A raise (an owner's steps of 40, 50 and
  60 %, CFO, 13 November) counts from the next withdrawal on; what the old share accrued stays. Each
  code's part is its own (profit x its `brought` / `brought` x its `bps`), so raising one code's share
  takes nothing from another's.
- **A code for two.** `setSplitCode(code, partner, bps, author, authorBps)`: a code with an author besides
  its partner, say a blogger of the group's network, and the author's part of the code's share
  (`authorBps`, 1 to 9 999 bps of it); the partner gets the rest. The bounds of 40 to 60 % hold the code's
  share as a whole, not each part: a code of 40 % split half and half gives 20 % each. Partner, author and
  split are fixed by the first sale, as a code's partner is; after it only the share rises, by
  `setSplitCode` with the same three (`setCode` on such a code is `CodeUsed`: it names no author). A code
  for one is a code with no author (`setCode`), and works as before. An author is never the partner's own address
  (`BadShare`): that code is a code for one.
- **Selling.** `buy(line, code)`, with a code the creator set, or `NoSuchCode`. No seats and no
  share of the pool limit a sale. 0.5 % of the price goes to the platform, the rest to the pool,
  and that rest counts as what the code brought (`codeOf(code).brought`, and `brought` for all
  codes). `ExamSold` names the code and its partner. The buyer gets a paper exam whose orders only the factory's
  filler fills inside their window, at the book when the plan says so: never better for the trader
  than the oracle price and at most the plan's `maxWorseBps` worse (`PaperExam.initializeWithFiller`).
- **Lines on and off.** A line sells only while its creator has switched it on and the pool has two of its
  payouts free (`LOCKED_PAYOUTS`, the CFO's K of 16 November): `free()` is the pool's USDC less what it owes,
  holds, keeps for recent stops and keeps for its passed exams and running stages (`staged`). Otherwise `buy` and
  `buyWithAuthorization` revert before any money moves, `LineOff` or `NotCovered(short)` with how much is
  missing. A line short of money stays on and sells again by itself once the money is back. Only the creator
  switches a line (`setLine(line, on)`, `LineSet` each time): off at any time, on only while the pool is open
  and has the two payouts free (`NotCovered(short)` otherwise). No line switches itself on, and a switch acts on
  new sales only: exams and stages sold keep their rules. `lineState(line)` reads whether the line is on,
  whether it sells now, whether the creator may switch it on now, and how much the pool is short of that;
  `onMask()` holds the switches, bit `i` for line `i`.
- **Selling by signature.** `buyWithAuthorization(trader, line, code, salt, validAfter,
  validBefore, v, r, s)`: the trader signs a USDC transfer of the line's price to the pool (EIP-3009,
  Circle's FiatToken V2.2 on HyperEVM and its testnet) and needs no gas; anyone sends it, a relayer
  of the platform's or a partner's, and pays the gas. The signed nonce is
  keccak256(abi.encode(code, line, salt)), so the code and the line are in the trader's signature and
  whoever sends it can name neither another code nor another line at the same price. The token checks the signature against the line's price, the time and the
  nonce, and takes it only from the pool it names. Same price, same fee,
  `ExamSold` with `bySignature`. A token without EIP-3009 refuses with `NoAuthorization`; `buy`
  works on any.
- **The pass.** The exam tells its pool when its trader passes it (`PaperExam.graduate` calls `examPassed()`,
  `ExamPassed`; `passedOf(exam)`): from that moment its payout is in `staged`, kept from the creator's
  withdrawal and out of what is free for new sales and for switching lines on (A1 of the pre-deploy check,
  16 November). Nobody has to call anything for it. A pass not funded within the window is released by
  anyone (`release`), and its payout is free again, as a stage's that broke a rule or ran out of time.
- **The stage.** `fund(exam)`: the trader, within the plan's window after the pass, starts a second
  paper exam on the same size. Nothing moves, and `staged` does not change: the stage carries on the payout the
  pass put in.
- **The payout.** `claimPayout(stage)` once the stage has passed, by anyone, and by the keeper in its next
  slow pass (every 30 seconds): 8 % of the size. It is due 72 hours after the claim once the pool's
  approver has approved it (`approvePayouts`, `PayoutApproved`), and 7 days after the claim whether
  approved or not (`PAYOUT_HARD`, `payoutHardAt`): the trader's money never waits on the approver
  longer. The creator names the approver (`setApprover`, `ApproverSet`; zero for none, and every
  payout then waits the 7 days); approving sets neither the amount nor the trader. A batch of
  approvals passes over a payout that was paid, stopped or approved while it was on its way, and
  approves the rest. The keeper sends
  `pay` in its first pass after a payout is due, so an approved payout is paid 72 hours and about a
  minute after the claim, or later: the 72 hours and the 7 days are the earliest a payout may be paid,
  and it is paid by a `pay` call, in the order of the claims, when the pool holds enough. `pay(max)`, by
  anyone, looks at up to `max` payouts from the head, settled ones included, in the order they were claimed and pays each that is due
  while the pool's USDC covers it; one still waiting for its approval keeps its place: `pay` moves
  on to later ones, but pays one only if the money covers every such payout before it too, and the
  queue's head stays at the first unpaid; a payout the pool cannot cover yet waits (`PayoutQueued`) and is paid first from
  the prices that come in. A payout the token refuses to send (say the trader's address was blocked)
  is set aside for the trader (`PayoutHeld`), who takes it to any address with `claimHeld`, also
  after the pool has finished; the queue moves on. One stage, one payout: the stage has one step and its pass ends it.
- **The daily limit.** A pool settles at most `payoutsPerDay` payouts in any 24 hours (`PAYOUT_DAY`), a
  number the creator sets at the opening, from `MIN_PAYOUTS_PER_DAY` (1) to `MAX_PAYOUTS_PER_DAY` (8) of the
  factory; our first pool, two (CFO, 13 November). What follows is told for two. `pay` counts every payout that leaves the queue, paid or set
  aside for a trader the token refused, approved or gone out at its 7 days: if the limit counted only
  approved payouts, not approving would pass it. A payout that may be paid, with the day's two already
  settled, keeps its place (`PayoutWaitsLimit` with the time from which it may be paid), stays owed,
  and so does every payout after it: `pay` stops there, as it stops at a payout the pool cannot cover,
  so the order holds. The pool keeps the times of its last two settlements; one more goes out once the
  older is 24 hours old, so no 24 hours see three, and no history is walked. `payRoom()` says how many
  `pay` may settle now, `nextPayAt()` from when one more. A wave of five payouts claimed together and
  approved goes out two at their 72 hours, two a day later, one the day after. So the 7 days are the
  latest a payout waits for its approval, not the latest it is paid: the third and fourth of five
  unapproved payouts are paid 8 days after their claim, the fifth 9. `claimHeld` and `finish` are no
  payouts of the day. So the limit is on what `pay` settles, not on every transfer of a day: what was
  set aside for a refused trader on earlier days leaves when that trader claims it. Whatever the rules
  let through, `pay` settles at most two payouts in any 24 hours, of the largest size at most: 40 000 in
  the first pool if its creator switches 250 000 on, which sells only while 40 000 are free (the CFO, 13
  and 16 November 2026). A payout over the day's two keeps its place and its money: the creator's withdrawal
  leaves it in the pool (`test_dailyLimit_aPayoutOf20000…`).
- **A stop.** Until a payout is paid, approved or not, the factory's checker may stop it once, with
  a reason: 1, opposite positions with another account of the factory; 2, that and wallets linked;
  3, the result is disputed (a flaw of the exam, the oracle or the filler); 4, prohibited play: a
  series of exams bought by one person or by linked wallets, each traded at the edge of its risk to
  pass by luck, or one account hedged by another. For 1 and 2 the call
  names the other account, the transactions of the trades (at least two) and, for reason 2, the
  transactions that link the wallets; for 3 it names no account, at least one transaction that shows
  the flaw, and one hash, the keccak256 of the published text of the dispute; for 4, at least two
  transactions that show the play, one hash, the keccak256 of the published analysis, and the other
  account of the factory if there is one (a hedge), or none (a series, whose purchases the transactions
  name). Like 3, a stop of reason 4 is written by a person from a published analysis, never sent by
  `ops/sim_check.py`. Every stop is the same `PayoutStopped`, and the pool keeps its reason by the
  payout (`stopReasonOf(id)`, zero if never stopped, kept after a lift). The reason moves no money of
  its own: whatever the reason, a stop keeps the payout's money as follows. A stopped payout's money stays in the pool, set aside from the
  stop (`STOP_RESERVE`, `reserved`) until the factory's operator lifts the stop within 14 days, or until anyone
  makes it final after them (`finalizeStop`, which may come later than the 14 days, never sooner), as
  a held payout's is: the creator does not
  withdraw it, it is no profit for the partners' shares, and `pay` pays no other payout out of it.
  Within those days the trader may object on chain (`objectStop`, the keccak256 of the objection's
  text, `StopObjected`), and the factory's operator, our Safe, lifts a stop that was wrong (`liftStop`; the checker
  stops, it does not lift; `NotOperator` for anyone else): the payout is due
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
  the closer only cancels: it changes neither the amount nor where the money goes (for the
  creator's terms: the platform may cancel a withdrawal request; it never moves the money). There
  is no other way for the creator to take money out before `finish`. `withdrawable` is only what
  stays above every payout owed, held or kept for a recent stop and, until the pool is finished, above
  `floor()`: the greater of the payouts of every exam passed and not yet funded or released and every stage
  still running, each by its own size (`staged`: up at the pass, down when the stage's payout is claimed or the
  pass or the stage is released), and three payouts of the
  largest line with an exam or a stage not yet done, or switched on and selling (CFO, 16 November, item 3).
  A trader with a live exam of 250 000 keeps 60 000 in, its line switched off or not; with nothing on and
  nothing live, nothing is kept above what is owed, held or kept for stops. A trader's payout stays in the pool from the pass for as long as the stage runs; the
  creator takes profit only above it (CFO, 13 November). `staged` changes what may leave, not what
  profit is: the codes' shares are of the profit a withdrawal takes, as before. 0.5 % goes to the platform. Profit is the
  pool's money above what the creator put in, after what the pool owes, holds and keeps for recent
  stops; a withdrawal takes it first. Of the profit a withdrawal takes, less the platform's part,
  each code accrues its `bps` in the part its code brought of all the pool took:
  profit × (the code's `brought` / `brought`) × `bps`. A code with no share leaves its part to the
  creator. The withdrawal does not send the codes' parts: it moves one number (`accrual`, what a unit of
  a code's weight, its `brought` times its `bps`, has accrued) and keeps all the codes' parts in the pool
  (`sharesDue`), whatever their number. Each code counts what it accrued when it is next touched (a sale, a
  raise, a claim; `ShareAccrued`), and `claimShares(codes)` sends each named code's part to its partner,
  and on a code for two its author's part to the author, each apart (`SharePaid`). Anyone may call it, for
  any codes, also after `finish`: the money goes to each code's partner and author only, and its work is the
  number of codes named. What the codes accrued is kept like what is held: out of `free()`, `withdrawable()`,
  the profit of a later withdrawal, what `pay` may spend and what `finish` sends the creator. A code's part is
  its share to the unit (the step of `accrual` is rounded up). A code is counted whole units, and the fraction of
  a unit left stays the code's and counts the next time (`frac`): a code is counted the same whenever it is
  touched, and a claim by anyone between two withdrawals takes nothing from it. What is kept for the codes is all
  they accrued together, to the fraction (`accruedScaled`), rounded up, less what they got: never less than the
  codes may claim. Once every code has taken its own, what stays kept is the codes' fractions and that rounding
  up: at most as many units (0.000001 USDC each) as there are codes with a share. With 2 000 codes, three
  withdrawals and `finish`, 1 603 units stay (`test_codes_twoThousand_noLimit_withdrawFinishAndAPurchaseFitTheGate`). `shareDue(code)` reads what a code may claim now, its partner's part and its author's;
  `codeOf(code)` what it was counted and what it got (`partnerDue`, `authorDue`, `partnerGot`, `authorGot`, `frac`).
  A partner or an author whose address the token refuses has its part set aside, `heldOf` and `claimHeld`
  as for a refused payout (`SharePaid` with `sent` false says which). So is the creator's part of a
  withdrawal or of `finish` (`Withdrawn` with `sent` false): a refused creator holds up neither the
  withdrawal, nor `finish`, nor any code's share.
- **When shares go out.** The contract knows no days. Alex's schedule, once a week from Monday to
  Wednesday (16 November), is the signers' and the recipients' to keep: the creator's withdrawal is asked
  a week ahead and carried out in its window, and `claimShares` sends what the codes accrued whenever
  anyone calls it, the platform's keeper on that schedule or a partner or author on their own. `addToPot` puts in more principal,
  never shared, which comes back once the profit above it has been taken, or at `finish`. It is the
  only way to put the creator's money in: USDC sent straight to the pool's address is not principal.
  It covers payouts as any money of the pool does, and what of it is profit when a withdrawal is
  carried out (`_profit`: above what the pool owes, holds and keeps, and above the principal) is
  shared with the codes' owners as any profit taken; below that it shares nothing. A share accrued to a code is
  never taken back: if later payouts take the pool's money below what the creator put in, the
  creator bears it, and shares start again only once the principal is whole. `close` stops sales; `finish`, once nothing runs, nothing is owed and no stop
  can still be lifted, and a week after the close (`closedAt` + `WITHDRAW_DELAY`, `NotReady` before),
  sends what is left, the principal first, and ends a standing request. The week keeps `close` and
  `finish` from being a withdrawal of everything in one block.
- **The term.** A pool runs at least 6 months, 184 days, from its opening (`MIN_TERM`, `lockedUntil`;
  184 days is the longest half-year, so the six months hold whatever the day it opens). Before
  then the creator can neither `close` it (`Locked`), so it sells all that time, nor take back what
  it put in: `withdraw` gives the profit only, never the principal, the locked payouts or what
  `addToPot` added. Only the factory's closer, a platform key the operator names (`setCloser`), may
  close a pool sooner (`closedEarly`, `ClosedEarly`). That changes nothing about where the money
  goes: `finish`, by the creator or the closer, still waits until nothing runs, nothing is owed and
  no stop can be lifted, leaves what is held for its traders and partners and what the codes accrued,
  accrues each code its share and sends the creator the rest; the closer gets none of it. From `lockedUntil` on, the creator closes and finishes as before.

## The platform's numbers

The fee to open a pool (`creationFee`) is set when the factory is deployed: 1 HYPE on mainnet and 0.1 HYPE on testnet
by default, where a rehearsal runs on test HYPE. The factory's operator may change it at any time, with no delay
(`setCreationFee`, event `CreationFeeSet` with the old fee and the new), to any amount above zero: there is no cap,
and opening is never free (a fee of none reverts `BadFee`, at the deploy as at a change). The guard against a slip of
a zero is the script's: `ops/deploy_sim.py` prints the fee in HYPE and in wei, refuses none before anything is sent,
and on mainnet sends a fee above 10 HYPE only with `--confirm-large-fee`. A pool already
open paid its fee once and is not touched by a change. A creator reads the fee from the factory before opening:
`create` compares the HYPE sent with `creationFee` as it stands when the transaction runs. If they differ, `create`
reverts (`WrongFee`): the fee is not taken, though the creator pays the gas of the reverted transaction. A fee
changed and changed back before the transaction runs matches again. Constants of the factory, the same for every pool: 0.5 % of each
exam's price and of each withdrawal (`FEE_BPS`); a payout of 8 % of the size (`PAYOUT_BPS`), with no
fee on it; two payouts of a line free for it to be switched on or to sell, and at least two of the smallest locked at opening (`LOCKED_PAYOUTS`); three of the largest line live or selling kept while
the pool is open (`FLOOR_PAYOUTS`); 72 hours before a payout is paid with the approver's approval (`PAYOUT_DELAY`), 7 days
without it (`PAYOUT_HARD`); 14 days in
which a stopped payout's money stays set aside and the stop can be lifted, and after which it can be made final (`STOP_RESERVE`); 184 days a
pool runs before its creator may close it or take back its principal (`MIN_TERM`); 7 days from a
request to withdraw to its money, and from a close to `finish` (`WITHDRAW_DELAY`), and 3 days to
carry out a ready request (`WITHDRAW_WINDOW`); a fill
window of 1 to 60 blocks; a code's share of 0 or 40 to 60 % (`MIN_PARTNER_BPS`, `MAX_PARTNER_BPS`), and
as many codes a pool as its creator sets.

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
- **A stop is carried out only in a block where the oracle stands at it, and only by the filler inside its
  window.** A reach no transaction of the filler's lands in leaves no trace on chain. The keeper looks at every
  block with nothing in the way and says each miss at once ("The stops"), but a reach of a block or two can
  still go by, and so can every stop while the filler's one key or machine is down.
- **No depth, no funding, no resting orders**: those of the paper exam (`docs/PAPER-EXAM.md`).

## Tests

`test/sim/SimPool.t.sol`, 129 tests on the hyper-evm-lib simulator (EIP-3009 on a stand-in with
FiatToken V2.2's interface and words), and every `SM` mutation of `scripts/mutations.py` (on this branch
SM1 to SM166, SM170 to SM177 and SM180 to SM182: the delay, the window, the less of the two, finish's week, payout
approval and its 7 days, the running stages' payouts, the 184 days and a used code's raise among them), each
caught by a named test. The second command lists them from the file, so a new one is never left out:

```
forge test --match-path 'test/sim/*'
python3 scripts/mutations.py $(grep -oE "[\"']SM[0-9]+[\"']" scripts/mutations.py | tr -d "\"'")
```

`test/sim/SimPoolInvariant.t.sol` holds the pool's money to its books whatever is called in whatever order: a
handler buys with codes, directly and by signature, sets codes for three groups (40 %, 60 % and no share), now and
then for two (an author with 20 % or 50 % of the code's share), tries to set them again after a sale, has anyone
send some codes what they accrued (`claimShares`), passes and fails exams and stages, claims, stops, lifts and pays payouts, has
the trader object to a stop and anyone make it final after its 14 days, has the token refuse a trader or a group's
owner, an author or the creator, claims what was held, asks to withdraw, withdraws (at once now and then, which must be refused, otherwise
from the week on to past its window), cancels a request (the creator or the closer; a stranger refused) and adds
for the creator, closes (the creator from the term on, refused
before it; the closer early) and finishes (by the creator or the closer), jumps past the 184 days now and then
(48 runs of 120 calls in CI,
with `fail-on-revert`: a handler call the pool refuses unexpectedly fails the run instead of passing unseen).
Fourteen invariants: every dollar in and out accounted for; what is held always there and equal to the refused
payouts, partners' and authors' parts and the creator's less the claims, `reserved` the stopped payouts, and `pay` spending neither; `paidTotal` the sum of the
payments, and `pay` sends exactly those; a withdrawal never below three payouts above what is owed, held and
reserved, nor past `withdrawable`, and before the term not past the profit; one purchase, at most one payout; `owed` the due payouts; stops less lifts are
those standing and those made final; nothing due behind the head of the queue; `live` the accounts not done, a
pool finished only with nothing owed, running or reserved, and keeping only what is held and what the codes
accrued; `brought` each code's sales and all of them together, a code a sale has named keeping its partner, author,
split and share, each code's accrued share its part of every withdrawal worked out apart from the pool to a unit a
withdrawal, what it got what `claimShares` sent or set aside, and what all codes may claim kept for them and in the
pool; and each withdrawal's shares: the platform's 0.5 %, what the codes accrued never less than their parts of the
profit less that 0.5 % nor above 60 % of it and a unit, the creator the rest (sent, or set aside if the token
refuses); before the term, until the pool is finished, `principal` all the creator
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
  so two orders in one block go one after the other. The same loop keeps every running exam's stops and
  takes from their events and tries each armed one once a block by a call that changes nothing, sending
  `fillTrigger` when the exam would take it; with `--stops loop` they are a loop's of their own instead,
  in a thread beside this one ("The stops", below). A slow pass
  every 30 seconds finds pools, exams and stages and makes their calls: the day's snapshot, `breach`,
  `expire`, `release`, `claimPayout`, `pay`. It reads the pool's `payRoom()` and sends `pay` only for
  as many payouts as the daily limit has room for: none while the day's two are settled (it logs
  `pay_waits_limit` with `nextPayAt()`, once for each change), though it still moves the head past
  settled payouts. One `pay` is told to pay at most the day's two payouts and to look
  at most 192 from the head (`PAY_PER_CALL`, `SCAN_PAYOUTS`), so that it fits a small block of 2 000 000
  gas: measured from cold storage, two payouts paid past 94 waiting for approval, `pay(96)`, take
  730 903 gas with the transaction's own, each further payout looked at 5 390, and `pay(192)` takes
  1 248 428 (`test_pay_theKeepersFullBatch_fitsASmallBlock`: two traders with no USDC, none, one or both
  refused by the token). What is left is paid on the following days, two a day. `pay` counts from the
  head, so an approved payout more than 191 places behind a head that waits for its approval is out of
  the keeper's reach. 192 is the keeper's limit, kept to what was measured, not a small block's: a
  longer look may still fit one, by the queue and the token. The keeper logs `pay_out_of_reach`, and
  the payout is paid once the head moves, when the approver approves the ones in front or their 7 days
  come, which is before its own; anyone may also send `pay` with a larger `max`, in a big block if a
  small one does not hold it. `--measure EXAM --trader-wallet K` places orders on the
  trader's own exam and writes how many blocks late each fill landed, p50 and p99: the number the
  plan's fill window is set from.
- **The relayer**, `ops/sim_relay.py`: a signed purchase in, `buyWithAuthorization` out, from a key
  that pays the gas. It refuses before spending any: another factory's pool, no such line, a code the
  pool has not set, a signature ending within 30 seconds, a used nonce, a call the pool would refuse. `--serve PORT` listens on
  127.0.0.1 for `POST /buy`.
- **The deployment**, `ops/deploy_sim.py`: `deploy` puts PaperExam, the pool's two libraries (`SimPoolStops`,
  `SimPoolSetup`), SimPool linked to them (the record names all three), a TermsBook and the
  factory on the network (testnet unless named, see Networks) with Circle's USDC and the fee to open a pool set
  in it (`--creation-fee` in HYPE: 0.1 on testnet and 1 on mainnet unless named, above 10 HYPE on mainnet only with
  `--confirm-large-fee`; the plan prints
  it with the network's name, the factory is read back for it before anything else is sent, and the record keeps
  the fee at the deploy as `creation_fee_at_deploy_wei` and `creation_fee_at_deploy_hype`; the factory's own
  `creationFee` is the one in force), names the filler, the checker, the closer (four keys with
  the operator, none the same) and the recipient, lists BTC,
  ETH and SOL with their caps (no HYPE), hands the factory to our Safe last (`--safe`: `setOperator`, the
  operator read as the deployer's key before and as the Safe after; refused as 0x0, as one of the three keys, as
  the deployer's own key, as an address without code or as one that does not answer `getThreshold()` and `getOwners()`
  as a Safe does; the threshold and the owners it names are printed and shown in the confirmation a person types on mainnet; needed on mainnet, so that the key that deployed the
  factory never stays in charge of it), and writes `deployments/<network>-<label>.json` with demo3's
  keeper nodes on testnet, `--keeper-nodes` on mainnet. `pool --plan` publishes the terms, approves the locked USDC, opens the pool with the
  fee read from the factory and sets the plan's codes; `codes --plan` sets them on a pool already open, skipping one that
  is on chain as listed, sending a raise of a used code's share for the same partner, and refusing,
  before anything is sent, anything else on a code a sale has named.
  `buy --code` buys with a code's text. `close --key` and `finish --key` read the pool first and say as
  whom it would take the call or why it would refuse (`Locked` before the term unless the key is the
  closer; for `finish`, each thing still running, owed or reserved, and with `--exam` until when that
  exam runs, and the week after the close). `request --amount`, `withdraw` and `cancel` do the same for
  the creator's withdrawal: what it asks and from when to when it can be carried out, what it pays
  now, or `NotReady`, `RequestLapsed`, `NoRequest` with the times. Without `--send` they print what
  they would do and read no key. `code --code … --partner … --bps …` (with `--author` and `--author-bps` for a
  code for two) sets one code from the creator, checked as `codes` checks a plan's (`BadShare`, `CodeUsed`; the
  same as on chain, nothing sent). `pot --amount` puts more of the creator's USDC in (`approve`, then `addToPot`),
  or says `NotCreator`, `Done`, or that the creator holds less than that. `claim --code … [--code …]` says what
  each named code sends its partner and author and sends `claimShares` from anyone's key; nothing due, nothing
  sent. `pay --max N` lists the payouts within N of the head, asks the pool by eth_call from the sender how many
  `pay(N)` settles now, and with none says why (not yet due, waiting for its approval, the day's payouts paid, the
  money) and sends nothing. None of them caps an amount. With `--signer ADDRESS` (`pool`, `codes`, `code`, `pot`,
  `claim`, `pay`, `close`, `finish`, `request`, `withdraw`, `cancel`, `approver`, `approve`, `lift`) the call is from an
  address that signs elsewhere, our
  pool's creator being a Safe and its approver a Ledger: the command checks as that address and prints
  the calls in order as the Safe's Transaction Builder takes them, sending nothing and reading no key.
  `lift --pool … --id 3` lifts the stop of a payout (`liftStop`) from the factory's operator (`--signer` for our
  Safe, which gets the call printed: to, value, data; or `--key` for the deployer's key on a testnet factory not
  handed to a Safe): it reads the pool, the payout and the factory first and says what it would send, or why the
  pool would refuse it (not the operator, a finished pool, a payout not stopped, its 14 days over).
  `fee --deployment … --amount 0.5` changes the factory's fee from its operator (`--key`, or `--signer` for our
  Safe, which gets the call printed): it reads the fee and the operator first and says what it would send, or that
  the factory would refuse it (not the operator, a fee of none; on mainnet above 10 HYPE without
  `--confirm-large-fee` it refuses before reading anything); the same fee as the factory's it does not send
  at all, a check of the script's own (the factory would take it and change nothing); with `--send` it reads the fee
  back after.
  `check --plan …` asks `SimPoolFactory.check` about a plan by eth_call, sending nothing and reading no key: with
  `--deployment`, the factory it names; without, the factory, PaperExam and SimPool of the checkout, put in the
  call by a state override over the network's precompiles, with the assets `deploy` lists, the roles set and the
  terms as `pool` publishes them, so a plan is checked before any factory stands. Terms `TermsBook.publish` would
  refuse (empty, or over 24 575 bytes) are refused first. It then measures `create` of the plan with the factory's fee, read
  from it, by eth_call, the creator an address no key holds and USDC answered by a stand-in, at the least gas it runs on, and
  compares that, with 60 000 for USDC's own transfer, against the network's small block (the least `gasLimit` of
  the latest blocks, at most 3 000 000): above it, the pool would not open by an ordinary transaction, and `check`
  stops red. The first pool's plan measured 1 673 901 on testnet on 16 November 2026 (forge,
  `test_gas_createOfTheFirstPlanFitsASmallBlock`: 1 694 229 with the token, the base and the calldata).
  `pool --signer` prints the terms, the approval of the locked USDC and `create` with the factory's fee; the codes
  follow with `codes --signer` once the pool's address is known. The first pool's plan is
  `deployments/plans/sim-first-pool.json`: 24 lines, the sizes 5 000, 10 000, 25 000, 50 000, 100 000 and
  250 000 each on the tiers 3 / 9, 4 / 10, 5 / 10 and 6 / 10 % at the CFO's prices, each line's loss limit
  trailing the highest daily snapshot up to the starting balance (`drawdown_kind` 1), payout 8 % of the size, two
  payouts a day, 4 000 locked (`locked_usdc`), the lines of 5 000, 10 000 and 25 000 on and the larger ones off
  (`on`) until the creator switches them on with two of their payouts free; thirty days
  a stage with no minimum of trading days, seven days to start the stage after a pass, 0.045 % a fill
  (Hyperliquid's taker fee; no builder fee, since nothing trades on the exchange), at the book with a
  ceiling of 150 bp, a fill delay of 4 blocks in both stages (an order placed in block B is filled no
  sooner than block B + 4, so seeing the price a block ahead of the oracle gains a trader nothing; the
  CFO, 13 November 2026), and a fill window of 3 blocks, the window rule below at a filler that is
  never late, until `--measure` says otherwise. The factory needs an
  idle limit; the plan sets it to the stage's thirty days, where it never ends a stage before its
  deadline, so the terms name no idle rule. The rehearsal on testnet has a plan of its own,
  `deployments/plans/sim-rehearsal-998.json`: one line of 20 for 2 test USDC, a target of 0.25 % in both stages,
  3.2 locked (two payouts of 1.6), the first pool's rules, cost and fill delay, the widest fill window, and the
  creator's own code `OWN`; the partners' codes come by `code`. `check` takes it as it takes the first pool's, and
  mainnet refuses it.

- **The checker**, `ops/sim_check.py`: the open script behind every stop. For each payout still
  waiting it matches every fill of the paid stage with the fills of every other exam and stage of the
  factory: the same asset, opposite sides, at most 10 minutes apart, sizes within 20 %. A match is
  reason 1; with the two traders funded by one address, or one paying the other (USDC transfers since
  the deployment), reason 2. It stops a payout once, from the factory's checker key, and until when
  is the pool's own rule: on a pool with payout approval until the payout is paid, approved or not (an
  unapproved one waits up to 7 days, and a pair found on the fourth still stops it); on a pool from
  before approvals at least 5 minutes before it is due. It never lifts a stop: lifting is the factory's operator's
  (`deploy_sim lift`).
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
  - how late this trader's stops and takes were carried out, a series of its own beside the fills', against
    everyone else's by the same rule (the trader's median above the others' 90th percentile, five of each at
    least). A stop's lateness is the blocks from the oracle reaching its price to its close (`TriggerFilled`):
    from the first block of the stretch in which the oracle stood at or past the price without a break up to
    the close, never before `armedAt`. `fillTrigger` takes a trigger only while it holds, so the filler's
    choice is inside that stretch; a stop the oracle reached, left and reached again counts from the second
    reach. The oracle is read back from each close of every account of the factory, 60 blocks at most (the
    widest window an order may have); a stop that stood reached longer is counted as at least 60, and one
    whose prices were not read is said as not read and counted on neither side;
  - each stop and take of the stage and of the exam that the oracle reached while it stood and nobody carried
    out then, and how its time ended: carried out later, taken off by the trader, replaced, gone with the
    position, the account ended, or still standing. A stop missed leaves the position open, and if the price
    comes back the trader is spared the loss; the lateness above counts from the last reach and does not see
    it. A trigger's time runs from `armedAt` to the block it ended in. The oracle is read along it one block in
    60 at the least (`FALLBACK_BLOCKS`, also the widest window an order may have), 3 000 requests an
    account at the most (`--touch-reads`), the step growing to fit them with each trigger's first and last
    block counted; past them, in the order the triggers were set, a block is said as not read. A stretch
    held at the price 60 blocks or the step is never missed by the reading, a shorter one may be. These
    prices are kept apart from the review's others: no other check sees them. A block it was shown to have
    triggered in (`TriggerSeen`) counts as reached. Two reads at the price with none off it between are one
    stretch; for a trigger carried out, its last such stretch is the close's own. With the stops' loop's journal
    (`--prices prices-<deployment>.jsonl`, "The stops" below) every block of each trigger's time is judged by the
    journal instead, and no other read stands in for a block it does not hold. A reach nobody carried out (a stop
    taken off or moved by the trader while reached among them) is the reason `missed-stop`; a block of a trigger's
    time neither journal holds, no journal at all, two prices for one block, or a journal the venue's candles
    contradict (below) is `stop-unchecked`; a block only one of the two journals holds, which the optional node did
    not bear out, is `stop-one-journal` (no cross-check was made there: the second journal not there, refused,
    behind, or with a hole of its own); a stop carried out more than 60 blocks after the oracle reached it is
    `stop-late`. The document says in `journals` how many journals stand behind the verdict on the stops: 2 for a
    clean one, null where no stop stood.
    Either holds the payout, with the draft of a stop of reason 3 naming the transactions that set the stops;
    the review is clean only when every block of every trigger's time is checked and no reach went uncarried;
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
    - the drawdown line as the exam draws it (`drawdownKind`, read from the account): from the
      starting balance, or trailing the step's highest day's snapshot to the starting balance and never
      above it. Each snapshot is held to the line as it stood before it (the contract takes none while a
      rule is broken), and an end by the drawdown to the line at that time. Where the kind was not read,
      nothing is said of the line;
    - each stop and take as the contract writes it: a stop or take carried out (`TriggerFilled`, with
      the `Filled` right before it) counts in the cash and the position like any fill, and is held to the
      trigger that stood, to its block (not before `armedAt`), to the oracle having reached its price,
      and to what it closes (the smaller position, the size named or all of it). A trigger left on a
      position closed or turned round, one set on no position, and a carried-out trigger with no fill of
      its own are flags. A stop's close is no order's timing: the fills' lateness and the filler's choice
      of a block leave it out, and its own lateness is the series of stops and takes above;

    The oracle prices are read from the chain at their blocks. Where the node keeps no state that far
    back, `--prices` stands in. A pass in three fills or two days at most on the largest size is said
    as a profile, not counted as a reason.

    A second profile is the pass that needs no view of the market: a position at a small leverage held to
    the target, its stop just over the day's loss line, and after a stop nothing until the next UTC day,
    when the line is counted from the smaller equity. From the fills, a step at a time: four round trips
    (flat to flat) at most, each opened at three times the account at most; every one but the last closed
    with 80 % or more of the day's allowed loss gone (the account's own `rules()`, against the snapshot in
    force), and none opened again that day; the last one is the pass. One round trip alone is the same
    game won at once. Round trips under 5 % of the account are not counted either way. The rules allow
    such a pass and an honest trader may trade so: it is a grey line of the card (`recount.<part>.flip`),
    in no reason, and changes neither the verdict nor what is offered. A stop is seen only once it is
    taken: the same player who split one position over several round trips, or re-entered the same day
    at a size under the line, is not marked.

  - how often the pool's exams pass, against the CFO's thresholds (13 November, the first pool's; to be looked
    at again after its first 100 exams; `--thresholds` to change). Three counts:
    - passes among the pool's last 100 exams ended: yellow from 24, red from 28;
    - payouts recorded among its last 100 exams sold: yellow from 6, red from 8;
    - payouts over the net income from prices over 30 days: yellow from 0.5, red from 0.75.

    Fewer than 100 are counted per hundred. Shares count from 30 exams ended; before that, the pool is red
    only if the payouts recorded since it opened exceed its net income from prices. The same counts are
    split by code, by asset, and by the hour and weekday of the pass, with the passes in very few fills
    counted. Beside them stands how many of the passes in that window of the last 100 exams ended have the shape
    of a coin flip (`coinFlipPasses`, and how many of those with a stop at the day's line): a number for
    whoever sets the pool's rules, with no threshold yet and no part in the level. A rule of the exam that is wrong still counts up on chain; it shows only as more passing.
    Yellow or red marks every payout of the pool while it holds. The approval stays, since the trader is
    not to blame. Red means no approving in a batch: the operator's page refuses "Approve selected"
    (`approveSelectedCall`), and each payout is approved on its own;
  - the timing of the fills, judged against chance:
    - the filler's choice: how often a fill landed in the best block of its window for the trader, against
      how often best blocks occur in the window;
    - the lead on the feed: how often the oracle moved the trader's way in the block right after the fill,
      against a coin.

    Each is flagged over 8 fills at least, when chance gives as much with probability under 1 %. The oracle
    of every block of each order's window is read for it.

  Whether a payout waits for approval is what its own recorded events say (`PayoutDue`, then
  `PayoutApproved`, `PayoutStopped`, `StopLifted`, `StopFinal`, `PayoutPaid`, `PayoutHeld`, in the order the
  chain wrote them), not the clock: due and never approved waits, at any hour, and an approval stays through a
  stop and its lifting. The events and the pool's own answer must agree on the payout's state and, where the
  pool answered it, on whether it is approved; where they do not, or the events are no payout's life, nothing
  is offered and the review says what is not read. Where the pool's answer has no `approved`, the events give it.
  A payout whose result did not count up is held back from approval whatever the pool's rate says of approving
  in a batch, and its card keeps that rate (`batchApproval`).

  The pool's daily limit on payouts is read, never kept in the script: the pool's own `payoutsPerDay()` (its
  setting, 1 to 8), the factory's `PAYOUT_DAY()`, the pool's `payRoom()` and `nextPayAt()`, and the settlements the pool wrote (`PayoutPaid`,
  `PayoutHeld`: each uses a place for a day). For a payout still waiting the review says (`dailyCap`, beside the
  verdict and never a reason in it) whether a place is free (`free`), it is ready and every place is used
  (`waits-limit`, with `settleAt`, when `pay` settles it), or it is not ready (`not-ready`, its approval or its 72
  hours first). `settleAt` and `ahead` come from the order `pay` takes: ids in order, a payout not ready passed
  over, the first ready one the limit holds stopping the call, the oldest place taking each settlement; the pool's
  money is not counted. Where the events and the pool's `payRoom()`/`nextPayAt()` do not agree, or any of the
  reads or the payout's approval is missing, the status is `unread` and no time is said. A pool with no such
  limit (no `payoutsPerDay()` and no `payRoom()`) gives `none`; a pool with `payRoom()` and no number of its own, or a
  chain read without the reads, null: the number is not guessed.

  For a payout that was stopped the review names the reason in words and by number (1 to 4, prohibited play
  included; one it has no words for is named by its number, never an unknown stop): the pool's own
  `stopReasonOf(id)`, else the reason `PayoutStopped` recorded; where the two differ it names none and says
  so as not read.

  Its verdict is "clean", or "suspicious" with the reasons, with the payout's three times: its claim,
  72 hours after it, and the ceiling 7 days after it, past which it is paid unapproved. Both spans are
  read from the factory (`PAYOUT_DELAY()`, `PAYOUT_HARD()`), not assumed; where one was not read on a
  pool with approvals, the review offers no call and says no time. The call to
  sign is written out as text with its calldata:
  - `stopPayout`, reason 1 or 2, when there is a pair or a mirror with two opposite fills, since the
    pool takes no such stop without two fills;
  - `approvePayouts` otherwise.

  Each call is offered only if the pool would take it. A payout that is not waiting (paid, stopped, held or refused, as
  `--id` may name one) is offered neither an approval nor a stop, only its state in words. The approval goes only to a pool that answers
  `approver()`. A pool without it pays at 72 hours by itself, and the review says so, with no call and no
  ceiling. A payout approved already is offered no second approval (the pool would revert it), and a pool
  whose approver is the zero address is offered none: nobody could sign it, and the payout goes out at its
  ceiling. A stop goes only to a payout still waiting and never stopped, and in time by the pool's rule:
  until it is paid on a pool with approvals, until 5 minutes before `dueAt` on one without. Links of
  reason 2 leave out the senders the review does not count. An exam or stage of the pool that was not
  read makes the pool's running total incomplete: `enough` is false, and the account is named. The names, the
  selector, the fields of a payout and the pool's rule for a stop are held to `src/sim` by tests.

  A payout is stopped once. `stopPayout` sets `wasStopped` and nothing clears it: a lift (`liftStop`)
  sends the payout back to waiting, and a second `stopPayout` of it reverts (`BadPayout`), whatever the
  reason. So the stop of reason 4, prohibited play, is put as soon as its published analysis is ready,
  not after another stop of the same payout has been lifted, and a stop is not lifted meaning to stop
  again with reason 4. A standing stop keeps its reason (`stopReasonOf`) and cannot be changed to
  another; the reason moves no money, so a payout stopped for 1, 2 or 3 that turns out to be prohibited
  play too stays stopped and becomes final in its time, with the analysis published beside it. If the
  first stop was lifted already, the payout is due again and is paid in its turn: nothing on the chain
  stops it. What remains is each of the trader's other payouts still waiting, which has a stop of
  its own and may be stopped with reason 4, and the published analysis, so that the record says what
  was seen. Lift a stop only when its payout is to be paid.

  A result that does not count up comes with a draft of a stop of reason 3, a result disputed. The
  draft names the fills and leaves out the hash of the analysis, which a person writes. Such a payout
  is offered nothing to approve: its `action` is `hold` and its call is empty. So is a payout with a stop
  reached and not carried out (`missed-stop`), a block of a stop's time not checked (`stop-unchecked`), or
  an account's own events not read; the draft then names the transactions that set the stops. The operator's page
  shows no Approve button for it, only "Approve anyway" behind a warning the operator confirms
  (`approveAnywayCall`). Something else suspicious, with the result counting up, is `decide`: the
  approval is offered, and the choice is the signer's. The review cannot see:
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

  The operator's section of the control panel (`app/lib/simoperator.js`) draws the stops to the checker and to the
  factory's operator, and the button that lifts one (`liftStop`) to the operator alone, our Safe.

  A payout's review is the JSON `ops/payout_review.py --out` writes, schema `payout-review/4`, one file per payout.
  The page shows a review only where it is of this very payout and names the call the page itself would build
  (`reviewFor`). It never signs what a review hands it.

## The stops

A stop exists for the chain only in a block whose transaction carries it out: `fillTrigger` takes it while the
oracle price of that very block is at the stop or past it, and inside its window only from the factory's filler.
A reach that no transaction of the filler's lands in leaves no trace on chain. The keeper's one loop has blind
spots there: its slow pass, a receipt it waits for, and a node's rate limit (up to 63 seconds) each hold the stops.
By default (`--stops loop`, COLOSSEUM_STOPS; `inline` is the way back to the keeper as it was, with none of what follows)
the keeper looks at the stops in a loop of their own, `ops/stop_filler.py`, a thread beside its own loops:

- every block: one `eth_getLogs` of the blocks since its last look (stops and takes set, cleared and filled, the
  fills that move a position, exams sold and ended), one read of the oracle price for each asset with a stop or a
  take standing, and the comparison made by the loop itself, not one `eth_call` a stop;
- a stop reached at the head is signed and sent at once, with the fills' fixed gas, no read before it and no wait
  for its receipt; the receipt is read in the next blocks. A stop the exam refused once is tried with a call first
  the next time (a book past the ceiling, say);
- the filler's one key sends from both threads, and a nonce one thread took and had not yet handed to the node
  would hold every later transaction of the other. So every transaction of the key is numbered and handed to the
  node by this thread alone, one call each, never behind a retry ladder: the keeper's thread queues its fills and
  slow calls, they go out between two looks at the head, and the keeper waits for their receipts in its own
  thread. A nonce is spent only once the node took the transaction; one whose answer was lost on the way is sent
  again with the same bytes after 10 seconds without a receipt, never with other bytes under that nonce. The count
  is read from the node at the start and again only if the node says a nonce is taken by something else, and the
  next nonce never goes below one handed out. A transaction of the keeper's the node was busy for (or never reached)
  is tried again after 1, 2, 4, 8 and 16 seconds while the keeper waits for it, 60 seconds at most, with no thread
  held; the keeper's transactions go out between the blocks of a long step, not after it. A stop still waits behind
  any earlier transaction of the key that is in the node and not yet in a block, and the key's oldest transaction
  still out of a block after two minutes (the gas price left it behind, say) is an alarm (`send_stuck`);
- the slow pass, a receipt the keeper's own thread waits for, and that thread's retry ladder hold only that
  thread. Every call of the loop is tried once: a busy node costs it that look. The blocks it missed are read back
  the next time, their events always (1 000 blocks at most) and their prices for the last 20; the head's stops go
  out before the blocks behind it are read. Blocks with no prices are said (`stop_watch_blind`). Blocks skipped by
  the read-back of prices are an alarm at once when a stop stood armed in them or was set and armed in them
  (`stop_watch_gap`); a price read that fails block by block is an alarm only once 30 blocks in a row have no price
  with a stop standing (`stop_watch_blind_long`); past 1 000 the
  loop says it lost the thread (an alarm), forgets its reaches and takes the stops again from the next read of
  the exams;
- a stop whose position could not be read keeps each block's price; once the position is read, those blocks are
  judged one by one against the position as it stood before each (read by its block), so failed reads in a row do
  not hide a reach; a read of the past that fails keeps them first in line for the next step, and a stop that ends
  before they are judged is an alarm (`stop_unjudged`);
- a stop set while the keeper was down or behind its logs is read back from the exam by the slow pass at one
  block and handed to the loop (`stop_found_by_read`); a stop set on an exam sold in the same blocks is known in
  the step that sees the sale;
- the keeper's loop checks that the thread is there and moving: one that ended is an alarm and is started again,
  one that has not heard the node for 30 seconds is an alarm, and so are calls failing for 30 seconds (one alarm an
  hour at most, the episodes between said as lines) and 30 blocks without prices while a stop stands. An alarm's own
  reads (whether the exam was over) are made after the step's stops went out. A line `stop_loop_alive` every ten minutes says it works.

Two settings, each the keeper's old behaviour by default: `--stop-rpc` (COLOSSEUM_STOP_RPC_URL), the node the
loop reads through and the key's transactions go to, the keeper's own when empty; and `--stop-tip-gwei`
(COLOSSEUM_STOP_TIP_GWEI), the priority fee of a stop's transaction, zero when empty. On mainnet the stops' node
must not be the gateway's, as the keeper's must not.

The watch runs on the same prices. For each stop or take standing, armed, with a position open, a stretch of
blocks in which the oracle stood at it or past it is a reach. A reach that ends with no `TriggerFilled` in it,
the price back, the stop taken off or moved by the trader, the position closed or the exam over, is an alarm,
`stop_missed`; a reach still unfilled after `--stop-overdue` blocks (5) is one too, `stop_overdue`. Two are lines,
not alarms: a reach of a single block that the price ended (`stop_reach_too_short`: a transaction sent on seeing
it lands after it; `--stop-alarm-min-blocks 1` makes it an alarm too), and a reach after the exam's deadline or
idle limit (`stop_reach_after_exam_over`: `fillTrigger` refuses it, `TooLate`, and the keeper's `expire` is what
is due). An alarm is written as the host's watchdogs write theirs: an `ALARM` line in the journal, the line added
to the flag file `stops-<deployment>.alarm` next to the keeper's state, and the line handed to
`COLOSSEUM_ALERT_CMD` when the host has a channel. A miss does not mend itself: the flag stays until a person
removes it. Blocks whose prices were not read judge nothing, and are said.

Every block the loop looks at goes to its journal, one file a day by the loop's clock (UTC),
`prices-<deployment>-YYYY-MM-DD.jsonl` next to the state, each opening with a header
`{"journal": "colosseum-prices/2", "chain", "factory", "label"}`, then one JSON line a block:
`{"b": block, "t": its time, "w": the loop's clock, "px": {asset: price}}`, with every platform asset of the network
and any other asset with a stop or a take standing, a stop known or not (so the first blocks after a restart, before
the keeper's slow pass has read the stops again, are in it), and `"unread"` for an asset whose price was not read.
Blocks it did not look at are lines `{"gap": [from, to], "why": ...}`: not running (written from its last line when
it starts again), the head ahead of its read-back, behind past 1 000 blocks. It is appended line by line by one
process and survives a restart (a line cut short is cut off). Nothing in it is sent to the chain.

It is the keeper's own record, so the review believes it only where something else bears it out, with no node that
keeps old blocks:
- a second journal: the same loop alone (`python -m ops.stop_filler`, no key, it sends nothing) on another machine
  and another provider's node writes `watch-prices-<deployment>-…` the same way, copied to the host every minute
  (docs/HOSTING.md, "The second writer of prices"). A block is held where either journal has it, and where both
  have it the prices must be the same; a reach in either is a reach. A restart of one writer is covered by the other;
- the chain's own prices: a fill at the oracle (any source but the book) is at the oracle price of its block, and
  `TriggerSeen` names it; a journal must give the same price at every such block it holds;
- the venue's minute candles (Hyperliquid's `candleSnapshot`, kept by the review's service, `ops/venue_candles.py`),
  a coarse screen over every minute of a stop's time: a minute the journal shows the oracle at the stop while the
  candle stayed more than `CANDLE_BPS` (1 %; 3 % on testnet, whose perp trades thinly) short of it all minute, or a
  candle past the stop by more than that where the journal shows no reach, contradicts the journal. A candle is the
  price of trades on the perp, not the oracle (a median of other venues' prices; the perp trades at a premium or a
  discount to it, and a thin book prints wicks): 1 % is far past either for BTC, ETH and SOL in a calm market, and
  near enough not to hide a reach by more than that. Fewer than 90 % of a stop's minutes with a candle is a doubt too,
  and so is a block whose journal line carries no time (it falls in no minute: not screened). The stops' loop writes
  each block's time on its line, a block before the head of a step over several asked of the node;
- optionally, a node that answers for old blocks (`COLOSSEUM_ARCHIVE_RPC_URL`): calibrated on the chain's prices and
  asked for a sample of 200 blocks an account (`--journal-checks`, at least 1), and for each block only one journal
  holds (as many again an account at most), which it bears out. Without one nothing is held for it.
A block neither journal holds, two prices for one block (in one journal or between the two), a candle that
contradicts, or a file of another chain, factory or deployment (refused whole) is `stop-unchecked`; a block only
one journal holds (the other refused, missing, behind or down there) and no node bore out is `stop-one-journal`:
either way the payout is held. "Clean" needs every block of every stop's time in both journals at one price. A line of the wrong shape is a hole, never the end of the review.

### What the terms say a stop does, and what a missed one costs

As the code does it, with the reasons a payout may be stopped for that the pool already has:
- a stop or a take is carried out by the factory's filler, `fillTrigger`, in a block where the oracle price of that
  block is at its level or past it, at the oracle price of that block (or the book's, for an exam filled from the
  book, within `maxWorseBps` of the oracle). The stops' loop looks at every block and sends the fill in the block
  that sees the level reached; it does not wait for anything else;
- if a reach of a stop goes by with no fill (the filler down, a node behind, a fill that did not land in time, the
  trader taking the stop off while reached), the chain keeps no trace of it: the account goes on as if the stop had
  not been reached. The loop raises an alarm at once (`stop_missed`);
- the payout of a stage or exam with such a reach, or with a block of a stop's time the journal does not hold, is
  held: the review offers no approval (`hold`), and a pool with approvals pays it unapproved only at its ceiling,
  7 days after the claim. A person decides from the review and the journal whether the result is disputed; if it
  is, the checker stops the payout for reason 3, "the result is disputed: a flaw of the exam, the oracle or the
  filler", with a published analysis, sent with the checker's key. A stop of reason 3 is never sent by a program,
  and only the factory's operator lifts it (`liftStop`). A pool without approvals pays by itself at 72 hours: the
  review's service raises an alarm with the last moment a stop is still taken (`payout_needs_a_stop`).
Nothing here moves money by itself, changes a result on chain, or pays anyone back.

### What it costs

Only a stop's fill is a transaction; the loop sends nothing on a timer, and its reads cost no gas. One fill,
`fillTrigger`, measured in the contract tests (`forge test --match-path test/exam/PaperExam.t.sol --gas-report`):
88 736 gas of execution from cold storage, 85 454 at most in the report; with the transaction's own 21 000 and its
calldata, about 110 000 gas. The loop sends it with a limit of 400 000 and a fee cap of twice the gas price, and pays
only the gas used. At a gas price of 0.1 gwei (HyperEVM's, read on 20 November 2026; it moves):

| fills a day | gas a day | HYPE a day (0.1 gwei) | at most (every fill at its limit and four raised fees) |
|---|---|---|---|
| 0 | 0 | 0 | 0 |
| 10 | 1.1 million | 0.00011 | 0.0020 |
| 100 | 11 million | 0.0011 | 0.020 |

"At most" is 400 000 gas at twice 0.1 gwei raised by a quarter four times (about 2.44 times), every fill: a bound,
not an estimate. A fill the exam refuses (the price back by the time it lands) costs its gas too and closes nothing. The watch also runs alone, with no
key and sending nothing, on another machine and node (`python -m ops.stop_filler --deployment <label>`), with
files of its own (`watch-prices-…`, `watch-stops-…`): it sees what the keeper missed even while the keeper is
down.

What the loop does not do: carry out a stop whose reach is shorter than the way from a block seen to a
transaction landed, about one or two blocks (the oracle moves about every three); carry out a stop the exam
refuses (its book more than `maxWorseBps` from the oracle); stop the trader taking a reached stop off; replace
a transaction the gas price left behind; free a stop from an earlier transaction of the same key; or go on while
the filler's one key or its one machine is down. Each needs the contract (a key of the stops' own, or anyone
allowed to carry a stop out), a second filler, or both.

## Measuring the filler's window

The fill window of a plan is fixed at creation, so it is measured first, on a pool of its own
(`deployments/plans/sim-measure-pool.json`: one line of 1 000 for 9, the first pool's fill delay of 4 blocks and
the widest window, 60 blocks, so a late fill is measured and not lapsed), with the host's keeper running and filling from the filler's key.

    spike/.venv/bin/python -m ops.deploy_sim buy --deployment <label> --pool <measuring pool> --line 0 --trader trader --send
    spike/.venv/bin/python -m ops.sim_keeper --deployment <label> --measure <exam> --trader-wallet trader --orders 100

`--measure` sends the trader's own transactions (by hand, and on mainnet only after the typed confirmation; it has no dry run). It places the orders
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

## Networks

The scripts above run on testnet unless mainnet is named twice: `COLOSSEUM_NETWORK=mainnet`, which the plumbing
reads when it starts (spike/hlspike/network.py: chain id, RPC, info API, USDC), and `--network mainnet` on the
command line. Either alone stops the program. What is the simulated pool's own per network is in
`ops/sim_network.py`, in one table: each asset's perp index (testnet BTC 3, ETH 4, SOL 0; mainnet BTC 0, ETH 1,
SOL 5, from each network's info API meta) and the asset `--measure` trades. The precompiles PaperExam reads, the
oracle price at 0x…0807 and the best bid and offer at 0x…080e, are at the same addresses on both networks.

On mainnet a command that reads, or that prints calls for a signer elsewhere (`--signer`: the Safe, the
Ledger), needs nothing more. One that sends needs a key directory named for mainnet (`COLOSSEUM_KEY_DIR`, not
testnet's default) and a confirmation: `deploy_sim ... --send` and `sim_keeper --measure`
ask the person at the terminal to type `send on mainnet` and refuse without a terminal; the keeper, the relayer
and the checker as services take `--confirm-mainnet <label>`, the deployment's label, so that a unit copied from
testnet does not start sending there. `deploy_sim deploy` on mainnet reads the meta again and stops if an index
moved, takes the keeper's node from `--keeper-nodes` (a node of its own, not the gateway's), and `pool` refuses a
plan whose terms say testnet. The MultiPool keeper stays testnet's; on mainnet the host's unit serves only a
simulated pool's record.

    spike/.venv/bin/python -m ops.sim_probe --network mainnet

reads, and only reads, each asset's perp info, oracle price and best bid and offer at its index on the network,
against the info API's own meta and oracle price, and the network's USDC (name, version, decimals, the EIP-3009
read the relayer uses). On 14 November 2026 at 17:36 UTC (mainnet block 51 261 165) every check held: BTC,
ETH and SOL at 0, 1 and 5, the oracle precompile within 0.7 bp of the API's oracle price, the books 0.1 to
0.5 bp wide and standing 8.7 to 12.9 bp below the oracle; USDC "USDC", version "2", 6 decimals.

## A flat file for an AI code check

    scripts/flatten-sim.sh <commit> [out-dir]

writes one Solidity file of the contracts `deploy` puts on chain (PaperExam, SimPool with its two libraries
SimPoolStops and SimPoolSetup, TermsBook, SimPoolFactory) and everything they import, once each, as they are at that commit, with a MANIFEST of the
commit, the compiler, each source's sha256 and the flat file's, and checks that the flat file compiles on its
own. It works in a temporary worktree and leaves this checkout alone. The flat file only repeats the sources of
its commit, so it is not kept in the repository: for a later commit (the one that brings the menu of sizes, the
next change of a contract) run the script again with that commit and upload the new file with its MANIFEST, so
the check's report names the code it read.

## Not done yet

Nothing for now. The checker's unit on the host, the last item here, is `colosseum-sim-checker@<name>`
(docs/HOSTING.md, "A simulated pool's checker").
