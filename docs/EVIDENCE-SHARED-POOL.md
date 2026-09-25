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

## What this does not show

- A deposit at a price other than 1: all three pools took their deposits fresh.
- A passed challenge or a funded stage on a seat, and the funded term running out.
- A stop on a seat. The rules above would stop the challenge at 2.70 USDC of equity, or at 2.85 on
  the day; the trader stayed well inside.
