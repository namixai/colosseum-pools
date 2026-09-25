# Design: the shared pool

Status: 25 September 2026: shares, deposits, the pool's value, settlement points, withdrawals and
their queue, the funded term and the platform's fee; run on testnet with one depositor, then two.
Testnet only, and not part of the reviewed core: `Pool`, `PoolFactory`, `ChallengeAccount` and
`KeyRegistry` are used as they are, unchanged.

## Why a shared pool

A pool of its own holds one investor's capital. Smaller amounts belong in a shared pool: many
investors hold shares in one book of seats, and the platform publishes the seats, their rules and
terms before anyone deposits. So the investor chooses a pool whose limits are published; they don't
set the limits themselves.

## Contracts

- `SharedPool` (`src/shared/SharedPool.sol`). Creates its seats through `PoolFactory.createPool`,
  which makes it their owner. A seat is an ordinary `Pool`, with its own challenge and funded stage.
  Everything the shared pool needs from a seat, an owner already has: `withdrawOnCore` for an idle
  seat's capital, `withdrawEarned` for the challenge prices, `stopFunded`. It keeps the share ledger
  (no token: shares can't be transferred or sold around a settlement point) and has its own
  HyperCore account.
- `DepositTicket` (`src/shared/DepositTicket.sol`). One per deposit, an EIP-1167 clone at an address
  computed from the depositor and a counter. It can only pay into its own pool.

## A share and the pool's value

A share is a claim on a part of the pool's value. The value is what the pool would have if every
account were closed at the mark now and everyone it owes were paid (1e8 = 1 USDC):

- its spot USDC on HyperCore and its USDC on HyperEVM;
- for each seat: its spot and perp account value, the challenge prices it has earned
  (`Pool.earned`), and the spot and perp value of its running challenge;
- closed tickets that still hold money (their sweep into the pool may be in flight);
- less what traders are owed: a passed challenge's share not yet paid, and on a funded seat, or on
  a challenge at or past its target, the trader's share of the profit as if the stage ended now
  without a breach.

A price paid for a challenge that hasn't started (`heldPrice`) is not counted: it can still be
refunded. The platform starts the pool with a deposit of its own whose shares never leave
(`start()`); the starting shares must stay worth at least 5% of the seat plan, which `addSeat`
enforces. They also keep the price of a nearly empty pool from being pushed up by a donation.

## Deposits

A contract can't see who sent a HyperCore transfer, and on testnet the USDC bridge credited nothing
to a contract bridging to itself (spike, question 5). So each deposit gets its own address:

1. The depositor calls `openTicket()` and sends USDC to the returned address on HyperCore. The
   address is known in advance (`ticketAddress`). HyperCore charges the sender 1 USDC for creating
   the account of a new address.
2. A settlement point that names the ticket reads what it holds. From `minDeposit` up, the deposit
   becomes shares at the pool's value without it: `shares = deposit x totalShares / value`, rounded
   down. Every deposit in one point gets the same price, and the price of a share doesn't move.
3. The ticket is then closed. Everything on it belongs to the pool and is swept into it; money sent
   to a closed ticket later is a gift to all holders, taken in when someone names the ticket. Less
   than 1 USDC left on a closed ticket is dust: it is not swept or counted, and the ticket drops off
   the list every settlement point reads. Keeping a ticket on that list would otherwise cost next
   to nothing; now it costs 1 USDC a point, which the pool keeps. For the same reason `minDeposit`
   can't be set below 1 USDC: a smaller deposit would be counted, closed and forgotten as dust in
   one point, and its shares would stand for money the pool never took in.

Shares are minted only for money a precompile has already shown on the ticket. A deposit address
used more than once would not work: the pool couldn't tell its own sweep, still in flight, from a
new deposit by the same person, and would count the money twice or not at all.

## Settlement points

`settle(tickets)` is open to anyone, like `breach()`. `blocker()` is the only rule it obeys; while
it names something, no price is struck:

- a funded seat or an active challenge breaks a rule right now (someone has to call `breach` first);
- a seat is closing its funded stage;
- a stopped challenge still holds positions (the loss is still moving);
- a payout to a trader has been sent and hasn't landed;
- the pool's own payments on HyperCore were sent less than `PAYOUT_WAIT` (5 minutes) ago.

Not in it: a transfer between two of the pool's own accounts (the pool, a seat, a challenge, a
closed ticket). HyperCore debits and credits a transfer in one step, and a read sees both sides from
the same state, so the value doesn't change while one is in flight. Measured on 24 September 2026:
four transfers sent through the API, read 264 times, never seen half done, each visible on both
sides in the same read about a second after it was sent (`spike/shared_pool.py atomic`). The same
night, four transfers a contract sent through CoreWriter, the way the pool and its seats send: read
281 times, never seen half done, each visible on both sides in the same read, one block after the
block of the transaction that sent it (`atomic --via corewriter`).

