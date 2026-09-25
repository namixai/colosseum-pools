# What the demo has actually done on chain

Status: 25 September 2026, HyperEVM testnet (chain 998). Every line here is a state a contract
holds right now, not a claim about a past run, so anyone can check it without trusting this file
or a screenshot. Where a transaction is named it was read back from its receipt.

The demo's deployment is in `deployments/testnet-demo.json`: factory
`0xf2707FCf99eD546BBA4612783761e7906FA1958e`, key registry
`0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA`, 16 published agent keys.

## Checking it yourself

You do not have to take the list below in order. The registry knows every account that has ever
held a key, so the whole set can be rebuilt from two calls:

- `KeyRegistry.bindingOf(key)` for each published key returns `(state, account, trader)`.
  `state == 3` is Retired: the key was cut when that account stopped, and it is never reused.
- The account then answers for itself: a challenge with `status()` and `breachReason()`, a pool
  with `stage()` and `fundedEndReason()`. Both enums are in `src/Types.sol`
  (`Breach`: 0 None, 1 Drawdown, 2 DailyLoss, 3 Leverage, 4 ForbiddenAsset).

The app's `#/verify/<address>` page does the same reads in a browser.

## The end states recorded so far

Five keys were Retired when this was written, one Bound to a funded stage still running and ten
Free. Those counts move as the demo runs — `freeCount()` and the bindings are the live answer,
and the rows below are the part that does not change: a retired key stays retired.

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
  address** — it found the violation and sent the stop with no one asking it to.
- second pass, `0x75f6d5f34563e31e8ffbfe6d42f856d2b795d5c78bffb3c11ddb6f5cc7c38b09`,
  block 65180551, sent by the trader.

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
- **The orders were placed by a person**, through the gateway, not by an autonomous trading
  agent. Where a demo says "AI agent", read "a program holding a wallet that signs gateway
  requests" — that part is built, but these trades were driven by hand.
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
- **Both pools above are benches, and both have soft targets.** `0x2b108c46…` asks +0.2% to
  pass (10% daily, 20% drawdown) and `0x914E4bf9…` — where the first pass, the funded-stage
  leverage stop and the daily-loss stop all happened — asks +1% on 11 USDC. Both were built to
  exercise the transitions, not to show trading, and we own both. An investor's pool sets its
  own terms: the demo pool is 8% target, 3% daily, 6% drawdown, and none of the records above
  are from it.
- **Every wallet here is ours.** The pools' owner and the trader are two keys we hold, so where
  a line says a pool's investor or its trader did something, read "we did, wearing that hat".
  The mechanism is what the chain proves; an arm's-length trader is not.
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
