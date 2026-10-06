# What the shared pool has actually done on chain

Status: 5 October 2026, HyperEVM testnet (chain 998) and Hyperliquid testnet, mock USDC. The
shared pool (`src/shared/SharedPool.sol`, [the design](SHARED-POOL.md)) is a layer over the pools of
this demo. Nobody has reviewed it. Every line below is either a state a contract held when its
section was written, which anyone can read, or a transaction that was read back from its receipt.
The earlier sections stand as they were written; where a later day changed a state they name, the
later section says so. The demo's own record is in [EVIDENCE.md](EVIDENCE.md).

## The shared pools

| Pool | Factory of its seats | Open it in the app | What it showed |
|---|---|---|---|
| `0x6cAA4Ce577728F8386FF15fcFaA8F224E261486A` | its own, `0x436d9074AB42001538D5a999a0eEe721deC516Db` | `#/shared/0x6cAA4Ce577728F8386FF15fcFaA8F224E261486A` | one depositor: deposit, shares, a request, payment at two points while the capital was in a challenge |
| `0x2f05940CA0da8302464ED6e82B91fa5628D9B0C0` | the demo's | `#/shared/0x2f05940CA0da8302464ED6e82B91fa5628D9B0C0` | two depositors: a short payment split between them, the same fraction at one price |
| `0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6` | the demo's | `#/shared/0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6` | a seat a trader bought and traded |
| `0x43f7562CF3aDD90942416a74aBFC8Ee0A3F6a717` | the second deployment's, `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222` | `#/shared/0x43f7562CF3aDD90942416a74aBFC8Ee0A3F6a717` | a pool deployed with the seal: started, one seat published, the book sealed before any deposit |
| `0xa2eEe2CF75d5f740E436a08007345dA5789a4499` | the second deployment's, `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222` | `#/shared/0xa2eEe2CF75d5f740E436a08007345dA5789a4499` | three seats of different sizes, a fee of 0 on a holder's gain, and a seat an agent trades through the client |

The first three are pools of the earlier rounds: two have their seats on the first deployment's factory,
which is an archive now, and one on a factory of its own. The app opens each by its address, shows its
record, and takes no deposit into it; a holder can still ask to withdraw. The fourth and the fifth have
their seats on the second deployment's factory, the one the gateway and the keepers serve. `#/shared`
with no address opens the fifth, and it is the one pool the app takes a deposit into. The fourth is
kept as a record: its one seat is too small for an agent to trade through this repository's client.

## Checking it yourself

Each kind of record has its own source:

- **Contract state.** A plain `eth_call` on `https://rpc.hyperliquid-testnet.xyz/evm`, as below.
- **Transactions.** `eth_getTransactionReceipt` for each hash.
- **Trades.** Hyperliquid's testnet info API: `POST https://api.hyperliquid-testnet.xyz/info` with
  `{"type": "userFills", "user": "0x416C4D9B9A3973C39607f0Ac68Dbbc6bA1b476A7"}`.
- **The gateway's answers.** Recorded as they came back, in `spike/results/2026-09-25.jsonl`.

The contract state:

- `SharedPool.value()`, `totalShares()`, `sharesOf(holder)` and `queuedShares()` give the pool's
  state now. `payments(holder)` gives when the pool last paid a holder and what it has paid them so
  far, HyperEVM and HyperCore apart. The first pool is older than `payments` and reverts on it.
- `SharedPool.seats()` names the seats. Each seat is an ordinary `Pool`: `owner()` is the shared
  pool, `rules()` and `terms()` are what was published, and on the demo's factory
  `PoolFactory.isPool(seat)` is true.
- `KeyRegistry.bindingOf(key)` on the demo's registry `0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA`
  says which account a key trades and for whom.

The app's shared pool page does the same reads in a browser.

## Two depositors, a short payment split

On `0x2f05940C…B0C0`, `payments` answers the same for both depositors:

| Holder | `payments(holder)` | `sharesOf(holder)` |
|---|---|---|
| `0xbD97438655835138daBeE38f3B7d96275eDc315a` | at 1790310783 (04:33:03 UTC), HyperEVM 0, HyperCore 1951219500 (19.512195 USDC) | 0 |
| `0x278AbBC5B78F34F77829dDd7887566E182beBD83` | the same | 0 |

