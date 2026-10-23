# A pool of several investors

`src/multi/PoolShares.sol` holds many investors' money in one pool of many traders
(`docs/MULTI-POOL.md`). It is the `MultiPool`'s owner, and a share is a claim on a part of the
pool's value. On the creation form this is the choice called "Shared pool".

The rules are Alex's: the money is locked for the term, as tokens are in a token lock, and it can
only be a stablecoin; the creator extends; each investor decides, ahead of the end, whether to
stay or to take their part.

**Draft. Nothing here is deployed, no page uses it, and the audit has not read it.**

## The creator

Whoever creates the pool deploys this contract, then the `MultiPool` with this contract as its
owner, and binds the two with `bind(pool)`: once, by the creator, on a pool owned here that has
no exam or account running, with both accounts already on HyperCore (so that no transfer between
them pays 1 USDC for creating one).

Whatever the pool holds while there are no shares -- what the creator put into it before the
bind, or what is left once every share has gone -- becomes the creator's shares, one per unit, at
the next settlement point and before any deposit there is priced. So a first deposit buys in at
the value as it stands, and does not take what was there before it. A rule in `bind` alone, that
the pool be empty, would not do: binding needs both accounts on HyperCore, which takes a transfer
that stays on the balance, and the pool would still be open to the second case.

Free exams (docs/MULTI-POOL.md, "Free exams") are the creator's too: how many a month, and the
address that signs the invitations, are in the pool's plan, fixed when it is created, so an
investor sees them before putting money in.

From then on the creator can do one thing in this contract: extend the pool in its last fourteen days. They hold
no share for being the creator, and they have no way to the money. The pool answers only this
contract, and this contract pays only holders.

## Putting money in

A deposit is USDC on HyperCore, sent to an address made for it. `openTicket()` makes one for the
caller, as in the shared pool people can see today: a contract can't see who sent a HyperCore
transfer, so the address is the attribution. The first transfer to it costs the sender 1 USDC for
creating the account.

`settle(tickets)`, by anyone, is a settlement point. Each named ticket holding at least the
smallest deposit (1 USDC or more, set at deployment) becomes shares, all at one price: the value
before them. The first shares are one per unit, 1e8 to a dollar. From the ticket the money goes
straight into the pool, where the traders' accounts are funded from; once the pool has stopped it
comes here instead. No ticket opens after the end.

## Leaving

- **Saying so.** `requestExit(shares)` asks for shares to leave at the end of the term, and
  `cancelExit(shares)` takes that back. Either works up to fourteen days before the end (the
  pool's `notice`). The shares asked out stay the investor's and go on bearing the pool's result.
- **The last fourteen days.** What was said holds: nothing is asked or taken back. The creator
  now knows who leaves and decides whether to extend.
- **Extending.** `extend(term)` by the creator, in those fourteen days only. It closes the round:
  the shares asked out are priced at the value now, leave the count, and what they are worth comes
  out of the pool together with the extension. If the pool cannot give that much, because the
  money is in the traders' accounts, the extension does not happen: nobody who asked to leave is
  held for another term. Whoever said nothing stays.
- **An extension made earlier** would move the end, and with it the moment to decide, away from
  an investor who had not decided yet. So there is none.
- **No extension.** The pool stops at its end. Its accounts are closed (`endSeat`, by anyone),
  and from then on any investor may ask to leave; `closeRound()`, by anyone, closes a round at
  that day's price and takes what it owes out of the pool. Until the accounts are closed and
  settled the pool cannot give the money, and the round waits.
- **Being paid.** `pay(holder)`, by anyone, pays what a holder's closed rounds owe: USDC on
  HyperEVM first (the exams' prices), the rest on HyperCore once it has landed here. A holder with
  no HyperCore account pays 1 USDC out of that part for creating it. A HyperCore part no larger
  than that would send nothing, so it stays owed until the account exists or USDC on HyperEVM
  covers it. `positionOf(holder)` shows shares that stay, shares asked out and what is owed.

With 60 put in by one investor and 40 by another, and the pool worth 110 at the end of the term:
the second asks to leave before the last fourteen days, the creator extends, 44 leave the pool and
are paid to them, and the first investor's 60 shares are still worth 66.

## Value, and what holds a price back

`value()` is the pool's value, what this contract holds on HyperCore and HyperEVM and what sits on
counted tickets, less what closed rounds owe. Every price is struck from it: deposits and rounds
alike. It counts the traders' open positions at the mark, as `MultiPool` does.

`blocker()` says when no price may be struck, and who holds it up:

- a funded account that has broken a rule and is not closed yet, or one being closed;
- a payment to a holder in the last minute, which may not have left this balance yet;
- money the pool sent here in the last minute, which the pool still counts against itself.

Money on its way from a ticket into the pool holds nothing back: HyperCore moves it between the
two accounts in one step, so the value reads the same before and after. That is also why this
contract's own HyperCore balance sees nothing leave but payments to holders: nobody can hold a
payment up by making that balance move.

## Weak points

- **If many leave at once,** and the money is in the traders' accounts, there is no extension: the
  pool stops at its end for those who wanted to stay as well. The other way round, paying the
  leavers part now and the rest later, would hold their money past the term they agreed to, and
  `MultiPool` lets money out only at the end of a term.
- **Whoever says nothing stays.** An investor who forgets is kept for the next term. The other
  default would have to pay every silent holder at each extension, a list with no bound.
- **A price moves with open positions,** as in every pool: an investor who comes in while a
  trader's position is up pays for it.
- **The creator chooses the moment of the leavers' price.** The round closes when the creator
  extends, at any moment of the last fourteen days, at the value then, open positions at the
  mark. A creator who also holds shares and stays could pick a minute of low marks. A fixed moment
  would need someone to call at exactly that minute and would still take the marks of that
  minute; it is left as it is, and said here.
- **Shares cannot be passed on.** There is no transfer.
- **Rounding.** What a round owes is rounded down for each holder; the remainder, under a unit
  (1e-8 USDC) per holder, stays counted as owed.

## Tests

`test/multi/PoolShares.t.sol`, 28 tests on the hyper-evm-lib simulator. 78 mutations (`Y238`–`Y307`
and `Y338`–`Y345` in `scripts/mutations.py`, run with the pool's), each caught by a named test:

```
forge test --match-path 'test/multi/*'
python3 scripts/mutations.py $(seq -f 'Y%g' 238 307) $(seq -f 'Y%g' 338 345)
```

## Not done yet

- A script that deploys it, a keeper and the app's pages.
- A run on the testnet.
- The audit's reading.
