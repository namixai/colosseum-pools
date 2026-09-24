# Spike: the questions to answer before the pool contracts

The pool design leans on a handful of HyperCore behaviours that the docs either don't state
or state only for the API, not for contracts calling CoreWriter. We check them here before
writing the real contracts. Started 16 September 2026.

Two tools answer different halves:

- **The hyper-evm-lib simulator** (`test/spike/SpikeAccount.t.sol`, library pinned at
  `4eb7ab0`). It executes CoreWriter actions 1–7 and 13. Actions 9 (add API wallet), 10 and
  11 (cancels) and 12 (builder fee) are ignored without a revert, so for those a green test
  only proves that our contract sends the right bytes. Transfers run on the library's own
  model, including its 1 USDC activation fee.
- **Hyperliquid testnet** (`spike/run.py`, chain 998). This is where effects are observed.
  Every run appends to `spike/results/<date>.jsonl`.

## The questions and where each one stands

The live run was on 17 September, 07:38–07:53 UTC, with follow-ups on the bridge from 09:25
to 09:49 UTC. Every observation is in `spike/results/2026-09-17.jsonl`. The contract
addresses and every burned agent key are in `spike/results/state-testnet.json`.

| # | question | before the live run | source | live check, 17 Sep |
|---|---|---|---|---|
| 1 | Does replacing the API wallet through CoreWriter action 9 really cut off the old key? What happens to orders the old key left resting? | The docs say an `ApproveAgent` with the same name, or a new unnamed one, deregisters the previous agent. They don't say it for action 9, and they say nothing about resting orders | docs; simulator can't model it | **Yes.** Contract A replaced its agent twice. Each time the old key's next order got "User or API Wallet … does not exist". Its resting orders stayed open. A fresh address with no account was accepted as the replacement. `run.py q1` |
| 2 | Can one agent address serve two unrelated accounts? | The docs only cover a master and its sub-accounts. `userRole` for an agent returns a single user, which hints at no | docs | **No.** Contract B's approval of A's agent changed nothing and raised no error. The same approval through the API from an EOA got "Extra agent already used." The key's orders kept landing on A. `run.py q2` |
| 3 | Can USDC go pool → challenge → pool through CoreWriter, and what does a new contract account cost to create? | Works in the simulator, under its own fee model. Circle's `CoreDepositWallet` on testnet charges 0 for a new account (`newCoreAccountFee() = 0`, read 16 Sep) | simulator; RPC | **Yes, on HyperCore.** A → B, B's spot → perp → spot, B → A all went through. Sending to an address with no account costs the sender 1 USDC on top, from an EOA and from a contract alike. `run.py fund`, `run.py q3` |
| 4 | Which call gives account equity? | `accountMarginSummary(perpDexIndex, user)`, precompile `0x…080F`: account value, margin used, notional, raw USD | library; simulator | **Confirmed.** The precompile read directly and through a contract equals `clearinghouseState`, in millionths of a dollar. `run.py q4` |
| 5 | What does the trader pay with? | Testnet USDC as an ERC-20 on HyperEVM (`0x2B3370eE501B4a559b57D449569354196457D8Ab`, 6 decimals), pulled with `transferFrom`, so the payment is tied to `msg.sender`. A transfer made on HyperCore can't be attributed by a contract at all | RPC; simulator | **Paying works; the bridge to HyperCore mostly doesn't.** `pay()` recorded the payer, and HyperCore → HyperEVM (`sendAsset`) worked. Of 15 bridge deposits back to HyperCore, one arrived. See "The bridge" below. `run.py q5`, `q5b`–`q5f` |
| 6 | Can a contract cancel orders? | Yes: action 10 by order id, action 11 by client order id. No precompile lists open orders, so a contract can't find the ids by itself | docs; library | **Yes.** Contract A cancelled one order by oid and one by cloid. `run.py q1` |
| 7 | Which margin mode does a new contract account get, and who can change it? (added 16 Sep) | Accounts that never chose report `"default"`; the founder's testnet account reports `"unifiedAccount"`, where spot USDC backs perp positions. The docs say both the account and its agents can switch modes. The Signer enclave build signs only `order` and `cancel`, so its keys couldn't (since 17 Sep the demo signs in the pool gateway instead) | info API; docs; enclave source | **`default`.** Action 16 set `disabled` and it held. A plain agent key switched B to `unifiedAccount`, and the contract switched it back. `run.py q7` |
| 8 | Does HyperCore take an address that already has an account as an agent? (added 17 Sep) | not asked before | — | **No.** Action 9 naming `0x…dEaD`, which has an account on testnet, changed nothing and raised no error. `run.py q8` |