Each had put in 20 USDC. The pool was 41 USDC on 41 shares (the platform's 1 included). Creating the
seat's HyperCore account cost the pool 1 USDC, so every share lost 1/41 of a dollar alike, the
platform's too: 20 × 40/41 = 19.512195. The short payment came first: point
`0x39994dbf111750d6624e28a22b524ba3bd177643844485b7382faf93508a3209` had 28 USDC free against 39.02
owed and paid each 13.999999 USDC, the same 71.75% of their request, both through CoreWriter in one
transaction. Point `0x09190d77acbc59e87c9924a89db461231df768a3f0dc239b2a5c0d7088ec6a33` paid the rest,
5.512196 each, once the seat's capital was back.

## A seat a trader bought and traded

On `0x547067e2…26a6`, the seat `0xa24868c68088dcd0c1d8e3121af1d7bec40f8c7c`:

- **Terms and rules.** `rules()` is `(500, 1000, 500, [0, 3, 4])`: a 5% daily loss, a 10% drawdown, 5× leverage, SOL, BTC
  and ETH. `terms()` is a 1.5 USDC price, 3 USDC of challenge capital, an 8% target, a day, 80% of a
  funded profit to the trader, and 30 USDC of funded capital. The Economics page prices that pool;
  its floor for the price is 1.05.
- **Where the capital came from.** The seat's 34 USDC came from two deposits of 20: point
  `0x820c72ddc394b9351eabe38825c9b75f5b9fff4b589b121f34229048900c7203` gave each depositor 20e8 shares,
  and `armSeat` `0xc39506009ac9dc673b8fdbc38bf5d6883529f735b8c0975a1fd751292b4ce3cd` moved the
  capital.
- **The trader.** `0xc6E83B1B88110C54e34977AD215E94cfdbC0374E` bought its challenge
  `0x416C4D9B9A3973C39607f0Ac68Dbbc6bA1b476A7`, paying the 1.5 price and the 0.7 platform fee
  (`buyChallenge` in `0x31f09c61aa7cd0194946bfa2663fda46e701120edf02cf6e91c9d192127260e1`). The
  challenge was activated in `0xc1a02149831173b539597b2dcfff83fbdd2a208b8c4d4d656a4f73c5cdd12b74`.
- **The key.** `KeyRegistry.bindingOf(0x01b4811f38A5613E2882976c546c0921d56A2A07)` is `(2, challenge,
  trader)`: Bound, to that challenge, for that trader.
- **The trades.** The trader bought 0.00013 BTC at 84 619, about $11, and sold it at 84 618. Hyperliquid's
  fills for the challenge account (`userFills`) carry the hashes
  `0x816918ba80d4b58782e2042a27dafa010b0030a01bd7d4592531c40d3fd88f72` (oid 61001916689) and
  `0x28ba561e1b2ff35a2a34042a27ddf6010c006e03b623122ccc830170da23cd44` (oid 61001960703). The account
  is flat again at 2.98997 USDC. The fees and the one-dollar move took 0.01.

**How the trader acted.** Not by clicking the app's pages: we would have had to put the trader's
private key into a browser wallet, and we don't do that. The buy and both orders went through
`agents.client`, the gateway client in this repository. That is the same path the app takes. The
purchase is `approve` and `buyChallenge` from the trader's own wallet. Each order is signed by the
trader's wallet and sent to the live pool gateway (`https://pools-api.usenami.io/v1/order`), which
checked the binding and signed it with the challenge's agent key. Twice before the first order went
through, the gateway answered 429 `upstream_busy`: its own chain node was refusing reads. That answer
means wait, not a refusal of the order.

## A deposit at a price other than 1, and the way out at that price

On `0x547067e2…26a6`, after the trade above, the pool held 41.48997 USDC on 42e8 shares, a price
of 0.98785643 a share. A new holder, `0x329166c41C9249Aa857cd7B7D8F3aF5F4E4CE09f`, deposited 20
USDC through a ticket of its own. Once the lock had run, it asked for everything.

| | `value()` | `totalShares()` | new holder's `sharesOf` | each earlier depositor's `sharesOf` | price of a share |
|---|---|---|---|---|---|
| before the deposit's point | 41.48997 | 42e8 | 0 | 20e8 | 0.98785643 |
| after point `0x2dce964a4cb671d034e3e1cefd9795b2fb28a1e8b1b5e43c7482e03c2836c353` | 61.48997 | 62.24585701e8 | 20.24585701e8 | 20e8 | 0.98785643 |
| after point `0x248495a343ea8b898d25679b0ffe902ac76a6d0c834a2e1ae188d688e7dfd462` | 41.489971 | 42e8 | 0 | 20e8 | 0.98785645 |

- **In.** 20 × 42e8 / 41.48997 = 20.24585701e8 shares: the deposit bought at the price it found, not
  at 1. The earlier holders' 20e8 shares were worth 19.757 USDC before it and after it.
- **Out.** The request was `0x864349c7744deba7498b9a207401534f01fbd9335b99781056d5f418e28e7926`. The
  point paid 19.999999 USDC at the same price: 1.5 on HyperEVM, the challenge price it collected from
  the seat, and 18.499999 on HyperCore. `payments(0x329166c4…E09f)` reads `(1790327053, 1500000,
  1849999900)`. That is 20.24585701e8 shares at the point's price, 0.98785643, rounded down to the
  millionth a payment carries. The millionth left behind stays in the pool: the value after the point
  is 41.489971, not 41.48997, and the price of a share went up in the eighth decimal, to 0.98785645,
  for everyone still in. The earlier depositors' 20e8 were worth 19.75712857 before and 19.75712905
  after.
- **The money went back.** The step was funded by `0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4`, which
  sent the pool's operator 22 USDC on HyperCore (`0xd53c3f5396b7bdbdd6b5042a2822ca010900573931badc8f7904eaa655bb97a8`
  in the ledger); the operator passed 21 to the new holder. The holder's payout went back to that
  address: 18.499999 on HyperCore (`0x05302871574daf0506a9042a2861550103004056f240cdd7a8f8d3c4164188ef`)
  and 1.5 on HyperEVM (`0x04ced3f1dbe84f83f48a7848f3fa5c9a834caa9e5f7de6352b6cca39b68e0819`).
  Its two new HyperCore accounts, the holder's and the ticket's, cost 2 USDC; that money is gone.

