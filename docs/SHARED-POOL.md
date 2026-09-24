# Design: the shared pool

Status: 25 September 2026: shares, deposits, the pool's value, settlement points, withdrawals and
their queue, the funded term and the platform's fee. Testnet only, and not part of the reviewed
core: `Pool`, `PoolFactory`, `ChallengeAccount` and `KeyRegistry` are used as they are, unchanged.

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
check for a transfer a contract sends through CoreWriter is still to run; if it fails, only
`blocker()` changes.

## Withdrawals

`requestRedeem(shares)` locks shares for payment, once `lock` has passed since the holder's latest
deposit (a day; published). The shares stay the holder's and keep bearing the pool's result until
they are paid, so asking early fixes nothing: there is no price to race for. A request can't be
taken back, and the platform's starting shares never leave.

Each settlement point pays the queue at its own price, the same for every request and for the
deposits it takes in. If the pool's free money covers the queue, everyone is paid in full;
otherwise everyone gets the same fraction and the rest waits. Free money is the pool's USDC on
HyperEVM (the seats' earned prices are collected first) and its spot on HyperCore; HyperEVM pays
first, in the same proportion for every holder, and the app shows the two parts apart. A holder
with no HyperCore account yet pays the 1 USDC for creating it out of their HyperCore part. A
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

## Gas

Reading one seat the way a settlement point does, measured with `eth_call` against the live testnet
pools (`spike/shared_pool.py gas`): 28 265 gas for an idle seat, 74 336–74 759 for a seat with a
running challenge. One `spotBalance` read costs 7 431, `accountMarginSummary` 8 590, a `violation()`
call about 22 400–23 400. The contract caps the seats at 12 and the tickets per point at 8, which
keeps a point well inside a small block. Anyone can open tickets for the price of gas; a settlement
point never walks the open ones, only the list a caller names, so that costs the keeper reads and
nothing else.

## Not done yet

- The app view and the keeper calls for the shared pool; a live run on testnet.
- Deposits through HyperEVM on mainnet, where the sender is visible.
- At most 16 holders wait at once; a request past that waits for the queue to move.

The limits of the core apply here too: the operator's gateway holds the demo's agent keys, a stop
comes after the breach, and a payout the contracts sent is taken as landed after `PAYOUT_WAIT`.
