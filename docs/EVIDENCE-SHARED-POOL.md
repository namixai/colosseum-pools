# What the shared pool has actually done on chain

Status: 25 September 2026, HyperEVM testnet (chain 998) and Hyperliquid testnet, mock USDC. The
shared pool (`src/shared/SharedPool.sol`, [the design](SHARED-POOL.md)) is a layer over the pools of
this demo. Nobody has reviewed it. Every line below is either a state a contract holds now, which
anyone can read, or a transaction that was read back from its receipt. The demo's own record is in
[EVIDENCE.md](EVIDENCE.md).

## The three pools

| Pool | Factory of its seats | Open it in the app | What it showed |
|---|---|---|---|
| `0x6cAA4Ce577728F8386FF15fcFaA8F224E261486A` | its own, `0x436d9074AB42001538D5a999a0eEe721deC516Db` | `#/shared/0x6cAA4Ce577728F8386FF15fcFaA8F224E261486A` | one depositor: deposit, shares, a request, payment at two points while the capital was in a challenge |
| `0x2f05940CA0da8302464ED6e82B91fa5628D9B0C0` | the demo's | `#/shared` (the page's default) | two depositors: a short payment split between them, the same fraction at one price |
| `0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6` | the demo's | `#/shared/0x547067e2D6c5627C5463c4cf62086eeD1B2C26a6` | a seat a trader bought and traded |

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

## What this does not show

- A passed challenge or a funded stage on a seat, and the funded term running out.
- A stop on a seat. The rules above would stop the challenge at 2.70 USDC of equity, or at 2.85 on
  the day; the trader stayed well inside.
