# What the demo has actually done on chain

Status: 1 October 2026, HyperEVM testnet (chain 998). Every line here is a state a contract
holds right now, not a claim about a past run, so anyone can check it without trusting this file
or a screenshot. Where a transaction is named it was read back from its receipt.

Most of what follows is on the first deployment, `deployments/testnet-demo.json`: factory
`0xf2707FCf99eD546BBA4612783761e7906FA1958e`, key registry
`0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA`, 16 published agent keys. The live demo has since
moved to a second deployment of the same contracts, and its record has a section of its own
below. Both are on chain and both still answer; the first one's accounts did not stop being true
when the demo moved on.

## Checking it yourself

You do not have to take the list below in order. The registry knows every account that has ever
held a key, so the whole set can be rebuilt from two calls:

- `KeyRegistry.bindingOf(key)` for each key in the deployment record's `published_keys` returns
  `(state, account, trader)`. `state == 3` is Retired, and a retired key is never reused.
- The account then answers for itself: a challenge with `status()` and `breachReason()`, a pool
  with `stage()` and `fundedEndReason()`. Both enums are in `src/Types.sol`
  (`Breach`: 0 None, 1 Drawdown, 2 DailyLoss, 3 Leverage, 4 ForbiddenAsset).
- **A retired key on a POOL is not by itself a funded stage, and this is the one place the
  shortcut above misleads.** A pool takes its funded-stage key the moment the challenge is
  *sold*, bound to the buyer, so that a trader who passes cannot find the registry emptied by a
  stranger (`src/Pool.sol:232-238`). If nobody passes, `onChallengeSettled` gives that reserved
  key back unused — retired, bound to the pool and to the trader, looking exactly like a funded
  stage that ran. Worse, the pool then reads `stage()` Idle and `fundedEndReason()` **None**,
  which is also what a funded stage that ended with nothing broken reads. The event tells them
  apart: a funded stage that really started emitted `TraderFunded(trader, key, capital)`. No
  `TraderFunded`, no funded stage. On deployment 2 below there are two retired keys for one
  ending, and this is why.

The app's `#/verify/<address>` page does the same reads in a browser, and it reads **both**
deployments: the second is the live one, where pools are bought, traded and opened, and the first
is an archive, kept so its accounts can still be checked — it sells nothing. The page asks each
factory whether it made the address and then reads that deployment's own key registry, so an
account of either opens by the same link and the page says which it is. Read on the published
site, `https://pools.usenami.io`, on 3 October 2026:

- `#/verify/0xBA0c90BB481D6CAd2534AE3885d1CC9B9C30dB54` — `challenge`, `live deployment`; the rule
  the contract recorded is Leverage, the stop is at block 65746123, two fills, none outside the
  rules. It is the first row of the deployment 2 table below.
- `#/verify/0x914E4bf94751274b70855d321ec68EEf5aD79Df0` — `pool`, `first deployment, archive`; the
  funded stage ended at block 65176406 with Leverage recorded. It is the third row of the table in
  the next section.
- `#/verify/0xd4f31E7234308546c822C619705F1A4B5fC8f629`, the deployment 2 trader's own wallet —
  `not ours`: neither factory made it, and the page says there is nothing to check.

Those reads go from your browser to the public testnet RPC, which limits how much one address may
ask. A load it refuses says so on the page, and reloading gets past it: the first of our loads
that day was refused, and the same link loaded on a reload.

## The end states recorded so far

Five keys were Retired when this was written, one Bound to a funded stage still running and ten
Free. Those counts move as the demo runs — `freeCount()` and the bindings are the live answer,
and the rows below are the part that does not change: a retired key stays retired.

**One number will not reconcile, and the reason is ours.** That registry answered `freeCount()`
**41** on 1 October 2026, which cannot come from sixteen published keys: `_free` grows only in
`publish` and shrinks only when a key is handed out (`src/KeyRegistry.sol:103-135`), so keys were
published into it that `deployments/testnet-demo.json` does not name. We did not record them, and
no file here can tell you which they are. The rows below are about the sixteen the record does
name, and each still answers for itself. Said plainly rather than left for a reader to trip over:
a count in this file is checkable, and this one does not check out.

