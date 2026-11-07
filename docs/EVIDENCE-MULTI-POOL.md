# What the pool of many traders has done on chain

Status: 16 October 2026, HyperEVM testnet (chain 998) and Hyperliquid testnet, mock USDC. The pool
of many traders (`MultiPool`: an exam traded on paper, a funded account of the same size on the
pool's money after a pass, a term) is not in `main` yet. Its contracts are on the branches of the
pull requests that build it, from #137 on, and the run below used commit `c4d68b51` of that line.
Nobody outside the team has reviewed it.

## The first run, 15 October 2026

Deployed by hand at 09:14 UTC, from `c4d68b51`, and run one step at a time with that commit's
`ops/multi_run.py`. The record is [`deployments/testnet-multi-1.json`](../deployments/testnet-multi-1.json):
the six contracts, the ten transactions that made and set them up, and the plan as it was given.

| | |
|---|---|
| Pool | `0x56155A9a4bEf9f6C19064f62D371258b9F84A16a` |
| Its factory | `0x1200D7bd2F6303429b848941Ff87BB51D52c59F8` |
| Deployed in block | 66,957,866 |
| Menu | 5, 10 and 25 USDC, for 0.05, 0.1 and 0.25 |
| Exam | one step to a 0.1 % target, within an hour; a fill at the mark two blocks after the order |
| Term | six hours, with half an hour's notice; this deployment's bounds were one hour to one day |
| Funded stage | the trader stops it; 80 % of what it makes is the trader's; one day idle at most |

The plan is a plan for checking the mechanism by hand, not a product: its sizes, its target and its
term are small so that one afternoon could take every path once.

## Nine measurements the simulator could not make

| # | What was measured | Result |
|---|---|---|
| 1 | A transfer onto a contract's own HyperCore account (`fund`) | in its balance after 0 s |
| 2 | The mark a contract reads (the `markPx` precompile) against the exchange's | 0 bps apart: 81,164 and 81,164 (BTC) |
| 3 | A transfer between two contracts' accounts (`claim`, the pool to a seat) | 1 s |
| 4 | `prepareAccount` on a seat the contract created | went through |
| 5 | `openFundedFor` | stage Funded, started on 5 USDC, with one of the twelve agent keys the deployment published |
| 6 | Gas | the table below |
| 7 | `Locked` before the end, `Ending` in the last stretch | both refused |
| 8 | `extend` with 10 USDC out, inside the notice window | the end moved by an hour and the 10 USDC reached the owner; before the window, `Locked` (the audit's M-2 holds) |
| 9 | `endSeat` at the end, then `settleFunded` in two steps, then `finish` | the seat Free; a withdrawal of everything, 89 and 0.1 USDC, went through and left the pool empty |

The gas, per call:

| Call | Gas | |
|---|---|---|
| deploying `MultiPool` | 6,722,620 | about 6.0 million on the simulator |
| `buy` | 791,278 | the first; 688,678 each one after |
| `approve` | 55,437 | |
| `place` | 61,271 | |
| `fill` | 131,598 | |
| `graduate` | 92,307 | |
| `claim` | 128,934 | |
| `open` | 395,290 | 943 thousand on the simulator |
| `extend` | 169,641 | |
| `endSeat` | 201,000 | |
| `settleFunded` | 174,886 and 111,997 | two steps |
| `finish` | 142,910 | |
| `release` | 56,288 | |
| `withdrawEvm` | 79,520 | |

## Checking it yourself

- **The deployment.** `eth_getTransactionReceipt` for each hash under `tx` in the record, on
  `https://rpc.hyperliquid-testnet.xyz/evm`: all ten have status 1, the six that create a contract
  name its address, and `MultiPool`'s used 6,722,620 gas, the number in the table. Read back on
  15 October 2026.
- **The pool now.** `value()`, `held()` and `inWork()` on the pool are 0, `closing()` is true and
  `endsAt()` is 16:14:00 UTC on 15 October; `seats()` lists three seats, each Free in
  `seatInfo(seat)`. Read back on 16 October 2026 at 05:00 UTC, block 67,030,232.
- **The run's steps.** Measurements 1 to 9 are what `ops/multi_run.py` printed as the run went: each
  step prints its transaction's hash, block and gas, and `fund`, `claim` and `price` print the
  seconds and the gap they measure. Those transactions are the pool's and its exams' and seats',
  readable from their receipts and the pool's events; the record holds only the deployment's.

## What the run found

- **A small target is a lottery by hand.** An exam's orders fill at the mark of a later block, so a
  position in profit cannot be closed at the moment it is. With a 0.1 % target the first exam
  failed; the second passed with a watcher that closed by itself once the equity was 5.008 or more.
  `multi_run` has had an `autoclose` step for this since (pull request #147).
- **A refusal read as a traceback.** `Locked`, `Ending` and `BufferNeeded` came out as Python
  tracebacks with a selector in them. They print as one line with the error's name now (#147).
- **No step to expire an exam.** `release` takes only an exam that has ended, so before the pool's
  end it refused one that had run out of time; after the end it went through. There is an `expire`
  step now (#147).
- **The value reads low for a minute after a payment out.** `value()` counts a payment as leaving
  for a minute after it, and once it has landed it is counted twice in that minute (the audit's
  Info). `status` says so now (#147).
- **An empty pool answers `BufferNeeded`.** A second withdrawal after everything was out is refused
  with `BufferNeeded`, which is right and reads like something else. `status` says the pool is empty
  now (#147).

Not in this run: free exams, the noter, a pool of several investors (`PoolShares`), and a keeper.
The calls anyone may make were made one at a time, from the command line.
