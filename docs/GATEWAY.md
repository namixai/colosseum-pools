# Pool gateway

Status: built and tested offline (`gateway/`), 17 September 2026; the demo signer added the same day. Running on the pools host since 18 September 2026, against the rehearsal deployment (docs/HOSTING.md). The stop and the take on Hyperliquid (below) added on 28 September 2026, and checked live on testnet on 29 September through a gateway running this code: both placed before the order, the refusals and the moves, and each of them firing on its own (`spike/README.md`, "The live check through the gateway").

The gateway sits between a trader and Hyperliquid. It asks one question before it forwards an
order: is this key bound, on chain, to this account and this trader, and is the account allowed
to trade right now? The answer comes from the chain, not from our database.

In the demo the gateway also holds the agent keys and signs with them (`GATEWAY_SIGNER=demo`).
The keys are ordinary testnet keys made for the demo (`ops/make_demo_keys.py`). In the product
that job would go to Usenami Signer, our existing enclave service, and the gateway's `signer`
mode is written for it, but the demo doesn't use that mode or the enclave.

## Request

`POST /v1/order`, one order or one cancel per request:

```json
{
  "kind": "order",
  "order": {
    "account": "0x… challenge or pool address",
    "asset": 3, "isBuy": true, "limitPx": "60000", "size": "0.0002",
    "reduceOnly": false, "tif": "Alo",
    "nonce": 1789600000000, "expiresAt": 1789600060000
  },
  "signature": "0x… EIP-712 signature by the trader's wallet"
}
```

```json
{
  "kind": "cancel",
  "cancel": { "account": "0x…", "asset": 3, "oid": 123456, "nonce": 1789600000001, "expiresAt": 1789600060000 },
  "signature": "0x…"
}
```

To move the account's own stop or take (see "The stop and the take on Hyperliquid" below), the
kind is `stop` or `take` and the fields are the price it should trigger at:

```json
{
  "kind": "stop",
  "stop": { "account": "0x…", "asset": 3, "triggerPx": "58000", "nonce": 1789600000002, "expiresAt": 1789600060000 },
  "signature": "0x…"
}
```

The trader signs the fields themselves, under the domain `colosseum-pools gateway`, version 2,
chain 998, as `Order(address account,uint32 asset,bool isBuy,string limitPx,string size,bool reduceOnly,string tif,uint64 nonce,uint64 expiresAt)`,
`Cancel(address account,uint32 asset,uint64 oid,uint64 nonce,uint64 expiresAt)`,
`Stop(address account,uint32 asset,string triggerPx,uint64 nonce,uint64 expiresAt)` or
`Take(address account,uint32 asset,string triggerPx,uint64 nonce,uint64 expiresAt)`: two types
with the same fields, so a signature for one can't be used as the other. The
wallet shows the person the asset, the side, the price and the size. The gateway builds the
Hyperliquid action from exactly these fields, in Hyperliquid's field order, so what is
signed and what is submitted can't drift apart. Prices and sizes must be written the way
Hyperliquid normalizes them (`60000`, not `60000.0`): Hyperliquid checks signatures against
the normalized form, so any other spelling would be signed as one thing and verified as
another.

## Checks, in order

1. The request is well formed, carries exactly the fields of its kind, and `expiresAt` is in
   the future and less than a minute away.
2. The signature recovers to an address: the trader.
3. `PoolFactory.isAccount(account)`; the account is trading (`ChallengeAccount.status ==
   Active`, or `Pool.stage == Funded`).