## Both earlier depositors leave, at the price of a share (3 October)

On `0x547067e2…26a6`, eight days after the trade, the pool held 41.489971 USDC on 42e8 shares, a price of
0.98785645 a share: 8.500001 free on HyperCore and 32.98997 on its seat, which was idle. Both earlier
depositors, `0xbD97438655835138daBeE38f3B7d96275eDc315a` and `0x278AbBC5B78F34F77829dDd7887566E182beBD83`, asked for all of their 20e8 shares
(`0x1b18a7821e2e060d7c7ba213cb066c02bee2855017e09713fe81edf59f825b0a` and
`0x856ee70f18da440f53e651d04959715671013ab6cdd546304a94b4124620644d`).

- **A short payment, split evenly.** Point `0x2f0345103f2c0ee46b27164ddb02cbb7f73b9992cc87e4b1fcb49919918b5c1c` had
  8.500001 free against 39.514258 asked. It paid each depositor 4.249999 USDC for 4.30224401e8 of their
  shares, and left 15.69775599e8 of each waiting.
- **The seat gave its capital back.** `releaseSeat`
  `0x87a44b73807c59d3a57b622ceb7a51ff7cb4bf86a3fe86786d4ff31b004c81d4` returned the seat's 32.98997 USDC to the
  pool. The pool's own payments on HyperCore are given five minutes to land, so the next point waited
  that long.
- **The rest.** Point `0x4c1836407ecd83e083ac69ee62eee49f08c6b619380eea9dc42cd6c42c29ac63` paid each depositor the
  remaining 15.50713 USDC and emptied the queue. Each depositor's HyperCore spot balance read 19.757129
  USDC afterwards: 20e8 shares at 0.98785645, rounded down to the millionth a payment carries.
- **What is left.** The pool holds 1.975713 USDC on the platform's 2e8 starting shares, which never
  leave. Its seat is idle and empty.

The tables above are the pool's state at the points they name. Read now, `value()` and `totalShares()`
give what this section left: 1.975713 and 2e8. Every step's hash is in `spike/results/2026-10-03.jsonl`.

## The pool on the second deployment (3 October)

`0x43f7562CF3aDD90942416a74aBFC8Ee0A3F6a717`, deployed in
`0x1ad996f5295dab3f32eabd5be888cc5b8c522eb30bf8d8f05c689fb2161edec0` (block 65899174) with its seats
on the second deployment's factory, `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222`
(`deployments/testnet-shared-demo2.json`). A smallest deposit of 20 USDC, a lock of ten minutes, a fee
of 10% of a holder's own profit.

- **Started.** `start` in `0x4f08a713d6f763b48b7551049aaf8c1c089701c7ab0e2659300d452b27883228` turned 2 USDC
  from the operator into the platform's 2e8 starting shares.
