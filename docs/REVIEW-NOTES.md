# Review notes

An internal review of this repository, done between 25 September and 3 October 2026 by a reviewer who did
not write the code. It is not an external audit. Each finding was reproduced by a test when it was
reported. Most were fixed on a branch as they came in, and the fixes were checked again on main at
commit `9a79ae69`. Nothing here is deployed unless it says so.

## Scope

Reviewed at commit `2e1ffcd` (main, 25 September 2026):

- the contracts in `src/`: `Pool`, `PoolFactory`, `ChallengeAccount`, `KeyRegistry`,
  `RuledAccount`, `lib/CoreOps`, `shared/SharedPool` and `shared/DepositTicket`;
- how they use CoreWriter and the HyperCore precompiles, and what `spike/` measured on the
  simulator and on testnet;
- the breach and settlement flows, including two callers racing and repeated calls in one block;
- the pool gateway in `gateway/`: `demo` mode in full, and the client side of `signer` mode;
- the keeper in `ops/keeper.py` and the host configuration in `ops/host/`.

After that, the fix branch was read again at every commit the developer sent, and main at `c45a32e6`,
`e4287926` and `9a79ae69`. Code that reached main after the pinned commit was not reviewed as code.
The gateway's stop-and-take protection (`gateway/protect.py`, and the keeper's check that goes with
it) was only measured for what it costs in Hyperliquid's request budget (A-14). The stress package in
`stress/` and the deployment rehearsal in `ops/deploy_testnet.py` were not looked at.

Not reviewed: Usenami Signer and its enclave, the economics of the rules, and the app in `app/`.

## Method

Each finding rests on a line at the commit where it was found, which for most of them is the pinned
one. A finding needed three things: that line, who does what and who loses, and a test that
reproduces it. The tests run on the
hyper-evm-lib simulator with Foundry, or against fakes in Python.

`docs/AUDIT-FINDINGS.md` gives each finding a row that names the tests pinning its fix, and
`scripts/findings-check.py` fails if a named test goes missing. Each of those tests was also checked
the other way round, by taking the fix out and watching the test fail, with one exception: since the
fix for A-04, a second line covers the case A-11 was about, so taking out either one alone leaves the
A-11 test passing. The review's own reproduction tests went to the developer with the findings and
are not in the repository.

The live testnet was only read: chain state, receipts and Hyperliquid's public info API. No
transaction was sent, and no load was put on the running gateway.

## Findings

Status is as of commit `9a79ae69` on main.