## The bridge from HyperEVM to HyperCore

On testnet HyperCore links USDC to Circle's `CoreDepositWallet` (`0x0B80…C206`), not to the
ERC-20 itself. The wallet has two paths. For HyperCore spot it takes the USDC and emits
`Transfer(recipient → 0x2000…)`, and HyperCore is meant to credit the recipient. For an
enabled perp dex (on testnet dex 0 is enabled, dex 1 isn't) it emits `Transfer(wallet → 0x2000…)`, which credits
the wallet's own spot, then forwards to the recipient with a CoreWriter `sendAsset`.

We sent 15 deposits between 07:51 and 09:49 UTC. None reverted, and each one took the USDC
off HyperEVM. One arrived: the deployer, an EOA, depositing to itself on spot, credited
within seconds. We watched each of the others for a minute (the first one for 45 seconds,
and again about 90 minutes later):

- contract A bridging its own USDC, which is what `Pool.deposit` does;
- the deployer depositing on spot for contract A; for a fresh contract C before it had an
  account and then in `default`, `disabled` and `unifiedAccount` modes; and for the trader
  wallet, an EOA, before and after it had an account;
- every forward to dex 0 we sent: to A, C, a fresh contract D and the trader wallet, with 1
  and with 3 USDC. Circle's wallet credited itself each time, and the forward never arrived.

Forwards made by others did arrive: Circle's wallet ledger shows one at 07:52 and one at
08:03 UTC, each followed by a `send` to an EOA. The 08:03 transaction was sent by a third
address through another contract, so that recipient hadn't sent it. Forwarding works; it
failed only for the calls we made. The deployer wasn't credited in place of the recipients
either. This cost 24 test USDC.

Circle's wallet is credited for its own transfers. Apart from that, one reading fits every
case we saw: a spot deposit arrives only when the credited address sent the transaction. We
haven't found it written down, and we haven't looked at mainnet.

What it means for the pools: a contract can't bridge to itself, so `Pool.deposit` would take
an investor's USDC and credit nothing. It has to go before anyone deposits; that change is
queued with the other contract fixes. Capital reaches a pool the way it reached A, C and D
here, by a spot transfer on HyperCore from the investor's own account. The testnet faucet pays
out on HyperCore anyway. A trader still pays on HyperEVM, where `transferFrom` ties the payment
to the payer, and the pool keeps that USDC there.

## The two fallbacks

The scope decision named two fallbacks, and neither is needed.

- If replacing the agent hadn't cut the old key, a stop would have closed positions and moved
  the capital out, leaving the key to sign for an empty account. Question 1 passed.
- If USDC couldn't move pool → challenge → pool, the investor would have funded each
  challenge account directly. Question 3 passed on HyperCore.

Question 5 adds a constraint instead: capital enters a pool on HyperCore, not through the
bridge.

## What the design has to absorb

- **CoreWriter is fire-and-forget.** The EVM call succeeds, HyperCore acts a few seconds
  later, and a failure there does not revert anything. An account that doesn't exist on
  HyperCore yet has its actions dropped without a trace (simulator:
  `test_actionFromInactiveAccount_isSilentNoOp`). So every step that matters needs a later,
  permissionless call that re-reads the state through a precompile. The bridge above fails
  the same quiet way.
- **A stop can't discover open orders, and an order nobody names stays.** On 17 September
  a resting order that nobody named was still open after the free balance left the perp
  account, and it kept its margin: 0.55 USDC on an 11 USDC order. The script moved out only
  what was withdrawable, that went through, and Hyperliquid didn't cancel the order. The
  script cancelled it afterwards. Until someone names such an order, `settle()` can't bring
  that margin back.
- **A "dead" agent address has to be fresh.** `0x…dEaD` already exists on testnet HyperCore,
  and question 8 showed that action 9 silently ignores an address with an account. The stop
  uses an address derived for the purpose, with no known key and no account. If someone
  funded every candidate in advance, the replacement would do nothing and the old key would
  keep trading until someone calls `recut`, which tries other addresses. The candidates mix
  in the previous block hash, so funding them ahead is hard to arrange. It stays on the list
  of known risks.
- **An agent key is used for one account, once.** Hyperliquid warns that actions signed by a
  deregistered agent can be replayed after its nonce set is pruned. So a key is never
  approved again after it has been replaced, and when a trader passes the challenge, the pool
  approves a new key instead of moving the old one.
- **Spare pool capital is only safe on spot if the account keeps separate balances.** In
  unified mode spot USDC is perp margin. The contracts set mode 1 on every account they
  create (action 16), and it held on 17 September. A plain agent key can switch it back. The
  demo gateway signs only orders and cancels, but whoever holds its key files could sign the
  switch.
- **Prices sent through action 1 need rounding on our side.** An IOC close at an unrounded
  price did nothing and raised nothing; the same close at a rounded price filled. The
  contracts round.
- **A new account costs its first sender 1 USDC.** When a pool sends a challenge its capital,
  or pays a trader who has no account yet, it needs that dollar on top. The pool's capital
  check doesn't leave room for it yet; that fix is queued too.

## The shared pool (24 September)

Two things the shared pool's design leans on, checked before its code, with
`spike/shared_pool.py`. Every observation is in `spike/results/2026-09-24.jsonl`; the raw reads of
the transfer check are next to it, in `spike/results/atomic-*.jsonl`.

| # | question | how | answer, 24 Sep |
|---|---|---|---|
| 9 | Can a precompile read show a HyperCore transfer half done: the sender debited and the recipient not yet credited, or both at once? | One call reads both accounts (`SharedPoolProbe.pair`), about 2.5 times a second, while USDC moves between them; every read must add up to the same total | **Not in 264 reads.** Four transfers of 0.2 USDC, sent through the API from an EOA to an existing contract account: every read adds up, and each transfer shows on both sides in the same read, 0.93–1.31 s after it was sent. The same check for a transfer a contract sends through CoreWriter (`atomic --via corewriter`) is still to run. |
| 10 | What does reading one seat cost at a settlement point? | `SharedPoolProbe.seat` through `eth_call` with a state override, against the live demo and rehearsal pools, so nothing is deployed or spent | **28 265 gas for an idle seat, 74 336–74 759 with a running challenge.** One `spotBalance` read costs 7 431, `accountMarginSummary` 8 590, `coreUserExists` 3 897, a `violation()` call 22 357–23 380. |

Two things learned about the tools on the way:

- `eth_call` takes a state override on the public RPC and on Chainlink's, so a read-only contract
  can run against live accounts without being deployed.
- A precompile read through `eth_call` answers with HyperCore's state as it is now, whatever block
  is asked for. The demo pool's balance read at the block the demo was deployed in, before the pool
  existed, came back as the current one. HyperCore's history can't be read this way.

## Running it

Testnet only. `hlspike/common.py` refuses any RPC whose chain id isn't 998 and any API URL
that isn't Hyperliquid's testnet. Keys are read from outside the repository
(`COLOSSEUM_KEY_DIR`, one `<name>.key` and `<name>.addr` per wallet) and never printed.

```bash
uv venv spike/.venv --python 3.14
uv pip install --python spike/.venv/bin/python -r spike/requirements.txt
forge build
cd spike
.venv/bin/python probe_readonly.py      # sends nothing
.venv/bin/python run.py status          # sends nothing
.venv/bin/python run.py gas             # needs testnet USDC on the deployer's HyperCore account
.venv/bin/python run.py deploy
.venv/bin/python run.py fund --usdc 40
.venv/bin/python run.py q7
.venv/bin/python run.py q3
.venv/bin/python run.py q1
.venv/bin/python run.py q4
.venv/bin/python run.py q2
.venv/bin/python run.py q5
.venv/bin/python run.py q8
.venv/bin/python run.py q5b --usdc 1    # the bridge: three paths
.venv/bin/python run.py q5c             # the bridge: a fresh contract, each margin mode
.venv/bin/python run.py q5d             # the bridge: an EOA that sends nothing
.venv/bin/python run.py q5e             # the forward to dex 0
.venv/bin/python run.py q5f --usdc 3    # the same forward, a larger amount
.venv/bin/python shared_pool.py gas     # sends nothing
.venv/bin/python shared_pool.py atomic --key <name> --to <existing account> --usdc 0.2 --times 4
```

The testnet faucet only pays addresses that have deposited on mainnet, and the wallets here
are new, so the first USDC has to come from a person's testnet account.