- **One seat, published.** `addSeat` in `0xbdc52e52ac568b57df7bab0e86902a569e15768cb4fe0048aed7e78c4afc088d`
  made the seat `0x7eb971134b0ad523f55c76b44b2fbd6ed7885371`: 3 USDC of challenge capital and 30 funded, a
  1.5 USDC price, an 8% target, a 5% daily loss, a 10% drawdown, a day for the challenge and a day for
  a funded stage. `PoolFactory.isPool(seat)` on the second deployment's factory is true, and the seat's
  `owner()` is the shared pool.
- **Sealed before any deposit.** `seal` in `0x6860ec99f69152efa4d32236944aa549861bb584d2eb995c260b11e02e164c6b`
  closed the book: `seatsSealed()` reads true. This pool has one seat and can never be given another.
  Until that transaction the contract refused to open a deposit ticket.
- **Two deposits, at a price of 1.** `0xbD97438655835138daBeE38f3B7d96275eDc315a` opened the ticket
  `0x1739a40eeb6de7aa0a9dbccc69188ef012a9348f` in
  `0xb2b57aa7c564103e73bd75809a99fc32d3d71b9c2670c02d1245e9cb016718e9`, and
  `0x278AbBC5B78F34F77829dDd7887566E182beBD83` the ticket
  `0xad4819148054d513a8bec833f73113cebeeda50a` in
  `0x0cb386e01f761623331478730bf9c202ab1e106e4c76e35ab3f198b246102a3f`. Each sent 20 USDC to its ticket on
  HyperCore. Point `0x96e762b459f887f7644b1a0dcd34fadf07f0d100d99b61e35a858f393d81e6aa` (block 65903404)
  took both in: `totalShares()` went from 2e8 to 42e8, each depositor's `sharesOf` and `basis` read
  20e8, and `value()` reads 42 USDC, a price of 1 a share. Both tickets read as closed.
- **Where the depositors' money came from.** The two depositors are the ones who left the third pool
  the same morning with 19.757129 USDC each. The operator sent each 1.242871 more, so that each held
  21: the deposit and the 1 USDC HyperCore charges for creating a ticket's account.

**A stand, not a product.** The seat's numbers are small on purpose: 3 USDC at 5× leverage holds a
position above the exchange's minimum order of $10. They show the mechanics on testnet money. They are
not terms anyone is offered.

**Who runs what.** Every wallet here is ours. The seat is a pool of the second deployment's factory, so
the gateway and the keepers on the host see it like any other pool there; no challenge has been sold
on it, so that has not been exercised. The shared pool's own keeper, `ops/shared_keeper.py`, is not
installed on the host. It has not been run for this pool yet; when it is, it runs from the team's
machine, not from the host.

**Not armed, on purpose.** The seat holds no capital yet, and the list of pools shows it as not
prepared. Once a seat has its capital and its account is prepared, anyone can buy its challenge from
the site. So this one is armed only right before the sale it is meant for, not earlier.

At 08:59 UTC on 3 October the pool held 42 USDC on 42e8 shares, all of it free on HyperCore, with
nothing waiting to be paid. Its seat was idle, empty and not armed. Every transaction this run sent
on HyperEVM is in `spike/results/2026-10-03.jsonl` with its hash. Its six transfers on HyperCore —
the operator's perp to spot, the seed, the two wallets and the two deposits — are there with
Hyperliquid's answer and no hash: the answer gives none.

## A challenge sold on the seat, and started by the host's keeper (4 October)

On `0x43f7562C…a717`, the seat `0x7eb971134b0ad523f55c76b44b2fbd6ed7885371`. One sale, the only one this pool is meant to
have: each takes two keys from the second deployment's registry.

- **Armed, then bought within the minute.** `armSeat` in
  `0xb6ca52290d1e0d03add35fc5379008af8cdcc3a2c3f8398e72e1b13422a858a5` moved 34 USDC from the pool to the
  seat, and `prepareAccount` in `0x71892ba4e699cc1f482bc5753a268406878873671c7e27be55bf1b6b02a7b7c9`
  prepared its account. Creating the seat's HyperCore account cost the pool 1 USDC more.
- **The trader.** `0xc6E83B1B88110C54e34977AD215E94cfdbC0374E` bought its challenge
  `0xC9435A77Aba8bd9FCA244EeD6cE4Ff9727aE9262`, paying the 1.5 price and the 0.7 platform fee
  (`buyChallenge` in `0x690aaaf73646e224884059d0104ba9a86328a98646f3129473587074e60f4b6d`, block 65983974). The
  operator had sent the trader 2.11 USDC on HyperEVM for it
  (`0xe8033a88728c2af2a01eb2a0fa0735817fa3e9dddea673de840b1dae855f9246`).