4. `key = RuledAccount.agentKey(account)` and `KeyRegistry.isBound(key, account, trader)`.
5. The asset is in the account's rules. The signer checks the platform list and the caps again
   before it signs.
   - 🔴 **The leverage rule is NOT checked here, or anywhere in this gateway.** It reads `rules()`
     for the daily and drawdown floors and discards the leverage field (`rule_limits` in
     `gateway/chain.py`); the word does not appear in its code. So where `ForbiddenAsset` has two
     layers — refused here before signing, and `violation()` after — **leverage has one**: the
     contract and a keeper that pulls the stop. The protective stop resting on Hyperliquid does
     not cover it either, because that stop sits on the drawdown and daily floors, and a position
     can break the leverage rule while the account is barely down. After A-14 — where a shared
     request budget can leave a keeper's pass unfinished — this is the most exposed of the three
     rules. A pre-trade check is an intent for after the submission, not a thing that exists; it
     was never built, and that is not a decision recorded anywhere.
6. `(trader, nonce)` hasn't been seen. The pair is claimed here, before the enclave is asked,
   so a copy arriving meanwhile is refused without an enclave call. If the request then never
   reaches Hyperliquid (the Signer fails or refuses, or its signature is malformed or from the
   wrong key), the pair is given back and the same signed request can be retried. Once a
   signature for the right key exists, the pair stays spent, even if the call to Hyperliquid
   fails. The gateway remembers a pair until its request expires; after that the request
   fails check 1 anyway. The book holds at most a minute of traffic and answers `503 busy`
   past 100,000 entries instead of growing.

## Signing and submission

7. The gateway builds the action and has it signed with `key`.
   - `demo` mode, what the demo runs: before signing, the gateway's own code checks that the
     action is one limit order (`Alo`, `Gtc` or `Ioc`, no grouping) or one cancel, on an asset
     of the platform's list (BTC 3, ETH 4, SOL 0), with a size within that asset's cap
     (0.005 BTC, 0.15 ETH, 4 SOL) and, when it opens or grows a position, a notional of at
     most 400 USDC.
     Anything else is refused as 403 `refused_by_gateway`, code `over_cap` or `policy`, and the
     nonce is given back. Then it signs with the key file for `key` (phantom agent, source `b`).
     - The notional is size times the highest price the order can fill at. A buy never fills
       above its limit, so it counts at its limit. A sell never fills below its limit, and one
       priced under the market fills at the bids, which are under the mid. So a sell counts at
       its limit or at the asset's mid, whichever is higher. The mid is read from Hyperliquid's
       testnet (`allMids`) when the sell is checked. Without a mid, nothing is signed: 502,
       code `no_market_price`.
     - A price that moves up in the second between that read and the fill isn't covered.
     - The 400 USDC notional cap is on opening and growing a position (CTO, 18 Sep 2026). A
       reduce-only order passes it: Hyperliquid won't let it grow a position, and a position
       that grew with the price couldn't otherwise be closed in one order. The size cap and
       every other check above still bind it, and it reads no mid.
     - **`/v1/health` publishes that number**, as `max_order_notional_usdc`, so an agent can
       size against it instead of guessing. It is the SIGNER's number, read off the signer in
       use: in `signer` mode the field is **absent**, because the enclave enforces its own caps
       and this gateway does not know them — a number published from here would be a guess a
       client then sized against. An agent that guesses high sends an order the signer rejects,
       and that refusal is not `busy`, so it costs the agent one of its few daily orders for a
       number it could have read.
   - `signer` mode, not used in the demo: it sends the action to a Usenami Signer gateway
     (`POST /sign`, exchange `hyperliquid_testnet`, the same `kind`, the action, the trader's
     `nonce`) with the bearer token of the tenant that holds `key`.
8. It checks that the signature has Hyperliquid's shape (`r` and `s` as hex, `v` an integer 27
   or 28), recovers its signer the way Hyperliquid will (phantom agent, source `b`) and refuses
   to submit unless it is `key`.
9. It submits to `https://api.hyperliquid-testnet.xyz/exchange` and reads the answer: only
   an order that rests or fills, or a cancel that succeeds, counts as submitted. The nonce
   stays spent whatever Hyperliquid says. In `signer` mode a refusal comes back with the
   Signer's short reason (`error`, `reason`, `code`, `message`) and nothing is submitted, and
   `receipt` carries the Signer's signed decision receipt when the Signer issues one. In `demo`
   mode `receipt` is always empty.

