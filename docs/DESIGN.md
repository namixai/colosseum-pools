# Design: pools, challenges and a stop anyone can pull

Status: working design, 16 September 2026. The spike (`spike/README.md`) checks the
HyperCore behaviour this design leans on; where a live result is still pending, the section
says so.

## Roles

- **Investor.** Creates a pool, sets its rules and challenge terms, puts in capital.
- **Trader**, a person or an AI agent. Picks a pool, pays for a challenge, trades the
  challenge capital, and if the target is met, trades the pool's capital.
- **Operator** (us). Publishes the addresses of agent keys minted in the Signer enclave, runs
  the pool gateway that forwards a trader's orders to the enclave, and runs a keeper. The
  operator can't move pool capital and can't stop a stop.
- **Anyone.** Can trigger a stop when a rule is broken, and can push a finished challenge
  through settlement.

## Contracts (HyperEVM testnet)

- `KeyRegistry`: the operator publishes agent key addresses. A key moves `Free → Bound →
  Retired` and never back. A bound key belongs to one account and one trader. A key is
  approved on HyperCore once in its life (Hyperliquid warns that actions signed by a removed
  agent can be replayed once its nonces are pruned).
- `PoolFactory`: clones `Pool` and `ChallengeAccount` (EIP-1167), keeps the platform asset
  list (perp indices from the testnet `meta`), and is the only source of truth for "this
  address is one of ours".
- `Pool`: holds the investor's capital on its own HyperCore account. One trader at a time:
  `Idle → Challenge → Funded → Idle`.
- `ChallengeAccount`: one per purchased challenge, with its own HyperCore account and its own
  agent key. Its rules are copied from the pool when it is created, so the investor can't
  change them mid-challenge.

Every account the contracts create sets separate spot and perp balances (CoreWriter action
16, value 1) before any capital lands on perp. In unified mode, spot USDC would back the
trader's positions. The enclave build signs only `order` and `cancel` for Hyperliquid, so a
trader's key can't change the mode back (spike question 7 checks the contract side live).

## Rules and terms

Rules (per pool, copied into each challenge):

- `dailyLossBps`: equity may not fall more than this below the day's first snapshot;
- `maxDrawdownBps`: equity may not fall more than this below the starting capital, static;
- `maxLeverageX100`: notional over equity (both from precompile `0x80F`), above this is a
  breach;
- `assets`: perp indices the trader may hold, a subset of the platform list.

Terms (per pool): challenge price (HyperEVM USDC), challenge capital, profit target, duration,
trader's share of the challenge profit, capital for a funded trader.

Equity is `accountValue` from `accountMarginSummary` (precompile `0x80F`, dex 0), in units of
1e-6 USDC. Precompiles return the state at the start of the block.

## Lifecycle of a challenge

