# Design: pools, challenges and a stop anyone can pull

Status: working design, 16 September 2026, updated on 17 September after the live spike.
`spike/README.md` has the HyperCore behaviour this design leans on and how each piece was
checked.

## Roles

- **Investor.** Creates a pool, sets its rules and challenge terms, and puts in capital with a
  HyperCore spot transfer to the pool's address.
- **Trader**, a person or an AI agent. Picks a pool, pays for a challenge, trades the
  challenge capital, and if the target is met, trades the pool's capital.
- **Operator** (us). Makes the demo's agent keys (ordinary testnet keys) and publishes their
  addresses, runs the pool gateway that holds those keys and signs a trader's orders after
  checking the platform's caps, and runs a keeper. Usenami Signer, our enclave service, takes
  no part in the demo. The
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
  `Idle → Challenge → Funded → Idle`. Capital arrives as a spot transfer on HyperCore. There
  is no HyperEVM deposit: on testnet the USDC bridge credited nothing to a contract bridging
  to itself (spike question 5).
- `ChallengeAccount`: one per purchased challenge, with its own HyperCore account and its own
  agent key. Its rules are copied from the pool when it is created, so the investor can't
  change them mid-challenge.

Every account the contracts create sets separate spot and perp balances (CoreWriter action
16, value 1) before any capital lands on perp. In unified mode, spot USDC would back the
trader's positions. The setting held on testnet (spike question 7). A plain agent key can
switch it back. The pool gateway signs only orders and cancels, so a trader can't; whoever
holds the gateway's key files could.

## Rules and terms

Rules (per pool, copied into each challenge):

- `dailyLossBps`: equity may not fall more than this below the day's first snapshot;
- `maxDrawdownBps`: equity may not fall more than this below the starting capital, static;
- `maxLeverageX100`: notional over equity (both from precompile `0x80F`), above this is a
  breach;
- `assets`: perp indices the trader may hold, a subset of the platform list.

Terms (per pool): challenge price (HyperEVM USDC), challenge capital, profit target, duration,
trader's share of the challenge profit, trader's share of the funded profit, capital for a
funded trader. The two shares are separate numbers: a pool pays for performance on its own
capital without having to pay the same rate for passing the audition, and either may be zero.
A price of zero is refused at creation — a pool that gives challenges away spends the
investor's capital and the published agent keys on nothing.

Platform fee (per factory, set by the operator, zero by default; the testnet deployment sets
0.7 test USDC): paid by every challenge buyer on top of the price, to the operator's fee
recipient, and not refunded. A challenge uses
one agent key for good, and a pool's owner can sell challenges to itself at price zero, so
without a fee anyone could use up the published keys for the cost of gas.

Equity is `accountValue` from `accountMarginSummary` (precompile `0x80F`, dex 0), in units of
1e-6 USDC. Precompiles return the state at the start of the block.

## Lifecycle of a challenge

1. **Buy** (`Pool.buyChallenge`, the trader). The pool must be idle and hold on spot the
   challenge capital, the funded capital and 1 USDC more (`capitalNeeded()`): HyperCore
   charges the sender that much on top when a transfer creates an account, and the
   challenge's account is always new. The trader pays the price with HyperEVM USDC (`transferFrom`,
   so the payment is tied to the trader's address), and the platform fee if one is set. The
   factory clones a
   `ChallengeAccount`, the registry reserves a free key for it and the trader, and the pool
   sends the capital to the challenge's HyperCore address (action 6). The price stays in the
   pool until the challenge starts, so it can be refunded.
2. **Start** (`activate`, anyone, a later block). Requires the account to exist on HyperCore
   and hold the capital. Sets separate balances (16), moves the capital to perp (7), approves
   the reserved key as the account's unnamed agent (9), approves the builder fee if one is set
   (12), and takes the first daily snapshot. If the capital hasn't arrived an hour after the
   purchase, `abort` refunds the price and retires the key; once the capital is there,
   `abort` is refused, unless the reserved key has gained a HyperCore account
   (`keySpoiled()`): then the challenge can't start and anyone may abort it at once.
3. **Trade.** The trader signs an order with their wallet and sends it to the pool gateway.
   The gateway checks on chain that the key is bound to this account and this trader and that
   the challenge is active, checks the platform's caps (asset list, a size cap per asset,
   400 USDC per order), and signs with that key, which it holds. An order over a cap is
   refused before anything is signed.
