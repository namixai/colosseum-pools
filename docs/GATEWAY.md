# Pool gateway

Status: design, 16 September 2026. Nothing here is built yet.

The gateway sits between a trader and the Signer enclave. It asks one question before it
forwards an order: is this key bound, on chain, to this account and this trader, and is the
account allowed to trade right now? The answer comes from the chain, not from our database.

## Request

`POST /v1/order`

```json
{
  "account": "0x… challenge or pool address",
  "kind": "order" | "cancel",
  "action": { "type": "order", "orders": [ … ], "grouping": "na" },
  "nonce": 1789600000000,
  "expiresAt": 1789600060000,
  "signature": "0x… EIP-712 signature by the trader's wallet"
}
```

The trader signs `GatewayOrder(address account, bytes32 actionHash, uint64 nonce, uint64 expiresAt)`
under the domain `colosseum-pools gateway`, version 1, chain 998. `actionHash` is the
keccak of the action's msgpack encoding, the same bytes Hyperliquid hashes, so the trader
signs exactly what will be submitted.

## Checks, in order

1. The request is well formed; `kind` matches `action.type`; `expiresAt` is in the future and
   less than a minute away.
2. The signature recovers to an address: the trader.
3. `PoolFactory.isAccount(account)`; the account is trading (`ChallengeAccount.status ==
   Active`, or `Pool.stage == Funded`).
4. `key = RuledAccount.agentKey(account)` and `KeyRegistry.isBound(key, account, trader)`.
5. Every order is for an asset in the account's rules. The enclave checks the platform list
   and the size caps again on its side.
6. `(trader, nonce)` hasn't been seen.

## Signing and submission

7. The gateway sends the action to the Signer demo gateway (`POST /sign`, exchange
   `hyperliquid_testnet`, the same `kind`, `action` and `nonce`) with the bearer token of the
   tenant that holds `key`.
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