| Account | What it is | The contract's own record |
|---|---|---|
| `0xf01f16d0f933b46089947739d5147331764d612c` | challenge | Settled, `breachReason` **Leverage**, key `0xBB9EBcD7` cut at block 65149660 |
| `0x5cdd86ea90d9d8f94d7718a6e182e757ea34b9bd` | challenge | Settled, `breachReason` **None** — it **passed**; key `0x9F5B271A` cut at block 65176274 |
| `0x914E4bf94751274b70855d321ec68EEf5aD79Df0` | pool, funded stage | `fundedEndReason` **Leverage**, key `0xc2Bbf231` retired, pool back to Idle |
| `0x110Fb6B8985f89b4e651cbeb536925C5Fd1950b0` | challenge | Settled, `breachReason` **DailyLoss**, key `0x9066e1b4` cut at block 65178469 |
| `0xf4d98de2e668a8376319f54fc9592e948bd08655` | challenge | **Passed**, `breachReason` None, key `0x936433aB` cut at block 65180551 |

So the contracts have recorded, on their own accounts: a challenge stopped for leverage, a
**funded stage** stopped for leverage, a challenge stopped for a daily loss, and two challenges
passed. A pass and a stop end a challenge the same way — capital back to the pool, Settled,
nothing left on the account — and the difference is only what is written down, which is why the
pages read the recorded reason rather than measuring the wreckage afterwards.

Transactions read back from their receipts:

- funded-stage stop, `0x03b387efded880407b35d35e4ed44d7344c91662da7568c6b016458c7b1ba50f`,
  block 65176406, sent by us as the pool's owner.
- daily-loss stop, `0xf2365bf087abb81f069d51fd48ce9573ca92fb563a826c9e9cb3ba89369d3c07`,
  block 65178469, **sent by `0x75deec8b513Ee3f5D15d6b40B706A3314cf26590`, the keeper's own
  address** — it found the violation and sent the stop with no one asking it to. From chain data
  that stop looks slow: the equity crossed the day's floor around 02:38 UTC and the transaction
  landed at 02:43:16, five minutes later, against the fourteen seconds the host keeper took in
  the morning. **That gap is not detection.** No keeper was running: this one was started by
  hand, after the fact, and it reported `breach_found` on its very first pass at 02:41:44 — 224
  seconds of which it did not exist. What the chain shows for the remaining 92: the deployer sent
  that keeper gas in block 65178452 at 02:43:00
  (`0xc563490bec314ae5a156336c8410fc1ec79a834e1323745361d63e82711d08db`), sixteen seconds before
  the stop, and the keeper's address has sent **exactly one transaction in its life** — that stop.
  The reason it needed gas mid-incident, a first send refused on an empty wallet, is in the
  operator's local journal and **not in this repository**, so take it as an account rather than a
  record. Nor can it be recovered from the chain: a balance or nonce asked for at an old block
  comes back as today's, as the hosting notes set out. Both keepers saw the violation immediately
  once running; the difference between five minutes and fourteen seconds is a keeper that was
  already watching against one that was fetched to look.
- second pass, `0x75f6d5f34563e31e8ffbfe6d42f856d2b795d5c78bffb3c11ddb6f5cc7c38b09`,
  block 65180551, sent by the trader.

## Deployment 2, and the first run where the trader was not us

Everything above is on the first deployment. The live demo now runs a second one,
`deployments/testnet-demo2.json`: factory `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222`, key
registry `0x53AF27F65Dd7473c890f633aC0025b261307779e`, 24 published agent keys, deployed at block
65736733 from commit `e4287926`. Same contracts, its own registry, nothing shared with the first.

**The ending itself is not new, and saying otherwise would be the easy lie.** A challenge stopped
for leverage is the first row of the table above. What is new is who did it: the trader was a
separate AI agent on the team, not an operator of this repository. We funded its wallet
(`0xd4f31E7234308546c822C619705F1A4B5fC8f629`) with testnet USDC and gas so it could buy a
challenge at all, and the leverage breach was **arranged on purpose** — the agent was asked to
break the rule so the contract would have to write down what it does. That is a staged breach by
a separate AI agent of the team, not a trader we were surprised by — and not an arm's-length one
either, which the limit further down says plainly.

**How it was able to break the rule, since the gateway holds every key.** The entry was 0.00297 BTC
at 84076, which is **249.71 USDC of notional on 70 of capital — 3.57× against a 3× rule**. The
agent's own client would have refused it: its cap is equity × the rule × 0.8, or 168 USDC here. The
gateway would not, because **the gateway does not check leverage at all** — it reads `rules()` for
the daily and drawdown floors and discards the leverage field (`gateway/chain.py:117`), and the word
does not appear in its code. Unlike `ForbiddenAsset` below, leverage has **one** enforcement layer,
not two: the contract's `violation()` and a keeper that pulls the stop.