4. **Stop** (`breach(cancels, assets, salt)`, anyone). Allowed only when a rule is broken at
   the start of the block: drawdown floor, daily loss, leverage, or a position in an asset
   outside the list. In this order:
   1. the account's agent is replaced with a fresh keyless address (9), and the old key is
      retired in the registry. The address is a hash of the account, a counter, the caller's
      salt and the block; candidates HyperCore already knows are skipped, and if all of them
      are taken the stop uses one anyway instead of reverting, so funding the candidates in
      advance can't block it. HyperCore ignores an agent address that already has an account
      (spike question 8), so a stop that had to use a taken candidate leaves the old key in
      place. The account keeps the cut key (`cutKey`) and the block of the latest replacement
      (`cutBlock`); `recut(salt)` lets anyone replace the agent again while the account is
      stopped, and the keeper does so when `cutKey` still acts as the account's agent a few
      blocks later;
   2. the orders named by the caller are cancelled (10). No precompile lists open orders, so
      the caller reads them from the info API;
   3. every open position among the pool's assets and the caller's extra assets is closed with
      a reduce-only IOC order (1), priced off the mark with slippage and rounded to
      Hyperliquid's price rules.
   After the agent is replaced, the gateway could still sign an order for the old key, and
   Hyperliquid refuses it. That is the claim we make, and the only one.
5. **Pass** (`graduate`, anyone). Allowed when the challenge is active, no rule is broken, the
   account is flat, equity is at or above the target, and the deadline hasn't passed. The
   challenge key is replaced and retired as in a stop, the trader's share of the profit is
   recorded, and the pool funds the trader: the registry binds a **new** key to the pool and
   the trader, the pool approves it as its agent (9) and moves the funded capital to perp (7).
6. **Other ends.** `expire` (anyone, after the deadline) and `forfeit` (the trader) end the
   challenge the same way a stop does.
