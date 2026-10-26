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

Three limits hold a sale back.

- **The share.** The sizes spoken for may not pass the plan's share of the pool's value: 80% of
  it by default, the rest being the pool's buffer. With a pool of 100 and two seats of 25 sold,
  30 are left: six seats of 5, three of 10 or one of 25.
- **The money.** A passing trader's account is paid from the pool's balance on HyperCore, so
  the size of every exam still running, and the 1 USDC a seat's new account may cost, has to be
  on that balance. Value that sits elsewhere, in a funded seat or as prices paid on HyperEVM,
  does not sell a seat.
- **The keys.** Every funded account takes an agent key from the factory's registry and retires
  it for good when it ends, and the operator publishes keys by hand. A sale needs a free key for
  every seat the pool has spoken for, the funded ones included, so a pass is never sold that no
  key could open (deep audit N3). A key someone has spoiled with a transfer still counts until it
  is purged; the keeper purges them.

Both are counted in the contract's own storage. A balance read from HyperCore is the start of
the block, so two calls in one block would read the same one; a count in storage they do not.
What the pool has sent in the last minute is counted against its balance for that minute,
whether or not it has left it yet. `MultiPoolReader.room(pool, line)` says how many more exams of a size the pool
would sell now, by all four limits: the share, the money, the keys and the number of seats. The views
that only read are a contract of their own, `MultiPoolReader`, out of the pool, which is close to
the size a contract may have; it reads only what the pool shows anyone, and the scripts read
through it. Its `room()` counts the leavers' part on the balance as a sale does, and holds a line
priced under the floor to what is left of the quarter of the seats such lines share with the free
exams (audit window 2, A2-1).

An exam that ended without a pass, or whose pass nobody took within the plan's window, gives
its size back: `release(exam)`, by anyone.

## Free exams