## The stop and the take on Hyperliquid

The contracts enforce a pool's rules after the fact: someone sees a broken rule, calls `breach`,
and HyperCore closes the positions a few seconds after that block. The keeper looks every 30
seconds. In a cascade that minute is most of the loss: in the cascade package (`stress/`, Bybit
bars, not Hyperliquid) the worst entry of 10 October 2025 lost 83.7–94.1 % of the stress test's
pool on the long side when each stop filled a minute late (from the next minute's open to the
worst price inside that minute), and 3 of its 5 seats were liquidated. So the gateway also leaves
the closing to Hyperliquid itself: **before it submits any order that may open or grow a position
(every order that isn't reduce-only), the account has a stop at its rule line and a take at its
target on the exchange**, and the order isn't sent if they can't be put there. The keeper and the
contract stay behind it (docs/DESIGN.md, "Three layers").

**What they are.** Hyperliquid position TP/SL orders (`grouping: "positionTpsl"`), one stop and
one take per asset and direction: reduce-only, triggered by the mark price, executed as market
orders, and sized `0`, which Hyperliquid reads as the whole position however large it grows.
Measured on testnet on 28 September 2026 with one of our own wallets (`spike/tpsl_probe.py`, two
positions of about 11 USDC in ETH, a minute each): Hyperliquid accepts them before the position
exists and they wait for it; it answers `waitingForTrigger` and gives no oid (the gateway reads
the oids back from `frontendOpenOrders`); several can stand side by side, of either direction, but
one action may carry only one direction (an action mixing them is refused as a whole); when the
position closes Hyperliquid removes every reduce-only order on it, these included, and when one
order flips a long to a short, the short's orders placed beforehand stay; a `batchModify` moves
one in a single action and gives it a new oid. So an order that may flip a position gets its new
direction's stop before it goes, and the flip is guarded from its first fill.

**Where the stop is: the rule line.** The price at which the account's equity would reach the
nearest rule:

- the floor is the higher of the static drawdown floor (`drawdownBase × (1 − maxDrawdownBps)`)
  and the day's floor (`dayStartEquity × (1 − dailyLossBps)`, when there is a snapshot) — the
  same comparison `RuledAccount.violation` makes;
- the budget is equity minus the floor, equity being Hyperliquid's `accountValue`, the number
  the contract reads through the precompile;
- with several positions the budget is shared in proportion to their notional at the mark, so
  every stop sits the same fraction of its mark away: `mark × (notional − budget) / notional` for
  a long, `mark × (notional + budget) / notional` for a short. If all of them are hit together,
  the account loses exactly the budget;
- the take is worked out the same way from the room to the target.

**Where the take is: the target, plus what closing costs.** In a challenge the pass target is
`capital × (1 + targetBps)`, which is what `graduate` checks — and it checks it with no tolerance.
The take is a market trigger, so when it fires the position closes at market and pays a taker fee.
A take placed exactly at the target therefore leaves the account flat a fee BELOW it, and
`graduate` refuses with `TargetNotMet`. **Worked out, not observed** — on 1 October 2026, against
the real `lines()` and the demo's pass pool (capital 70, target 25 bps, 99.66 USDC of notional),
with the taker rate read from `userFees` for the trader's wallet: equity at the take would be
70.17493 against a target of 70.175, and 70.13 once closed. Retrying does not help, because the
next room is exactly that fee: every attempt lands on `target − fee`. No take at the target had
fired when this was worked out — nobody had passed a challenge since 29 September — so those
figures are arithmetic on the live rate, not a recording of a run. The first take that did fire
is below, under "Funding moves the line", and it was measured.

So the room reaches past the target by `CLOSE_COST_BPS`, and it carries **both** sides: the take is
placed with the order that opens the position, so the entry's fee is debited after the line is
fixed, and `reconcile` will not pull a resting take nearer — that is the trader's to move. Ten basis
points, covering only the close, left a margin of **1.01 bps** of the notional, which is less than
the 1.1 a stop had already slipped on 29 September. It is now `2 × (TAKER_FEE_BPS + SLIPPAGE_BPS) +
SPARE_BPS` = 18 bps, each part named where it comes from: the taker fee read from `userFees`, the
slippage measured on that stop, and a spare so the margin is a multiple of the slippage rather than
equal to it. On 100 USDC of notional that is 0.18, and it holds at four basis points of slippage on
both fills at once.

The take also rounds AWAY from the mark, so a price tick cannot eat the allowance; the stop still
rounds towards it.

**Funding moves the line, and the take has to follow it.** The first take that fired, on
3 October 2026 on the second deployment, left its challenge below the target. **Measured**, from
Hyperliquid's own records of the account: capital 70, target 40 bps (70.28), 0.00117 BTC bought at
84995, and a take on the book with the opening order at 85378. Over six hours the position paid
funding six times, 0.178665 USDC in all. The take fired, the fill was 85399, and the account held
**70.204304** — 0.075696 short. Without the funding it would have held 70.382969: the allowance
did what it was sized for, and funding is not in it.

The sweep had the right line every time, because it reads the equity as it is: about 85578 by the
sixth charge. It did not move the take. One nearer the market than the line was taken for the
trader's own choice and left alone. Funding lowers the equity, and that moves the stop's line
towards the market, where the sweep follows, and the take's line away from it, where it did not.

In a challenge the sweep now carries the gateway's own take out to the line once it stands short
of it by more than `TAKE_LAG_BPS` of the mark: 6.5 bps, the entry's own fee and slippage, which
leave a take that far short the moment the position opens. A take inside that lag still clears the
target by the close and the whole spare. **Worked out, not observed**: replayed on the six charges
of that day, the take moves three times, after the first, the fourth and the sixth, and at 85399
it would not have fired. A take the **trader** moved stays where they put it, short of the target
or not: taking the profit early is theirs to decide. A request Hyperliquid refused moves nothing
and claims nothing; one that got no answer may have landed, and whichever take then stands is the
trader's. The gateway keeps that in memory only, so after a restart every take is its own until
the trader moves it again.

The lag works both ways. The gateway's own take is left alone while it stands within 6.5 bps of
the mark from the line, on either side, and is put on the line outside that.

**One answer, one instant.** A line is a distance from the mark, worked out from the equity. The
line used to take its mark from the market's answer, while the account's equity — another request,
a moment apart — carried the position's gain at a mark of its own. Whatever the price did between
the two moved the stop and the take by that much. With no lag on the far side a take was pulled in
for every tick of it: the review counted 39 modifies in an hour in which nothing was paid and the
price stood still. The lag was meant to cover that difference, and the difference turned out
larger than the lag. **Measured** on the live challenge, from Hyperliquid's own order history: on
4 October 2026 at 20:47:12 UTC a take went from 2879.5 to 2877.0 and seventeen seconds later back,
with no order of the trader's and no payment between — the two reads 2.5 USD apart on ETH, 9 bps.
On 5 October at 00:37:11 a stop came 1.1 nearer for the same reason, and stayed, because a stop
never moves away.

The mark of every asset the account holds now comes from the account's own answer: Hyperliquid
gives each position's value beside the equity it counted from it, and the value over the size is
the mark. The two are then of one instant, and what the price does between two requests moves
neither line. An asset the account does not hold keeps the market's mark, since nothing in the
equity depends on it. The market is read first and the account after it, so the account's answer
is also the later of the two: whether a stop or a take the trader asks for "would close at once"
is asked of it too.

**After a restart.** The list of accounts the sweep goes round lives in the gateway's memory and
is filled by orders. A gateway that had just started watched nothing, so an account with a position
open was not swept again until its trader's next order: a new day's snapshot did not tighten its
stop, and its take followed nothing. Before the first sweep the gateway now asks the registry which
account each of its keys is bound to, and takes on those the key trades right now. This works
where the signer lists its keys, which the demo signer does and the enclave signer does not. A key
the node would not read at that moment is tried again by every sweep until it answers, after the
accounts the sweep already has and never before them. What a restart still loses is which takes
were the trader's.

What this does not cover. First, a charge that lands and a take that fires before the next sweep,
15 seconds by default: that take is short by the one charge, which the spare absorbs up to 5 bps
of the notional. Second, an account with exposure on more than one asset. There each take's line
moves with the other position's price, a take that followed it would be modified out and back in
with every swing between the two, and so those takes are left as they were before: they can still
stand short of the target. Third, a position with an order resting on the book that would grow
it. The line prices the larger size while the equity moves with the smaller, so the line moves
with the mark, and the take is not followed until that order has filled or gone. Fourth, an asset
the account has an order on and no position in is still priced at the market's mark; while such an
order rests beside a position, that position's lines move a little with the difference between
the two requests, by the order's share of the exposure. The same goes for a position the account's
answer gives no value for. Hyperliquid's gives one for every position; if it stopped, that
position's lines would be worked out from two requests again. The sweep then says so in its
journal (`protect_no_position_value`), once for the account and the asset — again after a value
has come and gone, or after the account was let go — and does not move its take: a take pulled in
by the difference between two requests closes the position short of its target. It still brings
the stop nearer when the line says so. The stop is what holds the pool's rule, and a new day's
floor or a payment out of the account has to reach it. Over time the difference between two
requests costs the trader room. A single placement can stand that difference too far, until a
later sweep: with the market's answer 2.5 behind, a stop placed for 0.037 ETH stood 2.5 beyond the
rule's line, 0.09 USDC, and the take placed with it 2.5 short of its own, where it stays. A stop
moved to a new line can stand too far the same way: brought in for the next day's floor with the
market's answer 2.5 behind, it went to 2680.1 where the line was 2682.6, and the next sweep put it
on 2682.6. Fifth, a
stop that had already come
nearer for that difference before this change stays where it is until its position closes: the
gateway cannot tell it from a stop the trader tightened, and it does not move a stop away.
**Measured** on the live challenge on 5 October 2026 at 05:55 UTC, with this change run beside
the code then on the host against the same two answers: the stop stood at 2662.9 and the line at
2658.5, 4.4 nearer, 0.16 USDC of the 1.45 the account had left to its day's floor. Of the 4.4, the
order history shows 1.4 from the two requests, in three steps. The other 3.0 is the funding the
position had been paid since it opened, 0.109428 USDC: paid funding moves the line away, and the
stop does not follow it.

A funded stage has no target and the pool's rules have no take, so there the take is at most one
challenge target away: `targetBps` of the equity at the moment it is set, with no allowance, since
nothing has to be cleared. Once it stands with its position the gateway does not move it. Its line
is worked out from the mark and the equity of the moment, so the line recedes as the position
gains and comes after the price as the position loses, and a take held to that line did the
second. **Measured** on 4 October 2026: a short entered at 84955 with its take at 84715, the price
went to 85206, and the take was found at 84967 — above the entry, where it would have closed the
position at a loss and called it a take. The trader may still bring it nearer the mark. What
holding it costs: after the position is added to, the take stands further than one target for
the new size.

**The assumptions, named.**

1. Every position moves against the account at once, each by the same fraction of its mark. A
   hedged book (a long in one coin, a short in another) loses less at its stops than the budget.
2. Orders resting on the book that could open or grow a position, and the order on its way,
   count as filled at the mark. A resting buy fills under the mark, so its real line is lower:
   counting it at the mark never places a stop looser than its fill would need. On each asset
   the larger of the two directions is what shares the budget.
3. The day's snapshot is the one the contract holds. Until someone takes today's (the keeper
   does at midnight), the contract measures the day from yesterday's, and so does the gateway:
   it reads the snapshot again at most once a minute until today's appears, and a sweep then
   moves the stop.
4. Fees, funding and slippage are not in the line. The stop is at the rule line itself, so when
   it fires the account is at its limit, the taker fee and the fill take it past, and the
   keeper's `breach` then ends the stage as it always did. What changes is how far past.
5. A stop is never further than half the mark away and never nearer than one tick: a position too
   small to use up the budget has no rule line, and Hyperliquid still wants a price. Prices are
   rounded to Hyperliquid's rules (five significant figures, at most `6 − szDecimals` decimals,
   whole numbers always): a stop towards the mark, so rounding never loosens it, and a take away
   from the mark, so rounding never eats the room that pays for closing. A take may therefore sit
   one tick beyond its line, never nearer.
