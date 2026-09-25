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
  block 65176406, sent by the pool's investor.
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

- **The breaches were deliberate.** Orders were placed to break a rule on purpose. Nobody lost
  money by accident; the point was to make the contract do its job and to see what it writes.
- **The orders were placed by a person**, through the gateway, not by an autonomous trading
  agent. Where a demo says "AI agent", read "a program holding a wallet that signs gateway
  requests" — that part is built, but these trades were driven by hand.
- **The keeper's stop was its own.** `ops/keeper.py` found the daily-loss violation and sent the
  transaction from the keeper's address. Two honest qualifiers: it was run from a laptop rather
  than the pools host, and its discovery state was seeded with the pool it follows instead of
  replaying ~180k blocks of logs. Finding the violation, choosing the call and sending it were
  all the keeper's.
- **Pool `0x2b108c46…` is a bench with soft terms** — +0.2% to pass, 10% daily, 20% drawdown —
  built to exercise pass → funded → clean stop → payout, not to show trading. An investor's pool
  sets its own terms; the demo pool is 8% target, 3% daily, 6% drawdown.
- **The money is testnet money** and the USDC is mock.

## A funded stage ended cleanly, and the trader was paid

This is the one the whole design is for, and it closed on 25 September on the soft bench
`0x2b108c465786b040355dcefe1b721e3e6276e80c`. The funded trader ended their own stage with
`stopFunded` — `0xaee60fe87fe216ffffd00986088b3814818dee1f6ef06eb642ee7722c87ea271` — which
records `Breach.None`: nobody stopped them, they stopped.

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

That leaves one ending unseen on either deployment: `Forfeited`, where the trader walks away.

## Not demonstrated yet

- `Forfeited`, where the trader walks away.
- `Drawdown` and `ForbiddenAsset`, the two remaining `Breach` values.
