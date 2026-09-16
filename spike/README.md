# Spike: six questions before the pool contracts

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

| # | question | answer so far | source | live check |
|---|---|---|---|---|
| 1 | Does replacing the API wallet through CoreWriter action 9 really cut off the old key? What happens to orders the old key left resting? | The docs say an `ApproveAgent` with the same name, or a new unnamed one, deregisters the previous agent. They don't say it for action 9, and they say nothing about resting orders | docs; simulator can't model it | `run.py q1` |
| 2 | Can one agent address serve two unrelated accounts? | The docs only cover a master and its sub-accounts. `userRole` for an agent returns a single user, which hints at no | docs | `run.py q2` |
| 3 | Can USDC go pool → challenge → pool through CoreWriter, and what does a new contract account cost to create? | Works in the simulator, under its own fee model. Circle's `CoreDepositWallet` on testnet charges 0 for a new account (`newCoreAccountFee() = 0`, read 16 Sep) | simulator; RPC | `run.py fund`, `run.py q3` |
| 4 | Which call gives account equity? | `accountMarginSummary(perpDexIndex, user)`, precompile `0x…080F`: account value, margin used, notional, raw USD | library; simulator | `run.py q4` |
| 5 | What does the trader pay with? | Testnet USDC as an ERC-20 on HyperEVM (`0x2B3370eE501B4a559b57D449569354196457D8Ab`, 6 decimals), pulled with `transferFrom`, so the payment is tied to `msg.sender`. A transfer made on HyperCore can't be attributed by a contract at all | RPC; simulator | `run.py q5` |
| 6 | Can a contract cancel orders? | Yes: action 10 by order id, action 11 by client order id. No precompile lists open orders, so a contract can't find the ids by itself | docs; library | `run.py q1` |

Nothing in the "live check" column has run yet. The testnet addresses have no funds;
see "Running it" below.

## What the design has to absorb already

These follow from facts we have checked, not from the pending runs.

- **CoreWriter is fire-and-forget.** The EVM call succeeds, HyperCore acts a few seconds
  later, and a failure there does not revert anything. An account that doesn't exist on
  HyperCore yet has its actions dropped without a trace (simulator:
  `test_actionFromInactiveAccount_isSilentNoOp`). So every step that matters needs a later,
  permissionless call that re-reads the state through a precompile.
- **A stop can't discover open orders.** It can cancel what a caller names, close positions
  with reduce-only IOC orders, and move the free balance out. What happens to a resting
  order nobody named once the margin has left is an open question: `run.py q1` leaves one
  behind on purpose and records whether Hyperliquid cancels it, keeps it, or refuses the
  transfer.
- **A "dead" agent address has to be fresh.** `0x…dEaD` already exists on testnet HyperCore
  (`coreUserExists` returns true), so it can't stand for "nobody". The stop uses an address
  derived for the purpose, with no known key and no HyperCore account.
- **An agent key is used for one account, once.** Hyperliquid warns that actions signed by a
  deregistered agent can be replayed after its nonce set is pruned. So a key is never
  approved again after it has been replaced, and when a trader passes the challenge, the pool
  approves a new key instead of moving the old one.
- **Prices sent through action 1 may need rounding on our side.** `run.py q1` sends a
  closing order with an unrounded price first and a rounded one second, and records which of
  them closes the position.

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
.venv/bin/python run.py q1
.venv/bin/python run.py q3
.venv/bin/python run.py q4
.venv/bin/python run.py q2
.venv/bin/python run.py q5
```

The testnet faucet only pays addresses that have deposited on mainnet, and the wallets here
are new, so the first USDC has to come from a person's testnet account.
