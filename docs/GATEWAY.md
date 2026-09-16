# Pool gateway

Status: built and tested offline (`gateway/`), 17 September 2026. Not running anywhere yet.

The gateway sits between a trader and the Signer enclave. It asks one question before it
forwards an order: is this key bound, on chain, to this account and this trader, and is the
account allowed to trade right now? The answer comes from the chain, not from our database.

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

The trader signs the fields themselves, under the domain `colosseum-pools gateway`, version 2,
chain 998, as `Order(address account,uint32 asset,bool isBuy,string limitPx,string size,bool reduceOnly,string tif,uint64 nonce,uint64 expiresAt)`
or `Cancel(address account,uint32 asset,uint64 oid,uint64 nonce,uint64 expiresAt)`. The
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
5. The asset is in the account's rules. The enclave checks the platform list and the size
   caps again on its side.
6. `(trader, nonce)` hasn't been seen.

## Signing and submission

7. The gateway builds the action and sends it to the Signer demo gateway (`POST /sign`,
   exchange `hyperliquid_testnet`, the same `kind`, the action, the trader's `nonce`) with the
   bearer token of the tenant that holds `key`.
8. It recovers the signer of the returned signature the way Hyperliquid will (phantom agent,
   source `b`) and refuses to submit unless it is `key`.
9. It submits to `https://api.hyperliquid-testnet.xyz/exchange` and returns Hyperliquid's
   answer together with the enclave's signed decision receipt. A refusal comes back with its
   receipt and nothing is submitted.

## What the gateway does not prove

The trader's signature is checked by the gateway, which we run. The enclave build in use has
no trader-intent check for Hyperliquid, so the operator could submit an order inside the
rules without the trader. The chain shows which key traded which account; it does not show
that the trader asked for each order.

## Secrets and where it runs

The gateway holds one bearer token per enclave key. Tokens live on the gateway host only,
never in this repository. Where the gateway runs for the demo is still open.