A creator may give exams away: the plan says how many a month (`freePerMonth`, zero for none)
and who signs the invitations (`inviter`, the creator's own address). A month is 30 days,
counted from the pool's start.

- **What a free exam is.** The menu's exam of the line invited, at no price: no price to the pool
  and no fee to the platform. Its size is spoken for from the moment it is taken, within the share
  in work and the number of seats, exactly as a sale speaks for it, so free exams come out of
  the part of the pool that works and never out of the buffer. A trader who passes takes the same
  funded account, on the same rules, and the pool pays the 1 USDC its seat's new account may cost
  as it does for a paid one.
- **How many at once.** No more than a quarter of the seats run free exams at the same time
  (`freeRunning`; with 8 seats, 2): a free exam holds its place until it is released, and a
  free pass whose trader took the funded account holds it until that seat is freed, since the seat
  is taken from the start of the exam (the terms, D8; the cloud audit, N14). Then the next may be
  taken (`TooManyFree` until then).
  This is on top of the month's number, so a pool with fewer than four seats gives none.
- **An exam sold at a loss counts as a free one.** An exam priced under the pool's floor
  (`priceFloorBps()`, bps of its size) counts among the free exams under the quarter, though it
  pays its price (the cloud audit's N11 and F33; the CTO, 18 October 2026). The floor is the CFO's
  formula (a file outside this repository), worked out once, at creation, from the
  plan: the chance of passing every step on the exam's drawdown, times what a purchase costs the
  pool by what follows a target and the trader's share, over the price the pool keeps after the
  platform's 0.5 %. The default plan's is 39 bps. The template states it (`ops/terms.py`).
  Free exams have no idle limit of their own: they lapse, fail and are released as paid ones do.
- **The invitation.** A pool with free exams creates its own `FreeExams` contract with it
  (`freeExams()`), and that contract alone may have the pool sell a free exam (`sellFree`). The
  inviter signs, off chain, `Invite(address trader, uint8 line, uint64 nonce, uint64 expiresAt)`
  under the EIP-712 domain of that contract on this chain (`domainSeparator()`). The invited
  trader calls `take(line, nonce, expiresAt, v, r, s)` on it. An invitation serves its trader
  only, once (`spent`), until it lapses; the inviter may withdraw one nobody has taken
  (`withdraw`). `ops/multi_run.py invite` signs one and prints it as a line of JSON; `free` is the
  trader's step that takes it. The invitations live in their own contract because `MultiPool` is
  close to the size a contract may have.
- **Why it is built this way.** The creator hands invitations out wherever they like, on our X
  or on their own, and nothing of that touches the chain: a signature costs no gas, and the list
  of who was invited is never published. An invitation names its trader, so one posted in public,
  or taken from a message, is no use to anyone else; a code anyone could redeem would go to
  whoever is fastest, a bot reading the mempool first. A number used once and a time it lapses
  keep a leaked invitation from serving twice or for ever. The month's number is the contract's,
  so a creator who signs more invitations than that still gives no more exams than the plan
  says.
- **What the contract does not check: who is invited.** That is the creator's decision alone.
  One person with many addresses, or the creator inviting themselves, gets as many free exams as
  the creator signs for, up to the month's number. What a free exam costs the pool, if its
  trader passes, is the funded account it then trades; that cost is worked out with the plan,
  not in the contract.

## From the pass to the account

- `claim(exam)`, by the exam's trader, once it has passed and within the window. The pool picks
  a seat of that size, sends it the money, and from then on the money is the seat's.
- `open(seat)`, by anyone, once the money has arrived: a few seconds later. It starts the funded
  account for that trader. If the money did not arrive, `rearm(seat)` sends what is missing
  after a minute.
- On a builder dex the account starts in two calls, as any account there does: the first `open`
  sends the capital to the dex, a later one opens the stage once the dex shows it. The seat is
  Armed until then, and only `open` takes that second step (self-audit v3, round 1, T3). A
  seat whose stage never opens gives the price back at `finish`; one on its way to the dex when
  the pool stops still opens, and `endSeat` ends it. A seat whose dex takes no transfer comes
  back once its pool's window is over: anyone may release the pool (`abandonFundedStage`), and
  `finish` returns the size and the price. A retake (`reopen`) takes the same two steps, and
  gives no price back if it never opens: its trader had a stage on that price.

Every pass has its seat: a sale on a line whose seats are all spoken for makes another one at
once, while the factory still takes the funded rules, so a delisting later refuses the next sale
and never a claim (deep audit F9). A seat armed and never opened, say because no key was free,
gives its size back with `finish` once it has had the claim window (deep audit F18), and its
trader gets back what the pool kept of the price, sent from the seat on HyperCore: a pass the pool
could not turn into an account was not sold after all (deep audit N2). A pass nobody claims within
the window is released and its price stays with the pool, as a failed exam's does: claiming needs
no key, so a trader who passed can always claim and be paid back. More spoiled keys on top of the
registry's list than one opening skips (sixteen) refuse every opening on the factory until somebody
purges them with `purgeSpoiled`, which anyone may send; the keeper does as soon as an opening is
refused for it. A funded
seat whose key gained a HyperCore account in the block it was bound, so that HyperCore never made
it the agent, takes the next free key with `rekey()` on the seat; the trader keeps the stage (deep
audit F10). Only the stage's trader may ask, in the stage's first day, twice at most: a key
HyperCore took keeps working after it gains an account, so on anyone's word a stranger's 1 USDC
would replace a working key and spend the registry's free keys one by one (the audit's AUD-2).

A seat that was just freed may still be sending its money back, so it rests for a minute before
an account is opened on it. The claim does not wait for that: it takes any free seat of the
line, sends a resting seat the whole size, since its old balance is leaving (deep audit F17),
and `open` waits out the rest. `sweep`, which takes back what reached a free seat, can
touch only free seats, and a sweep that finds nothing changes nothing. So nobody can keep a
trader who passed from the account by sweeping the seat, which the first version allowed.
A free seat counts in the pool's value like any other: what `finish` or `sweep` sends back is
the pool's on its way, counted on the seat until it lands here, so a price struck in the block a
seat is freed is the whole price (deep audit F1; before, a deposit settled in that block bought in
at a discount and a round closed in it paid its leavers short).

A seat is a `Pool` the MultiPool owns and runs. For that, `Pool` gained one switch:

- `runByOwner(traderMayStop)`, once: the pool sells no challenge of its own from then on;
- `openFundedFor(trader)`, by the owner of such a pool: a funded stage for a trader examined
  elsewhere, in full or not at all; only for an owner the factory knows as a MultiPool
  (`setMultiPool`, by the operator, a step of `ops/deploy_multi.py`). Each stage takes a key from
  the shared registry and retires it at its end, so a pool anyone could run this way would empty
  the registry for one account and gas (audit K-3);
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

A trader who opens and closes inside the day is flat at every look, and to the chain a round trip
from flat to flat looks like a gift landing on the account: nothing on chain tells the two apart.
So a trade like that can only be recorded by someone who sees the fills: the plan's `noter`, a
keeper that reads the funded account's fills on the exchange, calls `noteTrade(seat)`. It can
keep an account from being closed as idle and nothing more. **No keeper does this for this pool
yet, so trades made inside the day are not counted**, and an account that is flat whenever it is
looked at is closed as idle once its limit has passed, however much it traded.

`finish(seat)`, by anyone, closes the books on a settled stage: the seat's money goes back to
the pool and its size is no longer in work. A freed seat rests for a minute before it serves
again, so that what it sent back has left it.

## The term

A pool starts when its owner calls `begin(terms)`, naming the hash of the pool's terms
([POOL-TERMS.md](POOL-TERMS.md)): that call is the owner's acceptance of them, and nothing is
sold before it. The text has to be in the plan's book (`TermsBook`): terms nobody can read are not
accepted. It runs for a term the plan sets, 6 to 24 months. Those two bounds, the fourteen
days below and the ninety days of a renewal are not in the plan: whoever deploys the pool sets
them, and the pool holds its plan and every extension to them. A pool deployed to watch a whole
term in an afternoon has shorter ones, and says so (`minTerm()`, `maxTerm()`, `notice()`,
`renewal()`).

How long the money is locked does not rest on the deployer's honesty. The contract itself holds
the notice to fourteen days and to a twelfth of the shortest term the deployment allows, and to
no larger a part of a renewal than fourteen days of ninety, whatever the deployment names. On a
six-month term the money can leave only after its end, and a testnet deployment whose
shortest term is three hours can still have fifteen minutes of notice and renewals of 97 minutes.
`MultiPoolReader.productBounds(pool)` is true only for a pool with the product's bounds (6 and 24 months, fourteen
days, ninety), so a page can tell the pools it offers from the ones deployed to test.