- **Started by the keeper on the host.** `activate` in
  `0xd137d81e9f6256d8f6b9bae3f3f05eeba820529d6a5eefe26bc021185e39eedb`, block 65983979, five blocks after the
  purchase, sent by `0xD6F07317fC5f12302776b03A7206B1614FD49021`, the address of the keeper on the host. This
  run did not send it. `status()` on the challenge reads 2, Active, with a deadline of 5 October 07:00:15 UTC.
- **The key.** `KeyRegistry.bindingOf(0x7d2650bd3dD9A6eD6Cd0c6C393BB79De837FBe19)` on the second deployment's
  registry `0x53AF27F65Dd7473c890f633aC0025b261307779e` is `(2, challenge, trader)`: Bound, to that challenge, for
  that trader. `freeCount()` went from 18 to 16.
- **Nothing was traded.** The trader acts through `agents.client`, the gateway client in this repository, and
  that client caps one order at 0.4 of the leverage rule: 6.00 USDC on 3 USDC of equity at 5×. Hyperliquid takes
  no order under 10 USDC. Asked without sending anything (`--dry-run`), the client answered an order of 11.07
  USDC with "over this session's cap of 6.00 per order" and one of 5.96 with "under Hyperliquid's minimum order
  of 10 USDC". The challenge account holds its 3 USDC, with no position and no order. The cap is the client's
  own; the gateway and the contracts would have taken an order inside the leverage rule, and none was sent to
  them another way.
- **What it did to the pool.** `value()` reads 41.5 USDC on 42e8 shares, a price of 0.98809524 a share: 7 free
  on HyperCore, 30 on the seat, 3 on the challenge account, and the 1.5 price on HyperEVM at the seat. Two new
  HyperCore accounts, the seat's and the challenge's, cost 2 USDC; the price brought 1.5 in.

Every wallet here is ours. The keeper's transaction was sent by the host, not by this run. Every step's
hash, and the client's two answers, are in `spike/results/2026-10-04.jsonl`.

## The seat's challenge runs out, the host settles it, and both depositors leave (5 October)

On `0x43f7562C…a717`, the challenge `0xC9435A77Aba8bd9FCA244EeD6cE4Ff9727aE9262` sold the day before. Its
deadline was 5 October 07:00:15 UTC. Nobody of this run was watching at that hour.

- **Expired by the keepers on the host, eighteen seconds after the deadline.** `expire` in
  `0x4641ad99435632adf219302db9146b83bdc02a186fe7ab02468e9988ca38c3cb`, block 66071763 (07:00:33 UTC),
  sent by `0xcbd5C0299669e0C686D375cc6C07584Ad5C4fECa`. The ending is `Stopped(status 4 Expired, reason 0
  None, equity 3000000)`: it ran out of time, no rule was broken, and its 3 USDC were untouched.
- **Settled in three steps, two minutes in all.** `settle` in
  `0x46c3b230d7f185c8fa084f45d60c0b0da053fa098f8c4ffbbb9e37c35cc05762` and in
  `0xbd85f0c1d62cc8a96d9d093dd855d105ec59845d1bae5e69e6ee3acc30ffe53a`, both from the same address, and in
  `0x47312683bd34d268075988efc423671c0536293b955b98e9ad5bbef605cfc6a8`, block 66071867 (07:02:15 UTC), from
  `0xD6F07317fC5f12302776b03A7206B1614FD49021`. After them `status()` reads 8, the seat is Idle with 33
  USDC on HyperCore, and the 1.5 price is still held at the seat on HyperEVM. This run sent none of the four.
- **Both depositors asked for everything.** `0xbD97438655835138daBeE38f3B7d96275eDc315a` in
  `0xe781f6f2f8c4a118cae70ababb34f5793e9f40b40ad1a6228eafbc008c97c572` and
  `0x278AbBC5B78F34F77829dDd7887566E182beBD83` in
  `0xae12e7e7f577c5a4b12e39e95b5abbf5208e67b74e2b819e7afc06c2cd0e4dc5`, 20e8 shares each.