**That is not a decision we recorded — the check was simply never built**, and saying otherwise would
dress an absence up as a design. Two things such a check could not do are worth knowing, and they are
limits rather than reasons: it could not stop a breach the mark creates after the order is already
filled, and an equity read on every order is the per-minute budget that finding A-14 is about. What
it would buy is speed, because the contract-and-keeper path cannot answer faster than a block.

Read today, from the accounts themselves:

| Account | What it is | The contract's own record |
|---|---|---|
| `0xBA0c90BB481D6CAd2534AE3885d1CC9B9C30dB54` | challenge | `status` 8 Settled, `breachReason` **3 Leverage**, capital 70 returned, spot 0, key `0xeA688990` cut at block 65746123 |
| `0x237afA2D58B1612e19D47152FfB2E771c05Fe96D` | pool | `stage` Idle, `fundedEndReason` **None** — the reservation case, **not** a funded stage: key `0x99A4Ec10` released at block 65746163 |

`freeCount()` on that registry answers **22** of the 24, which is the two keys above and no
others.

Transactions read back from their receipts:

- the purchase, `0x287a52091d812d40a27a9dba18db394772b181942d693e9d3131166512a7c102`, block
  65746071, **sent by the trader itself** — price 7 USDC (`ChallengeSold`) and the platform's
  0.7 (`ChallengeFeePaid`). **This one transaction binds two keys**, and both `KeyBound` events
  are in it: `0xeA688990` to the challenge and `0x99A4Ec10` to the pool. The second is the
  funded-stage reservation, taken at the sale and bound to the buyer.
- the stop, `0x1804fa06b93c9cb9332925d3f0c9dc10419894bb3adc8d9b0422959229931aac`, block 65746123,
  sent by `0xD6F07317fC5f12302776b03A7206B1614FD49021`. The event is `Stopped(status 3 Breached,
  reason 3 Leverage, equity 69.884663)`, and the same transaction cut the challenge's key. Note the
  equity: against capital 70 the account was down **0.115337** — it was stopped for leverage with
  the balance all but untouched, which is what a staged leverage breach looks like from the chain.
- the last settle step, `0x9b1b842beab93c2323e7150764932871b7610ca7970d941da08980628ee87d11`,
  block 65746163, sent by `0xcbd5C0299669e0C686D375cc6C07584Ad5C4fECa` — a different address from
  the one that sent the stop. That step is also where the pool gave up its reserved key.
- **The pair anyone can check, on the venue's own fills.** `userFills` for the challenge holds
  exactly two: the entry at **13:54:54.160 UTC** (oid 61567491731, BTC buy 0.00297 at 84076) and the
  close at **13:54:57.383** (oid 61567495468, sell 0.00297 at 84075), **3.223 seconds** apart. The
  stop's block 65746123 carries 13:54:57, the same second as the close.
  **What that fixes is the order, not the mechanism.** The equity the stop recorded is 69.884663,
  which is capital 70 less the entry fee 0.112367 and the 0.00297 the position was down — the
  account with one fee paid and the position still OPEN. So the close came after the stop was
  written down. Which order filled it we did not establish: the keeper's journal would say and we
  did not read it, and HyperCore runs a CoreWriter action a few seconds after the EVM block, so a
  fill 0.383 s past that block is not on its own the stop's own close.
  **Read 3.223 as one draw, not a bound:** the
  keeper polls and sleeps 30 seconds between passes, so noticing takes anywhere from nothing to a
  cycle depending on where in it the rule broke, and this one happened to break near the start of a
  pass. The entry's fee is 0.112367 on 249.71 of notional and the close's is 0.112366 — 4.50 bps
  either way, the taker rate the gateway's allowance is derived from, and the one-unit difference is
  the close pricing a dollar lower. The two fills are
  `0x211da471d0ba0e8a2297042aa7c611010600bc576bbd2d5cc4e64fc48fbde874` and
  `0x29a6f21eddb279042b20042aa7c6480103000a0478b597d6cd6f9d719cb652ee`.
- **What those two addresses are, and what is not claimed here.** Per our own host records they
  are the two keepers' wallets, the first read from `/var/lib/colosseum-keeper/secrets/keeper.addr`
  on 25 September; the chain shows only that these two addresses sent these two transactions. **We
  did not read either keeper's journal for this run**, so this record does not say the stop was
  unprompted. Deployment 1's leverage stop above does say that, and backs it with the keeper's own
  journal timestamps and fills — that evidence is not here, and two services each sending a step
  is not the same claim.