- **Silence renews.** A term nobody stops runs on for `renewal()`, ninety days in the product, on
  the same plan, and again after that. `endsAt()` is the end of the term running now.
- **A stop.** The owner may say the pool stops at the end of its term (`setStop(true)`), and take
  it back (`setStop(false)`), until that end. From the word nothing more is sold, and the term
  ends no earlier than the last exam sold has had all its steps and its trader the time to take
  the account (`lastExamEnds()`): no exam is cut short and a pass always has its whole window.
  `closing()` says nothing is sold; `saleLead()` is how long an exam may take, all its steps and
  the time to take its account. A plan whose exam has no deadline, or whose steps take the whole
  term, is refused at creation. A pool that never began has sold nothing, so its owner's stop
  ends it at once, and what it holds is the owner's to withdraw (audit M-1); it cannot begin after.
  A stop waits for the exams still running (`waiting()`), and only for them: exams already taken
  or released hold nothing (deep audit F7). Why an exam still running holds it: an exam sold is
  given its whole time, and a leaver waits for it only when their money is in funded accounts and
  the pool must stop; while it is free, the round after their end pays them. A stop after the end a reserve was for, once the term
  has renewed by itself, ends the term now, as it would have at that end, so those who were to
  leave then are not held for the renewal (deep audit F5).
- **Extending.** The owner may move the end further on, by 6 to 24 months counted from the
  end, at any time before the pool has ended; that takes a stop back. Money said to leave (the
  reserve, below) leaves only once the end it was said for has passed, never in the term's last
  days (the terms, D5 and D6; DECISIONS 103): then the same call takes it out, and with a term of
  zero it only does that, under the two conditions of any withdrawal. Before that end the call
  moves the end and nothing else.
