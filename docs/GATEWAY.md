# Pool gateway

Status: built and tested offline (`gateway/`), 17 September 2026; the demo signer added the same day. Running on the pools host since 18 September 2026, against the rehearsal deployment (docs/HOSTING.md).

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
5. The asset is in the account's rules. The signer checks the platform list and the caps again
   before it signs.
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
     (0.005 BTC, 0.15 ETH, 4 SOL) and a notional of at most 400 USDC.
     Anything else is refused as 403 `refused_by_gateway`, code `over_cap` or `policy`, and the
     nonce is given back. Then it signs with the key file for `key` (phantom agent, source `b`).
     - The notional is size times the highest price the order can fill at. A buy never fills
       above its limit, so it counts at its limit. A sell never fills below its limit, and one
       priced under the market fills at the bids, which are under the mid. So a sell counts at
       its limit or at the asset's mid, whichever is higher. The mid is read from Hyperliquid's
       testnet (`allMids`) when the sell is checked. Without a mid, nothing is signed: 502,
       code `no_market_price`.
     - A price that moves up in the second between that read and the fill isn't covered.
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
| 503 | (empty body) | more than 32 connections at once |

A connection that sends nothing for 10 seconds is closed.

## What the gateway does not prove

The trader's signature is checked by the gateway, which we run, so the operator could submit
an order without the trader. The chain shows which key traded which account; it does not show
that the trader asked for each order.

In the demo the gateway holds the keys. A key file can sign anything Hyperliquid lets an agent
sign, and the caps above only bind what goes through this code, so whoever controls the gateway
host can trade every account whose key is on it until someone stops that account.

## Secrets and where it runs

In `demo` mode the gateway reads one owner-only `*.key` file per agent key from
`GATEWAY_KEYS_DIR` (an owner-only directory) and refuses to start if either is open to others;
the address comes from the key. In `signer` mode it reads one bearer token per key from
`SIGNER_TOKENS_FILE`. Either lives on the gateway host only, never in this repository.
`GET /v1/health` names the mode and, in `demo` mode, how many keys it holds.