**The second retired key is the trap the section at the top warns about.** Both retired keys name
the same trader, and the pool reads Idle with `fundedEndReason` None. From state alone that is
indistinguishable from a funded stage that ended with nothing broken — every field a reader would
reach for is cleared when a funded stage closes: `fundedTrader`, `fundedStart` (which is what
`drawdownBase()` returns), and the payout fields all go back to zero (`src/Pool.sol:485-498`).
`fundedEndReason` survives only when a stage was *stopped for a reason*, which is why deployment
1's funded-stage row can be read from state at all.

**One field does tell them apart, and we had it the whole time: `cutBlock`.** A pool records the
block where it cut the funded trader's key, and keeps it after the capital has come home. Only
`_cutAgent` writes it (`src/RuledAccount.sol:169-178`), and `Pool.onChallengeSettled` retires a
*reserved* key without going through that — so Idle with a cut block is a funded stage that is over,
and Idle without one never had one. This pool answers `cutBlock()` **0**. The rule has been in
`app/lib/verdict.js` as `pastFundedStage` since 24 September; an earlier draft of this section said
the event was the only witness, which was wrong, and `TraderFunded(trader, key, capital)` is a
second witness rather than the only one.

**Honest limit, and it is ours.** Those three transactions took an `eth_getLogs` walk back from the
head, and this node rate limited it repeatedly on the way — the shared minute of budget that
finding A-14 is about, met from the reading side. A reader starting from the record's
`published_keys` and calling `bindingOf` needs none of that; a reader trying to find the
transactions without the key list would be walking the same wall we did.

## The gateway refused two things, for two different reasons

Both were real refusals against the live gateway, not tests:

- `not_trading` (409), after a key was cut: the account is no longer in a trading state, so the
  gateway will not forward an order for it. **Honest limit:** this refusal is ours, not the
  venue's. Proving Hyperliquid itself rejects the cut key would mean signing with that private
  key, which we will not do.
- `not_your_account` (403): an order for a challenge, correctly signed, but signed by a wallet
  that is not that challenge's trader. `KeyRegistry.isBound` ties the key to the account **and**
  to the trader who bought it, and the gateway asks the chain, not its own records.

## What is staged, and what is not

The state transitions and the records above are real. How they were reached, plainly:

- **Two of the stops were deliberate, one was not.** The two leverage stops were staged: orders
  were placed to break the rule on purpose, to make the contract do its job and see what it
  writes. The **daily-loss** stop was not. That was a real attempt at the target that lost, on a
  guard of ours set too close to the floor to catch it in time, and the keeper stopped the
  account for it. It is the only record here nobody arranged.
- **On the first deployment the orders were placed by a person**, through the gateway, not by an
  autonomous trading agent. Where a demo said "AI agent", read "a program holding a wallet that
  signs gateway requests" — that part was built, and those trades were driven by hand.
  **On the second deployment that is no longer the case, and the record says which.** The
  challenge in the deployment 2 section was bought and traded by another AI agent of this team,
  holding its own wallet, through the same gateway: the purchase transaction was sent by that
  wallet, not by us. What it is not is evidence the program trades well — it was asked to break
  the leverage rule, and that is what it did. A trader passing a challenge on its own is still
  not here.
- **The keeper's stop was its own**, and it was run from a laptop. `ops/keeper.py` found the
  daily-loss violation and sent the transaction from the keeper's address
  (`0x75deec8b…`). Two honest qualifiers: that run was not the pools host, and its discovery
  state was seeded with the pool it follows rather than replaying ~180k blocks of logs. Finding
  the violation, choosing the call and sending it were all the keeper's.
- **The keeper on the pools host runs and acts, since 25 September.** Until that morning it had
  been reading the chain through a node that refuses its log queries, so it followed **zero**
  pools and sat 175,000 blocks behind the head — enforcing nothing, while saying nothing was
  wrong. Pointed at a node that serves logs, it caught up and went to work on its own:
  `0xf140a7500d5a2c7525a27fc1cc94b85d586c866c8a078ce48b5061087d2511bf`, a `settle` sent from the
  host keeper's address `0xD6F07317fC5f12302776b03A7206B1614FD49021`, which returned 5.03 USDC
  left stranded in a passed challenge and freed the pool to sell the next one. Nobody asked it
  to.
