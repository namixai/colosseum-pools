# Factory v3: the freeze

Frozen by the CTO on 23 October 2026, about 09:00 UTC:

**`23772e2dbf9f4913dc265efa68768030ab7d4d2a`** (pull request #152, branch
`kolizey/deep-audit-fixes-2026-10-16`).

The self-audit, the external audits and the testnet demo 3 start from this commit.
Until the self-audit reports, the contracts (`src/`) do not change. The keeper, the gateway and the
other scripts may, each change named in its own commit.

## What it was checked with

- CI on the commit: green, on `pull_request` and on `push` (scrub, identity, gateway, app,
  contracts).
- Tests:
  - forge 460 of 460;
  - ops 392, gateway 274, agents 97, app 210;
  - the scrub, identity, findings, ABI and mutation-list checks.
- Mutations (`scripts/mutations.py`): 2,090. `--check` finds each in its place, and every mutant of
  a Python file parses.
  - The full run on `275259d4` caught 2,089 of 2,089, each by a test it names.
  - Between that commit and the freeze only one file under mutation changed, `ops/terms.py` (D13,
    template 6). Its 19 mutations, the new K367 among them, were run on the freeze: 19 of 19.

## Sizes

| Contract | Bytes | Under the limit |
|---|---:|---:|
| Pool | 24,386 | 190 |
| MultiPool | 24,356 | 220 |
| PoolShares | 23,932 | 644 |
| ChallengeAccount | 19,859 | 4,717 |
| PaperExam | 13,191 | 11,385 |
| PoolFactory | 10,105 | 14,471 |
| AccountOps | 5,168 | 19,408 |

## A stop's gas

A small HyperEVM block holds 2 million gas. The worst stop needs about 3.4 million: sixteen assets in
the rules, each capped and open, sixteen extra positions, thirty-two cancels.
- `spike/run.py stopgas`, a read-only eth_call on testnet: 3,230,951 gas on xyz, 3,187,668 on the
  main dex.
- `test/spike/StopGas.t.sol`, the account's own work on the simulator: 179,792.
- The testnet node's eth_estimateGas for the same call: 3,286,733 (22 October 2026, 18:19 UTC). A
  call over a small block is estimated, not refused.
- The rules' own positions alone come to about 1.43 million.

The keeper fits each stop and each settlement step to a small block (`keeper.fit`): extra positions
first, then cancels, the rest in the next step. Big blocks are the fallback docs/DESIGN.md describes,
not switched on.

## What is in v3

- The factory: its token as a parameter (`DemoUsdc` on testnet), shares contracts created from one
  code (`createPoolShares`), the list of pools that sold a challenge (K-1), the registries' journal
  (A5-3).
  - `ops/deploy_v3.py` deploys it in one command; on mainnet only as a dry run.
- The agent: approved by name for 179 days, renewed with a fresh key from the registry. A challenge's
  spoiled key is rekeyed (A5-1, A5-2).
- Markets on a builder-deployed dex (HIP-3), one dex an account:
  - rules hold order ids, and the precompiles read the id less 100000;
  - the factory checks the market's name at listing (`NotThatMarket`), and refuses ids of dex 0
    (`NotAnOrderId`, В4-4);
  - closing orders, the stop's keyless address and the agent's approval are in the linked library
    `AccountOps`;
  - a start on such a dex takes two calls. The capital is sent, the account starts once a later
    block shows exactly it there, a lost send is made again, and a surplus goes back to spot first
    (В4-2, В5-1).
- Each asset's own leverage cap (`Rules.assetLeverageX100`), checked by the account and the paper
  exam. The account's cap is 10x in the run's plan and the scripts' defaults.
- `ops/list_assets.py`, on testnet only: a builder dex the run trusts, on USDC collateral, the
  factory asked first.
- The terms: each asset's cap, and D13 for a plan on a builder dex (the dex owner's powers, the
  gateway's weekend); template 6.
- The keeper:
  - a start waiting for its dex logged as waiting;
  - stops and settlement steps fitted to a small block;
  - a MultiPool's plan read by its version;
  - its own node checked at start (M-1).
- Merged from `main`: the keeper and the trader's desk on every dex, and the gateway's leverage
  checks with the app's v3 `createPool`.

## What is not (v3.1)

- Mainnet for `ops/list_assets.py` and for the keeper.
- The precompiles' and CoreWriter's addresses as parameters.
- Markets of more than one dex on one account.
- An open function that closes HIP-3 positions in a window: the gateway does it.
- Big blocks for the keeper: written down as the fallback, not switched on.