6. A market TP/SL fills within 10 % of its trigger (Hyperliquid's own tolerance); the order's
   price field is set to that bound, so it never makes the close stricter than the venue's rule.

**The slippage, said plainly.** A stop-market in a cascade fills below its trigger, sometimes far
below: Hyperliquid's 10 % tolerance is the bound, not the expectation. What walking Hyperliquid's
book costs has been measured only in a calm market (25–27 September 2026); the book in a cascade
has not been measured: nobody recorded it on 10 October 2025, and Hyperliquid's public API serves
the book as it is now. So how far past the line an exchange stop lands on a day like that is not
known, and `--lag 0` in `stress/`, which fills exactly at the line, is not
that number and is never quoted as one. The claim is the delay taken out, not a loss figure.

**What the trader may do with them.** Move the stop nearer the mark, never away from where it is,
with a `stop` request; move the take anywhere between the mark and the line the gateway
would place it on — the target plus the closing allowance — with a `take` request. Both need a position on that asset. The gateway builds the `batchModify` from the book
and signs it with the account's key. A `cancel` naming the oid of either is refused with 403
`protective_order`, before anything is signed; any other cancel goes through as before.

**Checks before an order that may open a position**, after the signer has cleared it and before
it is submitted:

- 409 `at_rule_line`: equity is at the floor already, so there is nothing left to protect with;
- 409 `target_met`: a challenge at its target; its take would close the order at once, and the
  trader should graduate instead;
- 502 `protection_failed`: the stop and the take could not be put on the book (the signer
  refused them, Hyperliquid refused or didn't confirm them, or it couldn't be reached). The
  order is not submitted and its nonce comes back, so the same signed request can be retried.

A 200 for such an order carries `protection`: for each asset and direction, the stop and the
take, and whether each was `placed`, `moved` or `kept`.

**One request at a time per account.** From the stop and the take placed for an order until
Hyperliquid has answered for the order itself, the book doesn't show that order yet, so nothing
else may act on the account in between: the account's other requests and the sweep wait. Otherwise
a second order would be protected as if the first weren't coming, and a sweep would move a stop that
guards nothing yet out to the line of a book without the order. The nonce check comes before the
wait, so a replayed copy is refused at once.

**The sweep.** Every `GATEWAY_PROTECT_EVERY` seconds (default 15) the gateway goes over the accounts
it watches — those it took on when it started and those it has traded since — and puts back what is
missing: a position that opened from an order that rested, a position opened again after its stop
fired while another order still rested (Hyperliquid had removed the stop with the position), or a
line a new day's snapshot has moved. A stop that guards a position is only ever moved nearer the
mark; one that guards nothing yet follows the line. The sweep reads Hyperliquid; it reads the chain
only when there is something to send, to check the key still trades the account, and for the day's
snapshot. The list of accounts lives in memory: a gateway that has just started fills it from the
keys it holds, as "After a restart" above says, and the stops already on the exchange stay where
they are. A sweep costs Hyperliquid's info API 20 of weight for the marks (`metaAndAssetCtxs`) and
22 for each account it watches (`clearinghouseState` 2, `frontendOpenOrders` 20). An account with
nothing open drops out of the list at the second sweep in a row that finds it so, and with the list
empty a sweep reads nothing. One such answer is not enough: the positions and the orders are two
requests, and an order that fills between them is in neither — not yet a position in the first, no
longer an order in the second. Let go on that, the account would hold a position nothing sweeps
until its trader's next order. Two such answers in a row can still both be wrong: an order filling
between the requests of one sweep, then that position closing and another order filling between the
requests of the next. A stop or a take of the gateway's own that stands for a direction with
nothing behind it, no position and no order that could open one, is taken off at the second sweep
in a row that finds it so, one cancel each, and the journal says so (`protect_cleared`); an account
is let go only once none is left. A pair goes on the book before an order that may open a
position, and an order that rested and went without filling left its pair standing, which the
trader could not cancel and the sweep never looked at, since it walks the directions with exposure
alone: the team's red team left one such pair on 5 October 2026 (A-22). A cancel Hyperliquid does
not confirm keeps the account on the list, and the next sweep tries again.

**The host's minute.** Hyperliquid allows an IP 1,200 of weight a minute, and the gateway and the
keeper on one host draw on the same 1,200. Three things spend it:

- the keeper: 20 a pass for the names of the markets and 22 for each active account, the same two
  reads as the sweep, once each for everything it decides about the account; a pass every 30 seconds;
- the sweep: 20 for the marks and 22 for each account it watches, a sweep every 15 seconds;
- an order that may open a position: at most 46, that is 20 and 22 for the stop and take's plan, 1
  each for the order and the protective action, and for a sell 2 more for the demo signer's mids
  (`allMids`); the table counts every order at 46.

nginx lets 15 orders a minute through to the whole server, after a burst of 10. With n accounts that
each hold a position, a minute costs:

| | at 15 seconds | at the former 10 seconds |
|---|---|---|
| keeper | 40 + 44n | 40 + 44n |
| sweep | 80 + 88n | 120 + 132n |
| orders at nginx's rate | 15 × 46 = 690 | 690 |
| two accounts | 1,074 | 1,202 |
| most accounts under 1,200, with orders | 2 (three are 1,206) | 1 |
| most accounts under 1,200, no orders | 8 (nine are 1,308) | 5 (six are 1,216) |

Past that, Hyperliquid refuses reads. A refused read before an order refuses the order before
anything is submitted, and its nonce can be used again; a refused sweep is logged and tried again at
the next one. The burst of 10 can take one minute past the table. The sweep's interval is the lever:
a position that opened again after its stop fired waits up to that long for a new one.

**Signing them.** The gateway signs its own stop and take with the account's key under nonces of
its own (never the trader's nonce of the request in hand). In `demo` mode the gateway's code
signs them as kind `protect` only if the action is one or two position TP/SL orders, or one
`batchModify` of one, each reduce-only, sized `0`, a market trigger, on an asset of the
platform's list; nothing else goes out under that kind, and no cap applies because such an order
can only close. `signer` mode would need the Signer to accept the same; the demo doesn't use it,
and that is not done.

**Not covered: HIP-3 markets.** The gateway reads the marks, positions and open orders of
Hyperliquid's main perp dex only. A builder-deployed market (HIP-3: another dex, names like
`xyz:GOLD`, asset `100000 + perp_dex_index × 10000 + index_in_meta`) has no mark there, so an
order on one would be refused with 502 `no_market_price` before anything is sent, and a position
on one would not be seen by the sweep or the keeper. The demo's platform list is BTC, ETH and SOL,
all on the main dex; a pool that trades HIP-3 markets needs these reads for its dex first.

## Answers

| HTTP | `status` | meaning |
|---|---|---|
| 200 | `submitted` | Hyperliquid confirmed the action: the order rests or filled, or the cancel succeeded |
| 4xx | `refused_by_gateway` | a check failed; `code` says which (`over_cap` and `policy` are the demo signer's checks before signing) |
| 403 | `refused_by_signer` | `signer` mode only: the Signer refused; `receipt` holds its signed receipt if it issues one |
| 422 | `refused_by_venue` | Hyperliquid refused the action or the order; its words are in `reason` |
| 502 | `venue_unconfirmed` | Hyperliquid's answer confirms nothing (not JSON, no statuses, or a status that is neither a resting or filled order, a successful cancel, nor an error); check the account |
| 502 | `signature_mismatch`, `bad_signer_response` | the Signer's answer can't be submitted; nothing was |
| 502 | `gateway_error` (`upstream_failed`) | a chain read, the market read or the Signer call failed; nothing was submitted. A chain read the RPC throttled is tried three times, 0.25 s and 0.5 s apart, before this answer |
| 502 | `refused_by_gateway` (`no_market_price`) | `demo` mode: Hyperliquid gave no mid for the asset of a sell, so it wasn't counted or signed |
| 502 | `venue_unreachable` | the call to Hyperliquid failed; the order may or may not have arrived, so check the account |
| 409 | `refused_by_gateway` (`at_rule_line`, `target_met`) | an order that may open a position, refused before it is sent: equity is at the rule line, or a challenge is at its target |
| 502 | `refused_by_gateway` (`protection_failed`) | the stop and the take could not be put on Hyperliquid, so the order was not sent; its nonce comes back |
| 403 | `refused_by_gateway` (`protective_order`) | a cancel of the account's stop or take |
| 403 | `refused_by_gateway` (`stop_looser`, `stop_past_mark`, `take_beyond_target`, `take_past_mark`) | a `stop` or `take` request outside what the rules allow |
| 409 | `refused_by_gateway` (`no_position`) | a `stop` or `take` request on an asset with no position |
| 503 | (empty body) | more than 32 connections at once |

A connection that sends nothing for 10 seconds is closed.

## What the gateway does not prove

The trader's signature is checked by the gateway, which we run, so the operator could submit
an order without the trader. The chain shows which key traded which account; it does not show
that the trader asked for each order.

In the demo the gateway holds the keys. A key file can sign anything Hyperliquid lets an agent
sign, and the caps above only bind what goes through this code, so whoever controls the gateway
host can trade every account whose key is on it until someone stops that account. The same goes
for the stop and the take: the gateway refuses to cancel them, but the key file could.

## Secrets and where it runs

In `demo` mode the gateway reads one owner-only `*.key` file per agent key from
`GATEWAY_KEYS_DIR` (an owner-only directory) and refuses to start if either is open to others;
the address comes from the key. In `signer` mode it reads one bearer token per key from
`SIGNER_TOKENS_FILE`. Either lives on the gateway host only, never in this repository.
`GET /v1/health` names the mode and, in `demo` mode, how many keys it holds.

**The gateway reads its own node, and that costs you requests.** Since 25 September 2026 it
reads `rpc.hyperliquid-testnet.xyz` while the keeper reads `rpcs.chain.link`, because the two
were sharing one per-IP budget on one host: an outsider's order costs the gateway five chain
reads *before* it is refused, so a stream of refusable orders could spend the budget the keeper
needed to notice a broken rule (audit A-03). Separating them fixed that and cost something
visible from outside. The gateway's node allows about 100 calls a minute against the old one's
200, so nginx is cut to match: **15 requests a minute across everyone, burst 10**, where it used
to be 30 and 20. Past that you get `429` from nginx, not a refusal from the gateway. It is a
smaller ceiling than before and it is deliberate: a keeper that cannot see a breach costs the
investor money, a trader who waits a few seconds does not.