- **And then it stopped a trader.** Later the same morning a funded account on pool
  `0x2b108c46…` was taken to 5.58× against a 5× rule — **deliberately, by us**, to see what the
  host would do with it. What it did, from its own journal: `breach_found` at 10:16:57 UTC,
  reason 3; `breach` **sent at 10:16:59**. Two seconds. The transaction is
  `0x7fa53a03f3aba4ac42a0e38f46103849cb95c1b425a93996e571f78ac72b15b2` in block 65206003, from
  `0xD6F07317fC5f12302776b03A7206B1614FD49021`, and the pool now records `fundedEndReason` **3
  (Leverage)** with the agent key cut in that same block; the keeper went on to `settleFunded`
  by itself. The staged half is the rule-breaking. **Nobody chose the stop, timed it or sent
  it** — the person who set the trap was still loading the page when it sprang.

  Those two seconds are the last leg only — from the keeper noticing to the keeper sending — and
  a reader could take them for the system's answer time, which they are not. The pair anyone can
  check is on the pool's own fills: the order that broke the rule filled at **10:16:45.354 UTC**
  (oid 61008081414, SOL buy 0.47 at 118.23) and the position was closed at **10:16:59.363** (oid
  61008094170, sell 0.47 at 118.14), the stop's block 65206003 carrying 10:16:59. **14.009
  seconds from the breach to the close**, both fills public under `userFills` for
  `0x2b108c46…`.

  What that number is made of: the keeper polls, it does not listen. The unit sleeps 30 seconds
  after each pass, so a cycle is 34–35 seconds, and noticing takes anywhere from nothing to a
  cycle depending on where in it the rule broke — here about 12 seconds — plus a block. The 14
  seconds is one draw from that range, not an average of anything: one stop is one stop.

  **A cycle bounds noticing only while every pass succeeds and reads the pools that exist now.**
  It is a bound on the good case, not a guarantee. A pass that throws is logged as `pass_failed`
  and the keeper waits for the next one, so that cycle finds nothing; this file records both
  failures that do it — a node rate-limiting the reads (`eth_blockNumber: rate limited 6 times in
  a row`, on this host the same day) and a node refusing the log queries entirely, which left the
  keeper following **zero** pools for the life of a deployment while every pass still reported
  success. A keeper that has not discovered a pool has no bound on it at all.
- **Two of the three pools above are benches with soft targets.** `0x2b108c46…` asks +0.2% to
  pass (10% daily, 20% drawdown) and `0x914E4bf9…` — where the first pass, the funded-stage
  leverage stop and the daily-loss stop all happened — asks +1% on 11 USDC. Both were built to
  exercise the transitions rather than to show trading, and we own both.
- **The third is not a bench, and neither are its terms.** The first row — challenge
  `0xf01f16d0…`, stopped for leverage — ran on the demo pool
  `0x2839d3c872ce82151a16afe0315756915d8a9b79`: **8% target, 3% daily, 6% drawdown**, the terms
  an investor would actually set. That pool was opened from another wallet of
  ours through the site's own form, capital sent by hand on HyperCore, not deployed by
  a script. So the softest thing about that record is nothing: the rule it broke is the rule a
  real pool would carry.
- **Every wallet here is ours.** Four of them: the deploy key that owns the two benches
  (`0x00d014df…`), the trader (`0xdc87191c…`), the one that opened the demo
  pool, which is named here only as that, and the deployment 2 trader
  (`0xd4f31E7234308546c822C619705F1A4B5fC8f629`) — another AI agent of this team, whose testnet
  USDC and gas we sent it, not an outside party. The first two belong to the project and the
  third belongs to a person, and a short address is still an address. So where a line says a
  pool's investor or its trader did something, read "we did, wearing that hat" — with the fourth
  as the one exception, where a teammate's program wore it and we only paid for the seat.
  The mechanism is what the chain proves; an arm's-length trader is not.
  The keeper's two addresses are ours as well, and the only actor here that decided anything
  without being told.
- **The money is testnet money** and the USDC is mock.

## A funded stage ended cleanly, and the trader was paid

This is the one the whole design is for, and it closed on 25 September on the soft bench
`0x2b108c465786b040355dcefe1b721e3e6276e80c`. The stage was ended from the funded trader's own
wallet with `stopFunded` — `0xaee60fe87fe216ffffd00986088b3814818dee1f6ef06eb642ee7722c87ea271`
— which records `Breach.None`: the contract was not made to stop it, it was let go. The call came
from a watcher of ours using that wallet, and the wallet is ours too; what this shows is the
path, not a trader we do not control.