- **The shared pool's keeper, run from the team's machine.** One pass of `ops/shared_keeper.py`, told not to
  arm anything, from the wallet `0x75deec8b513Ee3f5D15d6b40B706A3314cf26590`. It sent two calls anyone may
  make: `releaseSeat` in `0x0b590ba6b8f4c6aa384442aa19cc26ea4a086d9275b5913bd9163be53c45128e`, block 66075722,
  which brought the seat's 33 USDC back, and `settle` in
  `0x1654ea6b81944b8601d9fcdd90f4307863d56f3e76924026dcd0b1ff64f8fcb0`, block 66075727, five blocks later.
  The shared pool's keeper is still not installed on the host.
- **One point paid both in full.** `PointSettled(point 2, value 4150000000, sharesBefore 4200000000,
  sharesAfter 200000000)`, and for each depositor `Paid(shares 2000000000, feeShares 0, evm 750000, core
  1901190400)`: 19.761904 USDC, which is 20e8 shares at 0.98809524 rounded down to the millionth. The point
  took the 1.5 price from the seat and paid it out on HyperEVM, half to each. `payments(holder)` reads
  `(1791187532, 750000, 1901190400)` for both.
- **Read back from the balances, not from the receipt.** Each depositor's HyperCore spot balance read
  19.011904 USDC and each wallet 0.75 USDC on HyperEVM after the point. A receipt only says the payment
  was asked for; HyperCore carries it out afterwards.
- **No fee was taken.** This pool was deployed with a fee of 10% of a holder's own gain, and neither
  holder had one: each paid 20 and got 19.761904 back.
- **What is left.** The pool holds 1.976192 USDC on the platform's 2e8 starting shares. Its seat is idle
  and empty.

On 3 October the same way out took two points and a wait of five minutes, because the first point paid
before the seat's capital was back. Here the seat was released first and one point was enough. Every
step's hash is in `spike/results/2026-10-05.jsonl`; the host's four are marked there as read from the
chain.

## A second pool on the second deployment, with three seats (5 October)

`0xa2eEe2CF75d5f740E436a08007345dA5789a4499`, deployed in
`0x814ec30da330aea2e9e4c8c30a3f6d6a0e686c72a4bb69ab4591e1dd00b13153` (block 66066301) with its seats on the
second deployment's factory (`deployments/testnet-shared-demo2b.json`). A smallest deposit of 50 USDC, a
lock of ten minutes, and **a fee of 0**: `feeBps()` reads 0, so the platform takes nothing from what a
holder gains. The number is fixed in the contract and cannot be changed.

- **Started.** `start` in `0x259e5d27abd94a61778054a5e5f7b76977158047d77ebb20f799d178d568352e`: 34 USDC from
  the operator became the platform's 34e8 starting shares.
- **Three seats, published.** All under the rules of the live-run pool: a 3% daily loss, a 6% drawdown,
  5× leverage, SOL, BTC and ETH, a 10% target, 0 to the trader on the challenge and 80% on the funded
  stage.

  | Seat | Challenge capital | Funded capital | Price | `addSeat` |
  |---|---|---|---|---|
  | `0xf40da2862f1e3f20ab7d1ed7b5e67a83d333cb3b` | 10 | 100 | 2 | `0x9efc68b450db0a077de5499176ddedbd352ac35f72b3d08d2c13ea8a0f98129f` |
  | `0x4e8884ceb7f8f72aa4877bc71e217958567750a9` | 20 | 200 | 4 | `0x3719e296a3afe70b7482fe54ea9c1c1fd79dd925171aacd89d511fc672679651` |
  | `0x17df6fb2e45b13c7bd5340d30551749609607b3b` | 30 | 300 | 6 | `0xb97c1ae82e6d0ab14a1e68d7559941e51f6741ee27c9154155e2c91d4acd41b6` |

  `planCapital()` reads 663 USDC against a seed of 34: the contract asks for at least a twentieth.
- **Sealed before any deposit.** `seal` in
  `0x33ee1b7d844a54092749995b13a4f37df85b241bd50f405b594448abf88ba811`. Three seats, and never a fourth.
- **Two deposits of 318, at a price of 1.** Tickets opened in
  `0xb675dc236f779e21b743fdcbe0f3e4435b99a73e07816c0d912bab748b5e7713` and
  `0x9b39c4c3ce985d26c28ab7b2857897c2c7613d8207cf102eec4bdf9b8b279eec`; point
  `0x45abc4b1ca63476f45a1d2099ddcaffe700233d9be95b973f417740c8410bfd8` (block 66066642) took both in.
  `totalShares()` reads 670e8 and `value()` 670 USDC.
