# Key binding: agent keys without a list

**Status: in main, not deployed.** demo2, the first deployment and demo3 still take their agent keys from
`src/KeyRegistry.sol`, a list of published keys; this page is about `src/KeyBinder.sol`, which replaces it
for the next deployment. `docs/SAFETY.md` says what each deployment runs.

## Why

An account trades through a Hyperliquid agent key it approves on its own HyperCore account. With a list of
published keys three things go wrong:

- whoever holds the list's operator key publishes keys of their own, and the next accounts get them; the
  last key published is the first handed out;
- a renewal and a re-key take the next key on the list too, so already running accounts can be reached;
- a published key cannot be taken back, and anyone can spoil one for the price of a HyperCore account
  (about 1 USDC), which the list then has to skip or purge.

## How it works

1. **An account asks.** When an account needs a key it opens a request with `KeyBinder.request(trader,
   purpose)`: a challenge once its capital is in place (`activate`), a funded stage once its capital is
   where it trades (`openFundedStage`, `openFundedFor`), a renewal (`renewAgent`), a spoiled key's
   replacement (`rekey`). One request at a time, each with its own number.
2. **The publisher signs.** The publisher, a key the owner appoints and meant to live in the signing
   enclave, signs `Binding(account, request, trader, keyHash)` under EIP-712, with this contract's
   address and the chain's id in the domain. A signature fits one request of one account on one
   deployment.
3. **Anyone carries it in.** `bind(account, key, signature)` checks the signature, that the request is
   open, that the key is new to this contract and has no HyperCore account, and binds it. The account is
   called back in the same transaction and approves the key on HyperCore: a challenge starts, a funded
   stage opens, a renewed or re-keyed account replaces its agent. The address of a key is public only from
   the block it is approved in.
4. **A renewal waits.** With a renewal delay set, the publisher's signature is committed by the key's hash
   (`commit`) and the key is shown and approved only after the delay (`reveal`). The old key works the whole
   time; the renewal window is fourteen days.

Nothing is published in advance, so nothing waits to be handed out, spoiled or drained.

## Roles

| Function | Owner | Guardian | Publisher | Account | Anyone |
|---|---|---|---|---|---|
| `setPublisher(addr, pcrHash, attestationDocHash)` | yes; neither hash zero | no | no | no | no |
| `setGuardian`, `setMaxBindsPerDay`, `setRenewDelay` (at most 7 days) | yes | no | no | no | no |
| `transferOwnership` / `acceptOwnership` | starts / the named address accepts | no | no | no | no |
| `setAccountSource` | once | no | no | no | no |
| `pause` | yes | yes | no | no | no |
| `unpause` | yes | no | no | no | no |
| `lowerMaxBindsPerDay` | yes | yes, only lower | no | no | no |
| `revoke(key, salt)` | yes | yes | no | no | no |
| `request`, `cancel`, `retire` | no | no | no | its own | no |
| `bind`, `commit`, `reveal` | with the publisher's signature | with it | signs, sends nothing | with it | with it |

The owner is meant to be a multisig; on HyperEVM mainnet Safe v1.4.1 is deployed at its canonical
addresses, and on testnet the same code sits at the same addresses. The guardian is a separate key that can
only stop. The pause stops `bind`, `commit` and `reveal`; requests, cancels and retirements go on, so an
account can always give a key up.

`setPublisher` puts the hashes of the enclave's measurements and of its attestation document in its event.
The contract does not check an attestation itself: what it would prove is that an enclave with given
measurements holds a key, and which measurements are the right ones is the owner's decision either way.
The document is published beside the event, and the build is meant to be reproducible, so anyone can
compare.

## Revoking a key

`revoke(key, salt)` retires a bound key, voids a renewal committed for its account, and calls the account
back: the account cuts the key under its agent's name as a stop does, the salt picking the keyless address,
and opens a new request for its trader. Trading on that account stops until a new key is bound; stops and
takes already on Hyperliquid stay, and the rules can still be enforced by anyone. The guardian can stop
every binding and cut every key, and take nothing.

## If the publisher is silent

- A challenge that asked for its key and did not get it within its start window may be aborted by anyone:
  the price and the platform's fee go back to the buyer. The pool holds the fee until the challenge
  starts.
- A funded stage waits as `PassedAwaitingKey`; once its key was asked for and has not come for seven days,
  or, on a builder dex, once the capital sent there has not shown, anyone may release the pool, and
  capital sent to a builder dex comes back to spot.
- A renewal that is not answered leaves the old key until its term ends.
- A stop withdraws a request still waiting, so a key never lands on a stopped account.

## Who carries the signature in

The operator's keeper (`ops/keeper.py`, `ops/multi_keeper.py`) reads the open request of every account it
serves and answers it through `ops/key_relay.py`: a fresh key, the publisher's signature, `bind`; for a
renewal while the delay is on, `commit`, and `reveal` once the delay has passed. Before it signs it computes
the digest itself and compares it with the binder's own `digest(...)`, and it signs nothing while the binder
is paused or names another publisher. On testnet the publisher is a key in a file, which also makes the
agent key and writes it into the gateway's key directory before the binding is sent; the gateway reads that
directory again when it is asked for a key it does not hold. On mainnet a publisher in a file is refused: the
publisher is the enclave. A keeper given no publisher answers nothing and logs each request it sees
(`key_asked`), and any request older than ten minutes (`key_request_old`). Accounts of the deployments
before the binder are served as they were.

## What the contract does not cover

Who may sign an order with a bound key is decided where the key lives. For the guarantees above to mean
anything, the enclave that holds the keys has to check, before it signs, the trader's own signature on the
request against the trader sealed with the key, the request's expiry and nonce, and the platform's limits
on asset, size and leverage; without a trader's signature it should sign only the gateway's protective
orders, which reduce a position. The gateway's host, which today holds the demo's keys, would then hold
none.

## The daily limit

`maxBindsPerDay` caps bindings and commitments per UTC day. It is a fuse, not the defence: it bounds what a
stolen publisher key can do before the guardian pauses. The guardian or the owner can lower it; only the
owner raises it.