7. **Settle** (`settle(cancels, assets)`, anyone, repeated). Cancels the orders the caller
   names (a resting order holds margin, so this can't be a one-time step), closes whatever is
   still open, and moves the free perp balance to spot (7), but only once nothing is open at
   all: a position in an asset the caller didn't name still shows in the account's notional,
   and settlement waits for it. Once the perp side is empty it
   pays the trader's share, once and never again (a trader with no HyperCore account yet gets
   the share less the 1 USDC that creating the account costs, and a share smaller than that
   isn't sent), and sends the rest to the pool (6) only
   after the payout shows in the balance or five minutes have passed. Each call acts on
   start-of-block state and HyperCore executes a few seconds later, so settling takes
   several calls; the challenge is `Settled` when perp equity and spot USDC are both zero.

## Funded stage

The pool account trades with the funded trader's key under the same rules, measured from the
funded start. `Pool.breach` works like the challenge stop. `Pool.stopFunded` lets the investor
or the trader end it without a breach. Either way the key is retired, positions are closed,
the perp balance goes back to spot, and the pool returns to idle.

The trader's share of a funded profit (only when the stage ends without a breach) is computed
from what closing realized: the account value at the first `settleFunded` step with nothing
open, before any of it moves to spot. The equity at the stop is kept for the record only. A
reduce-only close can fill worse than the mark it was priced from, and a share computed from
the mark would make the investor pay the difference.

## What a cycle costs the pool, and why the two sides don't meet

A pool that has run a full cycle usually can't sell the next challenge out of what came home,
and the reason is structural rather than a loss.

The two figures live on different sides of the bridge. `capitalNeeded()` is spent on
**HyperCore**: the challenge capital, the funded capital, and 1 USDC for creating the
challenge's account — and HyperCore keeps that dollar. The price the trader paid arrives on
**HyperEVM**, where `buyChallenge` books it into `earned` for the investor to withdraw. So each
challenge a pool sells moves at least 1 USDC from its HyperCore balance to its HyperEVM one, and
nothing on chain moves it back: `withdrawEarned` pays the investor on HyperEVM, and the pool is
refilled only by a spot transfer the investor makes on HyperCore. The step is manual by design
— sending USDC to a pool on HyperEVM would lose it, since the testnet bridge doesn't credit
contracts — but it was not written down anywhere, and the pool page offered the sale regardless.

Measured on the stand pool `0x914E4bf9` (testnet, 25 Sep 2026), over one whole cycle — a
challenge sold at a price of 1.00, passed at 11.1135, the trader funded with 30, the funded
stage stopped on a leverage breach and settled home — it needs 42.000000 USDC on HyperCore to
sell the next one and had 40.781793 when everything had settled, so it is 1.218207 short.
HyperCore kept 1.000000 of that for the account; closing the funded stage cost 0.331676
(30.000000 out, 29.668324 back); and the challenge returned 11.113469 against the 11.000000 it
took, its 0.113469 gain going the other way.

The 1.00 is the floor: even a cycle that loses nothing leaves the pool a dollar short on
HyperCore while holding a dollar more on HyperEVM. The demo pool shows the same thing at its own
size — 769.56 on HyperCore against the 771.00 it needs.

The economics model already prices the fee (`new_account_fee = 1` in `app/data/calc_tables.json`;
zeroing it moves the demo's price floor from 4.0493 to 3.1306). What it doesn't say is that the
dollar leaves one balance and lands in another, so the pool needs the investor to close the loop
before it can sell again.

## What the design does not do

- **A pool's owner can buy their way out of funding a trader who passed, for about the price of
  the keys.** When a trader passes, the pool holds its capital in a stage that waits for an agent
  key, so the owner cannot simply withdraw from under them. That wait has to end, though: if no
  key is ever published the capital would be locked for good, which is worse than the hole the
  wait closes. So after seven days -- one challenge term -- anyone may call
  `abandonFundedStage`, the pool returns to Idle, and the trader keeps the pass and the challenge
  share they earned while the event records that this pool never funded them.
  An owner who wants that outcome can arrange it: give every free key in the registry a HyperCore
  account (`KeyRegistry` then retires each one, about 1 USDC apiece), including the key the sale
  reserved, and wait the week out. What stands against it is cost and daylight, not a rule. The
  operator can publish keys at any time and each one has to be spoiled again; the count the
  gateway holds and the registry's `freeCount()` are both public, so the two drifting apart is
  visible, and `docs/HOSTING.md` says to keep them reconciled. The release is open to anyone
  rather than to the owner on purpose -- the owner is the one who gains from it, so it should not
  be theirs alone to trigger. Found while designing the fix for A-02 and written down rather than
  left for someone else to find; the seven days and the "anyone" are one line each to change.
- **It cannot tell profit from a deposit, so a pass can be bought.** The target and the
  trader's share are both measured from the challenge account's perp equity
  (`ChallengeAccount.graduate`), and that number rises for any USDC sent to the account —
  a trade is not required and the contract cannot see the difference. A trader who sends
  their challenge the target amount passes without trading, takes their share of that
  "profit" back, and the pool funds them. The price of a bought pass is the target less the
  trader's share of it, plus the challenge price and the fee. On chain there is no fix:
  HyperCore offers no precompile that separates an incoming transfer from a realised gain.
  So read a pass as "the account reached the target", not as "this trader can trade", and
  price the pool on the first reading. Found by the audit, 25 September 2026.
- It does not check a trader's intent: the operator runs the gateway and could submit an
  order that fits the rules without the trader asking for it.
- In the demo the gateway holds the agent keys. A key file can sign anything Hyperliquid lets
  an agent sign, so whoever controls the gateway host can trade those accounts, within the
  caps or not, until someone stops them.
- It does not check the investor's limits before signing; the gateway applies one platform
  policy. Contracts enforce the pool rules after the fact, by stopping the account. Losses can overshoot a limit between the breach and
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
- Nothing on chain says who holds a published key. In the demo the operator does.
- Anyone can create a pool, at no cost beyond gas, and the factory's pool list has no cap. The
  keeper doesn't walk that list: it follows the pools named in the factory's
  `ChallengeCreated` events, which cost the buyer the challenge price and need the pool's
  capital in place. A long pool list still makes `PoolFactory.pools()` expensive to read for
  anyone else who calls it.
- The platform fee is the only brake on using up the agent keys. At zero, anyone can buy
  challenges from their own zero-price pool and burn one key each. The operator sets the fee
  and can change it at any time, including between a buyer's approval and purchase; a buyer
  who approves exactly price plus fee can't be charged more.
- USDC sent to a pool on HyperEVM is lost on testnet: the bridge doesn't credit contracts.
  The contracts have no entry point for it, and the app says so, but nothing stops a plain
  ERC-20 transfer to the pool's address.
- Someone who funds every stop candidate in advance keeps the old key trading until a `recut`
  lands on an address with no account. The candidates depend on the block and on the caller's
  salt, creating each candidate's account costs 1 USDC, and the keeper tries again with a new
  salt on every pass. Settlement drains the account either way.
- Anyone can send USDC to a published key address, which gives it a HyperCore account, and
  HyperCore won't take it as an agent then. The registry retires such a key instead of
  handing it out, and a challenge whose reserved key was spoiled before the start can be
  aborted at once for a refund of the price. The platform fee isn't refunded, so this costs
  a buyer the fee and costs the attacker 1 USDC per key. What happens when someone sends
  USDC to a key that is already an agent is untested.
- One trader per pool, no pool shares, no leaderboard, no mainnet.