- **What is to leave.** The owner may say how much will leave at the end of the term
  (`setReserve`); the share in work is then measured on the pool's value without it, so that
  money is not sold against: without its part of the pool as the pool is now, so that what the
  pool makes after the word is not sold against either (deep audit N4). The pool of several investors sets it to what its leaving shares are
  worth. The reserve is for the end of the term it was set in (`reserveFor()`), and it is a part
  of the pool: the reserve over the pool's value when it was set, rounded up. Once that end has
  passed and the term has renewed by itself, `extend` may take out that part of the pool as it is
  then, once, at any time, and not a unit more (audit M-2; deep audit F4). Money taken that way
  spends the reserve. A reserve set after the end is for the next one. A stop after that end ends the term
  at once (below), and then, until the renewed end (`lockedUntil()`), no more than the reserve's
  part of the pool leaves, whatever reserve is set afterwards; the rest leaves at the renewed end
  (the audit's AUD-1).
- **The end.** Only a pool its owner stopped ends. After `endsAt` nothing is sold, taken or
  opened. `endSeat(seat)`, by anyone, ends each funded stage without a breach, so its trader is
  paid their share of what it made, target or no target. Every size comes back: an exam still
  running or passed and not taken is released, a seat whose money was sent and never opened is
  emptied, and a seat waiting for its trader stops waiting. With nothing in work, all of the pool
  is the owner's to withdraw.

A pool that has ended cannot be extended.

**The plan as the pool holds it** is kept as the code of a contract made with the pool
(`planData()`), read with one call and decoded as the plan; it is out of the pool's own code,
which is close to the size a contract may have.

## The owner's money

The owner puts capital in by sending USDC to the pool's address on HyperCore. It is USDC and
nothing else: the pool counts no other token and sends no other out.

What is in is locked for the term, and for each renewal. Nothing leaves before the pool has
ended, except in the last fourteen days of a term, through `extend`. What the owner has just been sent no
longer counts as the pool's, though it may still show in its balance for a moment, so two
withdrawals in one block cannot take it under its share. After the end `withdraw` sends from
the HyperCore balance and `withdrawEvm` the prices paid for exams, which sit on HyperEVM. Every
time, what stays has to keep the sizes still spoken for inside the plan's share, and the money
promised to exams nobody has released where it is. Every payment out pays the platform 0.5 % of
it (`PLATFORM_FEE_BPS`, the terms' D10), and every exam's price does, out of the price; a free
exam pays none.

The owner is a single address. That is an investor on their own, or `PoolShares`, the contract
of a pool of several investors, which holds their shares and their rule for who stays and who
leaves when the term is extended ([POOL-SHARES.md](POOL-SHARES.md)).

## What anyone can check

The plan is public from the first block. Every sale, pass, claim and freed seat is an event of
this contract or of the exam and the seat, and `value()`, `inWork()` and the reader's `room()` can be called
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
- **Free exams go to whoever the creator invites.** The contract holds the month's number and
  one exam per invitation; who receives the invitations it cannot see.
- **Trades inside the day count only through the noter.** Without a keeper that reads fills and
  calls `noteTrade`, a trader flat at every look is closed as idle. With one, the keeper is trusted
  to say an account traded: it can keep an idle account open, and cannot close one or move money.
- **The plan is fixed.** A mistake in it is a new pool, and so is a different number of free
  exams a month.

## Tests

`test/multi/MultiPool.t.sol` and `test/multi/OwnerRunSeat.t.sol`, 90 tests on the hyper-evm-lib
simulator: 11 for the pool handed to its owner, 79 for the pool of many traders, 11 of them for
free exams. `test/multi/Deep*.t.sol`, 25 more: the deep audit's proofs of 16 October 2026 turned
round, each failing on the code before its fix. In `scripts/mutations.py`, 321 mutations of
`Pool`'s owner-run part, `MultiPool` and `FreeExams`, each caught by a named test; they run with
the rest of `test/multi/`:

```
forge test --match-path 'test/multi/*'
python3 scripts/mutations.py $(seq -f 'Y%g' 1 466)
```

## The keeper

`ops/multi_keeper.py` makes every call the pool leaves to anyone, when the pool would take it, and
holds no trader's key: it checkpoints, breaches and expires exams and releases those that are
over; sweeps, rearms, opens and finishes seats (purging the registry's spoiled keys when they
refuse an opening, and freeing a seat its account did not open in within the claim window);
says `keys_short` on every pass while no exam can be sold for want of keys; hands a funded seat to the core keeper
(`ops/keeper.py`: checkpoint, breach, recut, settleFunded) and then calls `endSeat` once the pool
is over, `hitTarget` once a stage is flat past its target, and `closeIdle` once it has gone the
plan's idle limit; and, on a pool owned by `PoolShares`, `renew` in the round's window when
somebody asked to leave. It finds the exams from the pool's `ExamSold` events, window by window,
and keeps its place in a state file.

It is also the noter when its wallet is the plan's. It reads a funded seat's fills on Hyperliquid
since the pool last saw the seat active, every hour and always before it would close the seat as
idle; a fill since then is recorded with `noteTrade`, and the seat stays open. If the fills cannot
be read, nothing is closed on that pass. Every call is tried with `eth_estimateGas` first, and a
refusal is logged by its error's name. `ops/tests/test_multi_keeper.py` holds what it decides;
36 mutations of it, K166 to K201, are each caught by a named test.

It has not run against a chain. It is started on the host by Alex's hand, after the week of
judging.

## Not done yet

- The notice itself: the contract says when the pool stops; telling traders is not written.
- The app's pages; the keeper's first run on the testnet.
- A run on the testnet to its end. The first, from c4d68b51, was under way on 15 October 2026,
  deployed with `ops/deploy_multi.py` and run a step at a time with `ops/multi_run.py`; its record
  comes with it. What it showed is in `multi_run` since: `autoclose`, the watcher an exam with a
  small target needs (its orders fill at a later block's mark, so a position in profit cannot be
  closed the moment it is); `expire`, which `release` needs first for an exam past its time; and a
  contract's refusal printed as one line with the error's name (`ops/contract_errors.py`).
- The audit's reading.