| ID | Severity | Finding | Status |
|---|---|---|---|
| A-01 | High | Anyone who sends a tiny amount to an account before each settlement step keeps a stopped challenge from reaching Settled, and a closing funded stage from reaching Idle, for as long as they keep sending. Meanwhile the pool owner can't withdraw (`withdrawOnCore` needs Idle), and a shared pool's settlement points stop. | Fixed. A stranger's transfers can now hold a settlement only while the account's own five-minute waits run: for its return to land (`RETURN_WAIT`), for money still crossing when the share is short (A-12), and for a payout to land (`PAYOUT_WAIT`). Then it finishes, and whatever arrives later goes to the pool through `sweep()`, which anyone may call. |
| A-02 | High | Passing took the funded stage's key from the free list at that moment. Anyone could spoil the free keys for 1 USDC each, which blocked the pass (the trader then expired without a share) and every new sale on the factory. | Fixed. The pass and its challenge share are now recorded whatever the key registry holds, and the pool keeps its capital locked for the trader while it waits. The funded stage opens by a separate call that anyone may make once a live key exists. If none comes within seven days, anyone may release the pool (`abandonFundedStage`), but only when no key can be had at that moment: the reserved key is spoiled and the registry lists nothing free. The pass and the share stay with the trader. A sale now takes two keys, one of them held for the pass. One thing rests on reasoning, not a test: a key is checked at the start of a block and made the agent after it, so a transfer landing in between could still start a challenge (`activate`) or a funded stage (`openFundedStage`) on a key HyperCore won't accept. |
| A-03 | High | The public gateway and the keeper read the chain through one node with a per-IP limit, from one host. Refusable orders sent to the gateway, five reads each before the refusal, could use up the keeper's budget and delay stops. | Fixed: they now read different nodes, and the gateway's limits were cut to fit its node. Applied on the running host on 25 September 2026; the operator's checks are in `docs/HOSTING.md`. The two still share Hyperliquid's own request budget, which is A-14. |
| A-04 | Medium | A trader's share is paid once nothing is withdrawable, on a passed challenge and at the end of a funded stage alike. Resting orders keep margin out of `withdrawable`, so the share can be paid short from whatever reached spot. A pool that is all on perp after the pass has little on spot. | Fixed, on both sides. The share waits while a resting order holds margin. In the step that releases the margin, it waits until spot covers the share or nothing more is on its way. The second half of this fix opened A-12, which is fixed as well. |
| A-05 | Medium | USDC sent to a challenge's perp balance counts toward its target and its profit share, so a pass can be bought for the target less the trader's share. | Documented as a limit in `docs/DESIGN.md`. |
| A-06 | Medium | The shared pool's operator can add seats, with rules of its choosing, after holders have deposited. The documents said seats are published before anyone deposits. | Fixed: the operator seals the book of seats, no ticket can open before the seal, and no seat can be added after it. An intermediate version of this fix opened A-13. |
| A-07 | Low | A seventeenth withdrawal request is refused, not queued as the documents said. | Fixed in the documents and the contract comment, which now say it is refused. |
| A-08 | Low | `buyChallenge` checks the pool's balance at the start of the block, so a withdrawal earlier in the same block makes the sale fail. The buyer gets the price back after an hour and loses the platform fee. In a shared pool that withdrawal is `releaseSeat`: anyone may call it while the withdrawal queue is short of money, and the shared pool's keeper calls it on its own. | Fixed: a sale is refused in the block of any `withdrawOnCore` call, and `releaseSeat` makes one. |
| A-09 | Info | `docs/DESIGN.md` described zero-price pools, which the factory refuses, and called the platform fee the only brake on using up keys. | Fixed: `docs/DESIGN.md` is corrected in four places. |
| A-10 | Info | The hosting notes listed `graduate` among the keeper's calls. The keeper never makes it. | Fixed (pull request #68). |
| A-11 | High | Found in an intermediate fix for A-01: two closing steps in one block paid a funded trader's share from the balance as it stood before the proceeds arrived, nearly nothing on a pool that is all on perp. | Fixed (`9f6bd6e`) before it was merged or deployed. |
| A-12 | Low | Found in the fix for A-04. Say a passed challenge ends up holding less than the trader's share. Then a stranger who puts a unit on its perp balance before every settlement step keeps it from settling for as long as they keep doing it: the share waits for money on its way, and there always is some. Meanwhile the pool can't sell another challenge. It takes a loss after the pass, on a position opened by an order that was still open when the trader passed. Before the A-04 fix, the two five-minute waits bounded it. | Fixed: that wait now has a five-minute clock of its own, on both sides, which starts over while margin is held so that a release in pieces can't run it out. With a unit of dust every minute, a short challenge settled in about twenty minutes and paid the trader what the account held. Tests in the repository pin both sides. |
| A-13 | Medium | Found in the second fix for A-06. `addSeat` needs the pool started, and it is refused once any deposit ticket is open. Anyone may open a ticket as soon as the pool starts, without paying into it, and an open ticket leaves the list only when a settlement point takes in a deposit from it. So a stranger who opens one between the operator's `start()` and its seats fixes the pool at the seats it has, possibly none, for good. The platform's starting shares, at least 5% of the seat plan, never leave the pool. | Fixed: the operator now seals the book of seats, and tickets open only after the seal (see A-06). |
| A-14 | Medium | The keeper and the gateway run on one host and read Hyperliquid's API from one IP, and Hyperliquid limits an IP to a request weight of 1200 a minute. A-03 separated their chain nodes, not this. The keeper spends 20 a pass plus 44 for each active account, every 30 seconds. The gateway's stop-and-take spends 46 on each order that may open a position, and sweeps every 10 seconds at 20 plus 22 for each account with a position. With two trading accounts that is 600 a minute before any order, and 1290 with orders at the rate nginx lets through; from five it is over the limit with no orders at all. Past the limit, reads fail. The keeper skips the pool whose read failed and walks its pools in the same order every pass, so the same pools at the end go unchecked each time, and the gateway refuses orders and stops moving stops. The stops already on Hyperliquid still hold the drawdown line. What goes unwatched is the daily line once it moves, leverage, and the keeper's other steps for those pools. | Partly fixed. The keeper asks the contract first, so drawdown, daily loss and leverage are checked and a breach is sent even when Hyperliquid refuses every read. It reads each account once a pass, 22 instead of 44, and starts each pass where the last one broke off. The gateway sweeps every 15 seconds instead of 10. The two still share one IP: with orders at the rate nginx lets through, the host stays under the limit with two trading accounts, and with eight when no orders come. |

## What this review could not check

- Whether a usdSend from a third party lands on a contract's perp balance. A-05, part of A-01 and
  A-12 rest on the repository's own note that it does (`spike/run.py`).
- How long CoreWriter orders wait before they execute. One sample: the closing order of the stop
  in block 65206003 (block time 10:16:59 UTC, 25 September 2026) filled at 10:16:59.363.
- How Hyperliquid counts its per-IP budget in practice, a fixed minute or a rolling one, and the
  host's real load. A-14 is arithmetic: Hyperliquid's published weights times the calls the code
  makes, which the review counted in tests.
- Anything under load on the running host, `signer` mode end to end, and mainnet.

## Measured along the way

- A key that is already an account's agent stays its agent after it receives USDC and so gains a
  HyperCore account. The developer checked this on testnet on 25 September 2026, and the key's role
  still reads `agent` on the public info API. So a live trader can't be switched off for 1 USDC;
  only a key not yet approved as an agent can be spoiled.
- One CoreWriter action sent from a contract costs about 26,700 gas. This was measured with a
  read-only `eth_call` and a code override on the public testnet RPC: 29,261 gas for one action,
  856,093 for thirty-two. A live stop cost 247,768 gas, and with thirty-two named cancels it would
  come to about 1.25 million, under the 2 million a small block holds.
- The demo deployment in use on 25 September 2026 matches a clean build of its deployment commit
  `00c52622`: `KeyRegistry` and the `Pool` and `ChallengeAccount` implementations byte for byte,
  `PoolFactory` byte for byte outside its six immutable slots, which hold the expected registry,
  implementations and USDC addresses. The two demo pools are EIP-1167 clones of that `Pool`. The
  contracts' source at that commit is the reviewed one: `git diff` of `src/` between it and
  `2e1ffcd` touches only `shared/` and `spike/`.
- The second demo deployment (factory `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222`, 1 October 2026)
  matches a clean build of its deployment commit `e4287926`: `KeyRegistry` and both implementations
  byte for byte, `PoolFactory` byte for byte outside its immutable slots. The pool and the challenge
  of its first run are EIP-1167 clones of those implementations. That commit is the one whose fixes
  this review checked in full.