1. **Buy** (`Pool.buyChallenge`, the trader). The pool must be idle and hold at least the
   challenge capital on spot. The trader pays the price with HyperEVM USDC (`transferFrom`,
   so the payment is tied to the trader's address). The factory clones a
   `ChallengeAccount`, the registry reserves a free key for it and the trader, and the pool
   sends the capital to the challenge's HyperCore address (action 6). The price stays in the
   pool until the challenge starts, so it can be refunded.
2. **Start** (`activate`, anyone, a later block). Requires the account to exist on HyperCore
   and hold the capital. Sets separate balances (16), moves the capital to perp (7), approves
   the reserved key as the account's unnamed agent (9), approves the builder fee if one is set
   (12), and takes the first daily snapshot. If the capital hasn't arrived an hour after the
   purchase, `abort` refunds the price and retires the key; once the capital is there,
   `abort` is refused.
3. **Trade.** The trader signs an order with their wallet and sends it to the pool gateway.
   The gateway checks on chain that the key is bound to this account and this trader and that
   the challenge is active, then asks the enclave to sign with that key. The enclave applies
   the platform policy (asset list, size caps) and signs or refuses with a signed receipt.
4. **Stop** (`breach(cancels, assets, salt)`, anyone). Allowed only when a rule is broken at
   the start of the block: drawdown floor, daily loss, leverage, or a position in an asset
   outside the list. In this order:
   1. the account's agent is replaced with a fresh keyless address (9), and the old key is
      retired in the registry. The address is a hash of the account, a counter, the caller's
      salt and the block; candidates HyperCore already knows are skipped, and if all of them
      are taken the stop uses one anyway instead of reverting, so funding the candidates in
      advance can't block it. `recut(salt)` lets anyone replace the agent again while the
      account is stopped;
   2. the orders named by the caller are cancelled (10). No precompile lists open orders, so
      the caller reads them from the info API;
   3. every open position among the pool's assets and the caller's extra assets is closed with
      a reduce-only IOC order (1), priced off the mark with slippage and rounded to
      Hyperliquid's price rules.
   After the agent is replaced, the enclave will still sign an order for the old key if asked,
   and Hyperliquid refuses it. That is the claim we make, and the only one.
5. **Pass** (`graduate`, anyone). Allowed when the challenge is active, no rule is broken, the
   account is flat, equity is at or above the target, and the deadline hasn't passed. The
   challenge key is replaced and retired as in a stop, the trader's share of the profit is
   recorded, and the pool funds the trader: the registry binds a **new** key to the pool and
   the trader, the pool approves it as its agent (9) and moves the funded capital to perp (7).
6. **Other ends.** `expire` (anyone, after the deadline) and `forfeit` (the trader) end the
   challenge the same way a stop does.
7. **Settle** (`settle(cancels, assets)`, anyone, repeated). Cancels the orders the caller
   names (a resting order holds margin, so this can't be a one-time step), closes whatever is
   still open, and moves the free perp balance to spot (7). Once the perp side is empty it
   pays the trader's share, once and never again, and sends the rest to the pool (6) only
   after the payout shows in the balance or five minutes have passed. Each call acts on
   start-of-block state and HyperCore executes a few seconds later, so settling takes
   several calls; the challenge is `Settled` when perp equity and spot USDC are both zero.

## Funded stage

The pool account trades with the funded trader's key under the same rules, measured from the
funded start. `Pool.breach` works like the challenge stop. `Pool.stopFunded` lets the investor
or the trader end it without a breach. Either way the key is retired, positions are closed,
the perp balance goes back to spot, and the pool returns to idle.

## What the design does not do

- It does not check a trader's intent: the operator could submit an order that fits the rules
  without the trader asking for it. The Signer build used here has no such check for
  Hyperliquid.
- It does not put the investor's limits inside the enclave. Contracts enforce the pool rules
  after the fact, by stopping the account. Losses can overshoot a limit between the breach and
  the stop landing.
- The daily snapshot can only be taken in the first 15 minutes of a UTC day, once, so nobody
  can pick a convenient moment later. The keeper takes it at midnight and anyone else may.
  If nobody does, the previous snapshot stays in force, which can make the day's limit
  tighter or looser than the true start of the day.
- A payout is sent once. If HyperCore dropped it, nothing on chain resends it. After a payout,
  the rest waits until the payout shows in the balance or five minutes pass; a donation to
  the account at that moment can stretch the wait to the full five minutes, once.
- Whoever calls `checkpoint` first in the 15-minute window sets the day's base, so a trader
  who calls it at the window's lowest point gets that point as the base.
- No proof that a published key address was minted in the enclave. The Signer build doesn't
  offer one.
- Anyone can create a pool, at no cost beyond gas, and the factory's pool list has no cap. The
  keeper doesn't walk that list: it follows the pools named in the factory's
  `ChallengeCreated` events, which cost the buyer the challenge price and need the pool's
  capital in place. A long pool list still makes `PoolFactory.pools()` expensive to read for
  anyone else who calls it.
- One trader per pool, no pool shares, no leaderboard, no mainnet.