- **All three armed.** `armSeat` in
  `0xb1123cdbd0b9cdeeec872da904e9091db44b7429aae69a7d4b18673a30ea336e`,
  `0x9b46cf2daf75adaf43e7eb759ede3228618b552f8c7a8f07738f758855df5fd5` and
  `0x9dac9aee35c3e1eed05a193ab37656be91a5bc9f3b31112b04ea89daac33cf95` moved 111, 221 and 331 USDC to the
  seats, each followed by `prepareAccount`. Three new HyperCore accounts cost the pool 3 USDC: `value()`
  read 667 on 670e8 shares, and 4 USDC stayed free.

**Short on purpose.** A challenge on these seats runs seven days and a funded stage at most one. Those
terms are there so that a pass and the end of a funded term can be seen within a week on testnet money.
They are not terms anyone is offered. The sizes are small for the same reason; what they are large
enough for is the trader's client, which needs 5 USDC of capital at 5× to send one order.

### The first seat sold, and traded by an agent

- **The trader.** `0xd4f31E7234308546c822C619705F1A4B5fC8f629`, the same wallet of the team's agent as in
  the second deployment's record in [EVIDENCE.md](EVIDENCE.md), bought the challenge
  `0x48f948a895F82a32fb39eE57C457c05FdA7A7779` on the seat `0xf40da2862f1e3f20ab7d1ed7b5e67a83d333cb3b`, paying the
  2 price and the 0.7 platform fee (`buyChallenge` in
  `0xa8bf8df694b54b65f067f9142c7d3d6e1e9c017e7285f6abbbf5c5f18084eb11`, block 66068662, 5 October 06:09:43
  UTC). We funded that wallet and asked the agent to buy; it is not a trader we do not control.
- **Started by the host four blocks later.** `activate` in
  `0x418a526dac82ed383c1a23cad9686a05f8fd3cfa8312ebd4e77952bca16cb3c1`, block 66068666, sent by
  `0xcbd5C0299669e0C686D375cc6C07584Ad5C4fECa`. `status()` reads 2, Active, with a deadline of 12 October
  06:09:47 UTC.
- **The key.** `KeyRegistry.bindingOf(0x8c2eDe5DEd6B6188E22e222300a691Cf7509A7e6)` on the registry
  `0x53AF27F65Dd7473c890f633aC0025b261307779e` is `(2, challenge, trader)`. `freeCount()` went from 28 to 26.
- **A trade, through the client.** Two and a half minutes after the start the agent bought 0.0062 ETH,
  16.76 USDC, in one order: Hyperliquid's fills for the challenge account (`userFills`) carry the hash
  `0xc4f16fdff253ec0ec66b042af0c24001050087c58d570ae068ba1b32b157c5f9` (oid 61879888823), 0.0024 at 2703.5
  and 0.0038 at 2703.2. The client's cap on this account is 20 USDC, so the order fits where the one on
  the 3 USDC seat could not.
- **The stop and the take are the exchange's.** Read at 08:12 UTC, the account carried two reduce-only
  position orders placed by the gateway, a stop at 2656.7 and a take at 2869.2. They move as the gateway
  follows the rule line and the target; the numbers are of that reading.

Read at 08:11 UTC on 5 October: `value()` 668.147546 USDC on 670e8 shares, with a position open. The other
two seats were Idle and unsold. Every transaction this run sent on HyperEVM, from the start to the
arming of the three seats, is in `spike/results/2026-10-05.jsonl` with its hash. Its five transfers
on HyperCore — the seed of 34 USDC, 319 to each of the two depositors' wallets and their two deposits
of 318 — are there with Hyperliquid's answer and no hash: the answer gives none. The purchase, the
start and the trade were not ours to send and are not in that log; their hashes are the ones given
above.

### The second seat, bought and traded by the team's red team (5 October)

- **The buyer.** `0x5520a19389Cbd5dd6526315451799E8246b27C10`, a wallet the team funded for its own red
  team, a bot set to try the rules rather than to trade well. It bought the challenge
  `0x7aa03B872FAd788030456616f644344dEa209feE` on the seat `0x4e8884ceb7f8f72aa4877bc71e217958567750a9`,
  paying the 4 price and the 0.7 platform fee (`buyChallenge` in
  `0x2ec9115e76fd79ff32410ee8a0dea83372abbba701b400a0a57e6ef70b357c39`, block 66114957, 5 October 18:48:39
  UTC). It is not a trader we do not control.