## Withdrawals

`requestRedeem(shares)` locks shares for payment, once `lock` has passed since the holder's latest
deposit (a day; published). The shares stay the holder's and keep bearing the pool's result until
they are paid, so asking early fixes nothing: there is no price to race for. A request can't be
taken back, and the platform's starting shares never leave.

Each settlement point pays the queue at its own price, the same for every request and for the
deposits it takes in. If the pool's free money covers the queue, everyone is paid in full;
otherwise everyone gets the same fraction and the rest waits. Free money is the pool's USDC on
HyperEVM (the seats' earned prices are collected first) and its spot on HyperCore; HyperEVM pays
first, in the same proportion for every holder, and the app shows the two parts apart. For that the
contract keeps what it has paid each holder so far, each part where it was paid, and when it last
paid them (`payments`): the public RPC serves the event log 50 blocks at a time, which can't find a
payment made yesterday. A holder with no HyperCore account yet pays the 1 USDC for creating it out
of their HyperCore part; `payments` counts what was sent to them after it. A
request worth less than the smallest amount a payment carries (a millionth of a dollar) is cleared
at the next point, its shares burned for nothing, so dust never keeps the queue waiting. And a point
doesn't pay the queue within `ARM_WAIT` of a top-up: the top-up may not show yet in the balance the
payment would come from.

The platform's fee is its share of each holder's own profit, taken when they are paid: the payment
over what the paid shares cost that holder, never on a loss. It stays in the pool as the platform's
shares rather than being paid out.

While someone waits, the queue comes first. A seat is armed only from money the queue doesn't need:
what is left on HyperCore after the top-up, with the USDC on HyperEVM, must still cover the queue at
the current value. And while the queue needs more than the pool has free, `releaseSeat` (anyone)
brings an idle seat's capital back into the pool. A small request doesn't stop the seats; a large
one gets the capital as it comes free. Capital in a running challenge or a funded stage can't be
taken back early; a funded stage ends by its term (below). Whoever stays pays for a long wait in
revenue the pool doesn't earn meanwhile; there is no exit fee.

## Seats

`addSeat` (the operator) publishes a seat. `armSeat` (anyone) tops an idle seat up to its
`capitalNeeded()` from the pool's spot, paying 1 USDC once for a seat that has no HyperCore account
yet; after that anyone calls `prepareAccount` on it. A top-up is given a minute to land before the
same seat can be topped up again, and waits for the pool's own payments to land.

Each seat is published with a funded term. `noteFunded` (the keeper, or any settlement point)
records when the seat's current funded stage was first seen, together with that stage's agent key:
every funded stage gets a new key, so a new stage is never taken for an old one. Once the term has
run, anyone may call `endFundedTerm`, which stops the stage through `Pool.stopFunded`, without a
breach: the trader is paid their share of the profit when the stage settles. The term counts from
the first time the stage is seen, so it runs late by however long nobody looked.

## In the app

`#/shared` is the pool's page. It shows the value, the price of a share, what waits to be paid,
whether a settlement point can run now and what holds it up, and the seats with their rules and
terms. A connected wallet also sees its own shares, what they cost it, and what the pool has paid it
so far, each part where it was paid. From the page it can deposit (the wallet opens a ticket, then
signs the transfer to it on HyperCore), ask to withdraw, and run a settlement point. The page's ABI
is in `app/lib/shared.js`; its test reads this contract's source back, so a renamed function or two
swapped fields fail there first.

A seat the demo's factory made, like the second round's, opens on the pool page like any other
pool, and its challenge is bought and traded there. The first run's seat comes from a factory of its
own, which the pool, challenge and trading pages don't know; the shared pool's page says so.

## The keeper

`ops/shared_keeper.py` makes the calls anyone may make on the pool, when the pool would take them:
`noteFunded` for a funded stage new to the pool and `endFundedTerm` once the stage has run its
term; `releaseSeat` while the queue needs more than the pool has free; `settle` when a ticket holds
a deposit, when the queue waits (at most once every five minutes by default) or when a closed ticket
still holds money; `armSeat` and then `prepareAccount` when there is money to spare. It tries every
call with `eth_estimateGas` from its own address first and sends only what the contract would take,
so the rules stay in the contract. It reads the contract's state and the precompiles and never the
event log. The seats' challenges and funded stages are ordinary pools; the core keeper
(`ops/keeper.py`), run with the same deployment, takes them through their lives.

## Gas

Reading one seat the way a settlement point does, measured with `eth_call` against the live testnet
pools (`spike/shared_pool.py gas`): 28 265 gas for an idle seat, 74 336–74 759 for a seat with a
running challenge. One `spotBalance` read costs 7 431, `accountMarginSummary` 8 590, a `violation()`
call about 22 400–23 400.

Whole points on the testnet runs: 340 720 gas taking in one deposit with the seat idle, 513 366
taking in two; 169 440 paying one holder on HyperCore with the seat idle, 225 879 paying two;
297 935 paying one holder on both sides with a challenge running. So a deposit taken in adds about
170 000 and a holder paid about 56 000. The contract caps a point at 12 seats, 8 tickets and 16
holders waiting, and at those caps, with every seat running a challenge, a point comes to some 3.3M
gas by these numbers, past a small block's 2M. Nobody has run one that big. The keeper tries every
point first and names half the tickets, then half again, when it wouldn't fit; a point that still
doesn't fit needs the keeper's wallet on big blocks.

Anyone can open tickets for the price of gas. A settlement point never walks the open ones, only
the list a caller names, so they cost the keeper reads and nothing else. It reads the whole list of
addresses every pass but rations balance reads: tickets it hasn't seen yet first, at most 64 a pass,
then empty ones again, the longest-unread first, at most 32 a pass and none sooner than ten passes
after its last read. A pile of empty tickets slows a deposit behind it down and can't hide it.

## The testnet run (24 September)

One depositor, 22:30 to 23:16 UTC, on contracts of the run's own: `ops/deploy_shared.py` deployed a
`KeyRegistry`, a `PoolFactory` and a `SharedPool`, reusing the demo's `Pool` and `ChallengeAccount`
implementations (the script checks that their sources haven't changed since the demo was deployed),
and `ops/shared_run.py` ran the pool one step at a time. The addresses are in
`deployments/testnet-shared-run.json`. Every step, with the full transaction hash and what the chain
showed before and after it, is in `spike/results/2026-09-24.jsonl`. The run's parameters: a 20 USDC
minimum deposit, a 10-minute lock, a 10% fee; one seat with a 2 USDC challenge (price 0.5 USDC, a 1%
target, half an hour), 8 USDC of funded capital and a quarter-hour funded term. The pool deployed for
the run is older than `payments`.

| step | HyperEVM transaction | what the chain showed after it |
|---|---|---|
| The platform's 1 USDC, then `start()` | `0xf746465e` | 1e8 shares, value 1 USDC |
| `addSeat` | `0xcfc2f0d5` | |
| `openTicket()`, then 20 USDC to the ticket on HyperCore | `0x047f6c9d` | the ticket holds 20 USDC |
| Point 1 | `0xf3636c2f` | 20e8 shares for 20 USDC; the ticket swept into the pool |
| `armSeat`, `prepareAccount` | `0xa795ab84`, `0x930222ff` | the seat holds 11 USDC; creating its HyperCore account cost the pool 1 USDC more; value 20 |
| A trader buys the challenge, then `activate()` | `0xa46dbe1c`, `0x688be7ae` | 2 USDC in the challenge, 1 USDC paid for creating its account, the 0.5 USDC price earned; value 19.5 |
| `requestRedeem` for all 20e8 shares | `0x39ae2298` | 20e8 shares queued |
| Point 2 | `0xe2ea3bd2` | price 0.928571; 9.5 USDC free against 18.57 owed, so 0.5 USDC paid on HyperEVM and 8.999999 on HyperCore; 976 923 154 shares still queued |
| `expire`, then three `settle` steps of the challenge | `0x6863b125` and three more | the challenge's 2 USDC back on the seat; the seat idle with 10 USDC; value 10.000001, unchanged |
| `releaseSeat` | `0xc08ea386` | the seat's 10 USDC back in the pool; value unchanged |
| Point 3 | `0x10e5a304` | 9.071429 USDC paid on HyperCore; the queue empty |

The depositor put in 20 USDC and got 18.571428 back, 0.5 on HyperEVM and 18.071428 on HyperCore,
at the same price at both points. The difference is what the run cost the pool: 1 USDC for creating
the seat's HyperCore account and 1 USDC for creating the challenge's, less the 0.5 USDC the trader
paid for the challenge (the 0.7 USDC fee went to the platform). That is 1.5 USDC out of 21, and
20/21 of it fell on the depositor. A challenge priced above the 1 USDC its account costs brings
money in rather than out. The fee took nothing, since the depositor made no profit. The platform's
starting shares are worth 0.928572 USDC and stay in the pool.

Each point went through at the first call. The value stayed the same while the challenge's capital
and then the seat's came back into the pool.

Not covered by this run, only by the tests: two depositors sharing a short payment, a deposit at a
price other than 1, a passed challenge and a funded stage, the funded term.

## The second round (25 September)

Two depositors, 03:18 to 04:33 UTC, on a pool whose seats the demo's factory makes:
`ops/deploy_shared.py --on-demo-factory` deployed the SharedPool alone
(`deployments/testnet-shared-demo.json`), and its seat is a demo pool like any other. The seat keeps
the demo's proportion, a challenge of 1 USDC to 10 funded, the pool the Economics page prices. It is
too small to trade, and the round didn't need a trader: an armed seat made the payment short, and
`releaseSeat` brought its capital back. Every step is in `spike/results/2026-09-25.jsonl`.

| step | HyperEVM transaction | what the chain showed after it |
|---|---|---|
| Deploy the SharedPool | `0xaa169f94` | |
| The platform's 1 USDC, then `start()` | `0xe816bc8d` | 1e8 shares |
| `addSeat`: 1 and 10, price 1.5 | `0xb13916f8` | |
| Two deposits of 20, each through a ticket of its own | `0xb9e79541`, `0x3f43cfc8` | |
| Point 1 | `0xb8b756ca` | 20e8 shares each at 1.00; value 41 |
| `armSeat`, `prepareAccount` | `0x24bcf044`, `0x00ef511b` | 12 USDC on the seat; creating its account cost the pool 1; value 40 |
| Two requests for everything | `0x09d86ed7`, `0xc8a7f39f` | 40e8 shares queued, worth 39.02 |
| Point 2 | `0x39994dbf` | 28 USDC free: each holder paid 13.999999 on HyperCore, the same 71.75% of their request; 565 000 094 shares each still queued |
| `releaseSeat` | `0x4fd3b101` | the seat's 12 back in the pool; value unchanged |
| Point 3 | `0x09190d77` | 5.512196 more to each; the queue empty |

Each depositor put in 20 and got 19.512195 back, all of it on HyperCore: 20 × 40/41. The 1 USDC the
seat's new account cost fell on every share alike, the platform's starting share included, which is
now worth 0.97561. Point 2 paid two holders through CoreWriter in one transaction, and both
transfers landed. `payments` on this pool shows each depositor 19.512195 on HyperCore and nothing on
HyperEVM, the last at 04:33 UTC.

The public RPC dropped two replies on the way: `start()` and point 1 went through while the answer
to the sender timed out, so their hashes were read afterwards from the pool's own events. The rest
of the round went through Chainlink's testnet RPC (`COLOSSEUM_RPC_URL`).

## A seat a trader traded (25 September)

A third pool, `0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6`, on the demo's factory, with one seat of 3
USDC of challenge capital and 30 funded: a 10% drawdown, a 5% daily loss, an 8% target and a day,
rules the Economics page prices. Two deposits of 20 armed it. A trader bought its challenge and
traded $11 of BTC through the pool gateway, in and out; the account is flat at 2.98997 USDC. The
seat opens on the app's pool page like any demo pool, and the shared pool by
`#/shared/0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6`; the page's default is still the second
round's pool. Every transaction, and how the trader acted (the gateway client, not a click), is in
[EVIDENCE-SHARED-POOL.md](EVIDENCE-SHARED-POOL.md).

## Not done yet

- The keeper running on the operator's host.
- A deposit at a price other than 1 on testnet: both runs took their deposits into a fresh pool.
- Deposits through HyperEVM on mainnet, where the sender is visible.
- At most 16 holders wait at once; a request past that waits for the queue to move.
- A holder with no HyperCore account loses a HyperCore part of 1 USDC or less: it can't pay for
  creating the account, so nothing is sent, and the shares it stood for are burned. A deposit made
  from the depositor's own HyperCore account, the way the app makes it, means the account exists; a
  holder whose deposit came from another account can meet this.

The limits of the core apply here too: the operator's gateway holds the demo's agent keys, a stop
comes after the breach, and a payout the contracts sent is taken as landed after `PAYOUT_WAIT`.
