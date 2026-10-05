# Design: pools, challenges and a stop anyone can pull

Status: working design, 16 September 2026, updated on 17 September after the live spike, and on
28 September with the stop and the take on the exchange ("Three layers").
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
one agent key for good, so without a fee a pool's owner could sell challenges to themselves at
whatever price they liked and use up the published keys cheaply. Not for nothing, though: the
paragraph above says a price of zero is refused at creation, and it is (`PoolFactory`), so the
owner would be paying themselves the price while the fee is the only part that leaves. This is the
fee of the contracts on testnet, a flat placeholder. What the platform is meant to charge on mainnet
is set out on the site's Economics page, under "How the platform earns"; the contracts described
here charge none of it.

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
   refused before anything is signed. Before an order that may open a position goes, the
   gateway puts a stop at the account's rule line and a take at its target on Hyperliquid
   itself (see "Three layers" below).
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

## Three layers

A pool's rules are held in three places, each covering what the one before can't.

1. **Before signing: the policy.** The gateway checks on chain that the key is bound to this
   account and this trader and that the account may trade, keeps to the platform's assets and
   caps, and refuses an order that may open a position when equity is already at the rule line
   or a challenge is at its target. It binds only what goes through the gateway.
2. **On the exchange: the stop and the take.** Before any order that may open or grow a position,
   the gateway puts a stop at the rule line (the price at which equity would reach the higher of
   the drawdown floor and the day's floor, the budget shared across positions in proportion to
   their notional) and a take at the target (the pass target in a challenge; in a funded stage,
   at most one target's worth of the equity it has), as reduce-only position TP/SL orders on
   Hyperliquid for the whole position. Hyperliquid closes at the line on the mark price without
   waiting for anyone. The trader may move a stop nearer the mark and a take within the target,
   and may cancel neither. A sweep puts back what a position that opened later is missing.
   docs/GATEWAY.md has the arithmetic and each assumption it rests on.
3. **After the fact: the contract.** Anyone may stop an account that breaks a rule at the start
   of a block; the keeper looks every 30 seconds, cuts the key, cancels and closes (above). It
   also says in its log when an open position has no stop on the exchange, which it can't place
   itself: it holds no agent key.

The second layer exists because of the delay in the third. In the cascade package (`stress/`,
Bybit bars, not Hyperliquid), the worst entry of 10 October 2025 lost 83.7–94.1 % of the stress
test's pool on the long side when each stop filled a minute late, and 3 of its 5 seats were
liquidated. A stop on the exchange takes that minute out. It does not take out slippage: a
stop-market in a cascade fills below its trigger, within Hyperliquid's 10 % tolerance for a
triggered market order, and how far below on a day like that is not known: Hyperliquid's book has
been measured only in a calm market, never in a cascade. So the honest claim is the delay taken
out, not a loss figure. And the line is the rule itself: when a stop fires, the account is at its
limit, the fee and the fill take it past, and the stage ends through the contract as before.

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
  The release is refused while a key is there to be had: the pool checks, at the moment it is
  asked, that the key it reserved is spoiled and that the registry lists nothing free. So a week of
  nobody bothering to call `openFundedStage` cannot cost a trader their stage — only a week with no
  key can.
  An owner who wants that outcome has to make it true and leave the traces: give every free key a
  HyperCore account (about 1 USDC apiece), including the one the sale reserved, and then call
  `KeyRegistry.purgeSpoiled` so the count actually reaches zero. That last call is public and
  anyone can make it, which is the point — the registry has to be visibly empty, not merely full of
  keys nobody checked. What stands against the whole thing is cost and daylight, not a rule. The
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
- **Nor profit from funding, so a pass can be collected with the price hedged away.** Funding
  paid to the account is perp equity like any other, and the target counts it. A trader who is
  short on the challenge while longs are paying, and holds the same size long on a wallet of
  their own, has no net exposure to the price while both legs are open: what one loses the
  other gains. The challenge leg collects funding hour after hour and the hedge pays the same
  amount out. The hedge does not shelter the challenge account itself: a move against the short
  still lowers its equity, and a large enough one breaks a rule and ends the challenge before
  funding has reached the target. Then the trader is out the price and the fee, and holds the
  gain on the other leg. Short of that the challenge reaches its target, and what the pass cost
  is the funding paid on the hedge — the target itself — less the trader's share of it, plus the
  fees: the price of the bought pass above, paid by the hour instead of at once. The size of it
  was measured from the other side on 3 October 2026, when a long of about 99 USDC paid
  0.178665 USDC in six hours; a short of that size would have been paid the same. No fix on
  chain, for the same reason as above. Named by the audit, 3 October 2026.
- **The owner's withdrawal does not check the balance, and neither its receipt nor its event
  shows that money moved.** `withdrawOnCore` asks HyperCore to send the amount and emits
  `WithdrawnOnCore(owner, amount)` whatever the pool holds (`src/Pool.sol`). HyperCore acts on
  the request a moment later, or drops it without a word, and it drops one that asks for more
  than the spot balance. **Measured** on 4 October 2026 on a pool of the first deployment that
  held 771.556173 USDC: three calls asking for 771.56 were mined, each with its event, and
  nothing left the pool; a fourth, for 771.55, sent it. The log of that pool now holds three
  withdrawals that did not happen. The second deployment's `Pool` is the same code. So read a
  withdrawal off the spot balance, never off the transaction — **and read it in a later block.**
  HyperCore acts on the request after the block that carried it: in the seven transfers read
  back on 4 October 2026 its ledger shows the action 0.36 to 0.51 of a second after the block's
  timestamp. The balance precompile answers with the state at the start of its block, so a
  balance that has not moved inside that block says nothing yet, and a second request made on
  the strength of it is a second withdrawal. Not changed in the contract: a check against the
  balance precompile would refuse the over-ask and would still not see a second withdrawal in
  the same block, which is the blindness of A-08. On mainnet the call has to compare the amount
  with the balance before it asks, and the event has to be read as a request, not as a receipt.
- It does not check a trader's intent: the operator runs the gateway and could submit an
  order that fits the rules without the trader asking for it.
- In the demo the gateway holds the agent keys. A key file can sign anything Hyperliquid lets
  an agent sign, so whoever controls the gateway host can trade those accounts, within the
  caps or not, until someone stops them.
- It does not refuse an order for the room it would use under the investor's limits; the gateway
  applies one platform policy, refuses to open at the rule line, and puts a stop at that line on
  Hyperliquid before anything may open. The contracts still enforce the rules after the fact, by
  stopping the account. Losses can overshoot a limit: by the stop's slippage when it fires, and
  by the keeper's delay when there is no stop on the exchange — a position that opened from a
  resting order in the seconds before the sweep reached it, or a gateway that is down.
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
- The platform fee is the brake on BUYING up the agent keys, and it is not the only way keys go.
  At a fee of zero the owner of a pool can buy its challenges themselves — paying themselves the
  price, since zero is refused at creation — and burn one key each. But a key can also be spoiled
  without buying anything at all, for about 1 USDC and no fee (below), so the fee is a brake on one
  road and not on the other. It used to say "the only brake", which was wrong in both halves.
  The operator sets the fee and can change it at any time, including between a buyer's approval and
  purchase; a buyer who approves exactly price plus fee can't be charged more.
- The factory's operator sets the platform fee and who receives it, the builder and its largest fee,
  and the list of assets a new pool may choose from. None of this moves a pool's capital or changes
  the rules of a pool that exists. A new fee applies to purchases made after it, and a new asset
  list to pools created after it. A new builder is approved by an account when it is prepared or
  activated, and anyone may call `prepareAccount` on an existing pool again; a builder is paid only
  on an order that names it, and orders are signed with the agent key. The role itself moves in one
  call, `setOperator`, to any address, with no second step; `KeyRegistry` makes its new operator
  accept. A wrong address loses the role for good, and these settings then stay as they were (A-20).
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
  a buyer the fee, costs the attacker about 1 USDC per key — and costs the POOL 1 USDC as well,
  which the earlier version of this line left out: creating the spoiled challenge's HyperCore
  account is paid for by the sender, and an abort returns the price and the capital, not that.
  What happens when someone sends USDC to a key that is ALREADY an agent is no longer untested:
  measured on testnet on 25 September 2026, the agency survives it. The address keeps signing for
  the account exactly as before, so a live trader cannot be switched off this way. HyperCore's rule
  is about becoming an agent, not about staying one — which is why the window that matters runs
  from a key being reserved to it being set as the agent, and both ends of it are guarded
  (`ChallengeAccount.activate`, `Pool.openFundedStage`). Those two guards read the key's state
  **at the start of their block**, and the agent is set after it, so a spoiling transfer landing
  in the same block is not seen by them — HyperCore then ignores the assignment silently and the
  account has an agent that cannot sign. Whoever does that has to land in one particular block
  rather than any time in the week the key sits there, which is a much smaller window and not a
  closed one. Raised by the audit, 28 September 2026.
- One trader per pool, no pool shares, no leaderboard, no mainnet.