The share is not computed at the stop. `settleFunded` takes the result at the first step with
nothing open, which is what closing actually realized rather than the mark it was priced from,
and the events carry it:

- `FundedResult(trader, 10012723, 1017840)` in
  `0xab49fd6e3b4447a0321651e8d186e759569e3c90c6a92e84982675bf47601e5e` — the stage came home at
  **10.012723** against a `fundedStart` of 10.000000, a gain of **0.012723**, and the trader's
  80% of it is **0.0101784**.
- `FundedPayoutSent(trader, 1017840)` in
  `0xb1733533e70b08ef75efc310de11a02a62ef1aa490455a01d1d7409b7b8f9f5a` — it was sent.
- The trader's HyperCore spot balance went from **0.5** to **0.5101784**. That is the number that
  matters: a balance that moved, not a field a contract set.

**Read this one from the events, not from the pool.** When the pool returns to Idle it clears
`fundedResult` and `fundedPayoutOwed` for the next challenge, so reading them now gives zeroes.
The transactions above are the durable record. It is also why the amount is small: the bench
trades 10 USDC and the gain was a twelfth of a percent. The share is 80% of whatever the stage
earns, and the arithmetic is the same at any size.

## The fourth ending, on the rehearsal deployment

A challenge can also simply run out of time with nothing broken, and that one is not on the demo's
factory — it is on the rehearsal deployment (`deployments/testnet-rehearsal.json`, factory
`0x52A141515570eA66053D29bBCb61042e5693970D`), which runs the same contracts under a registry of
its own. Said plainly so the counts above still add up: the key it cut is not one of the demo's
sixteen.

Challenge `0x71bd0281f0099b87634464dd00c512aca4972018`, bought 25 September and given seven days,
was expired the moment its deadline passed, by `expire`
(`0xa51f272a6ff7a5a295e8672de2d2f75a57d7796d27b22d480f160e58d7c644dc`). It holds `status` 4
(Expired) with `breachReason` **0 (None)** — the distinction the whole design turns on: the
account stopped, and nothing says the trader did anything wrong. Three `settle` calls returned
the capital and the pool went back to Idle, whereupon its owner withdrew it
(`0x1183d9590d1a2db7655cc431f64dd57f9f58a53c41c832d2bfc6199fa38ed924`).


## A trader walked away

`Forfeited` is the ending nobody talks about and every prop firm has: the trader takes the
challenge, looks at it, and leaves. Only the trader can do it — `forfeit` reverts for anyone else
— and it is the one ending that says nothing about how they traded, because they need not have
traded at all.

On pool `0x9bc941cd7980a2564b06fd6baeb56b09a765237b`, challenge
`0xd736550af8ad2284fb67823aa3ca01145656e7d7` was bought, activated and forfeited without a single
order: `0x119b8243e0d5c2626cd77e031c93d04ff5c2e904247791d5b2ad499695a41608`, sent by the trader.
**Read this one from the event, not from `status()`.** The walk-away is
`Stopped(status 5 Forfeited, reason 0 None, equity 3000000)` in that transaction: it ended as
Forfeited, with nothing held against the trader, and the equity was **3.000000** — exactly the
capital it was given, untouched. Three `settle` calls then returned that capital to the pool and
carried the account on to `Settled`, so `status()` answers **8** today. The account is telling
the truth and so is this file; they are answering different questions, and the end state a
challenge passes through is only in its events.

## ForbiddenAsset, and why it is not in the list above

`Breach.ForbiddenAsset` cannot be reached through this demo, and that is by construction rather
than for want of trying. The gateway refuses an order whose asset is not in the pool's rules
before it signs anything (`asset_not_allowed`, 403), so no position in a forbidden asset can be
opened by a trader going through it — and in this demo the gateway holds every key.

The contract's check is the second layer, and it is there for the case the first cannot cover: a
key our gateway does **not** hold. If an account's agent key ever signed elsewhere, or the
operator submitted an order around its own checks, the position would still be visible to
`violation()` and anyone could pull the stop. Demonstrating it would mean signing with a key
outside the gateway on purpose, which is the one thing the design says nobody does.

So: not a gap in the evidence, a consequence of the layering. Worth saying plainly rather than
leaving a reader to wonder which it was.

## Not demonstrated yet

- `Drawdown`, the last of the three rules the pools enforce.