- **Started by the host 31 seconds later.** `activate` in
  `0x37cfbaff33471429930e6e431941c105f1efd2f22136071d51404a7ce672a028`, block 66114989, 18:49:10 UTC. The
  deadline is 12 October 18:49:10 UTC; the key is `0x21dAb145…`.
- **Two orders in, one out.** Through the client: a sell of 0.004 ETH at 18:52:06 UTC, filled at 2702.2,
  then 0.010 at 18:53:06, filled at 2702.2 and 2702.3; a reduce-only buy of 0.014 at 18:56:47 closed the
  short at 2704.1 and 2704.2 (Hyperliquid's `userFills`). Closed PnL −0.027 USDC; `accountValue` read
  19.939413 after it.
- **The stop and the take stood the whole time.** Hyperliquid's order history for the account: a stop at
  2852.4 and a take at 2197.5 placed at 18:52:05, a second before the first fill; the stop brought to
  2850.9 at 18:52:17; both re-lined for the larger position at 18:53:04 and 18:53:05, to 2744.9 and
  2554.3, before the second fill; the stop to 2743.8 at 18:53:08. Each replacement is a cancel and a new
  order in the same second. Both went with the position at 18:56:47 (`reduceOnlyCanceled`). Against the
  seat's rules on 20 USDC the lines hold: a 3% day is 0.60, and 0.004 × (2852.4 − 2702.2) = 0.60; a 10%
  target is 22, and 0.004 × (2702.2 − 2197.5) = 2.02.
- **Two refusals from the contract, read through the client.** `graduate` with the short open was refused
  `NotFlat()`; flat and below the target, `TargetNotMet(19.939413, 22.0)`. The client of that day ended on
  both with "run `forge build` first" instead of the reason: finding A-21 in
  [REVIEW-NOTES.md](REVIEW-NOTES.md), fixed since.
- **One more defect of ours, still there at the time of writing.** At 19:00:31 UTC the bot rested a buy
  of 0.005 ETH at 2000, far from the market, and cancelled it at 19:01:53. The gateway had put a stop and
  a take on the book for that order a second before it, at 19:00:30, and re-lined them twice. The order
  went; the two stayed. Read at 12:43 UTC on 6 October, the account holds no position and two position
  orders of the gateway, a stop at 2596.4 and a take at 3121.2. They can do nothing while nothing is open,
  and the gateway lets an account with nothing open go after two such answers (A-19), so nothing now
  removes them: a position opened later on this account would start with a stale pair on the book. Not
  fixed.

### The third seat, bought and traded by the team's trader bot (6 October)

- **The buyer.** `0xd4f31E7234308546c822C619705F1A4B5fC8f629`, the wallet of the team's agent, the same
  that bought the first seat. It bought the challenge `0x67de5E792c251384C1c6A80C1e7766E7CCF82ea0` on
  the seat `0x17df6fb2e45b13c7bd5340d30551749609607b3b`, paying the 6 price and the 0.7 platform fee
  (`buyChallenge` in `0x6fb6616d79cac32cc1f5adf8b63ec4683aa90b35e6c9fc3874ba7a41d022b211`, block
  66155662, 6 October 05:55:57 UTC). Not a trader we do not control either.
- **Started by the host 17 seconds later.** `activate` in
  `0xd04cdbc9decccbcc6ef366873dcf17d32e8e7124c88a17721b57f60d743a39b9`, block 66155680, 05:56:14 UTC. The
  deadline is 13 October 05:56:14 UTC; the key is `0x21DAF17b…`. On 30 USDC the target is 33, the
  drawdown line 28.2 and the first day's line 29.1.
- **One short, closed at a loss.** A sell of 0.00053 BTC at 06:16:11 UTC, filled at 85376 and 85378,
  with the gateway's stop at 87047 and take at 79534 placed a second before, the stop brought to 87036
  at 06:16:28. A reduce-only buy at 10:18:55 closed it at 86310: closed PnL −0.495 USDC, `accountValue`
  29.499179 after it, inside the rules. Both position orders went with the position.

## What this does not show

- A passed challenge or a funded stage on a seat, and the funded term running out. The seats of the
  fifth pool are short for that; none had passed at the time of writing.
- A holder leaving with a gain. Every way out recorded here was at a price under 1, so no fee on a
  gain was ever due, under the 10% of the earlier pools or the 0 of the fifth.
- A trader we do not control on a seat. The agent that bought the fifth pool's first seat is the team's.
- A stop on a seat. The rules above would stop the challenge at 2.70 USDC of equity, or at 2.85 on
  the day; the trader stayed well inside.
