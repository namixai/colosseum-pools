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

Five keys are Retired, one is Bound to a funded stage still running, ten are Free.

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

## Not demonstrated yet

- A funded stage ended **cleanly** (`stopFunded`, no rule broken) and the trader's share paid out
  of the result. This is the one that matters most — the whole promise is a share of what the
  trader earns — and it is in progress on the bench.
- `Expired` (the clock ran out with nothing broken) and `Forfeited` (the trader walked away).
- `Drawdown` and `ForbiddenAsset`, the two remaining `Breach` values.
