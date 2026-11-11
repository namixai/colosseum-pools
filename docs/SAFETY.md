# Safety: what the code holds, and what it does not

Testnet only: the USDC and HYPE here are Hyperliquid testnet tokens, and no external audit of this code has
been completed. This page lists the limits that the contracts, the gateway and Hyperliquid put on each party,
and the gaps between them. Each statement of what the code, the chain or Hyperliquid does points to the
line, the address or the page that makes it true (the table at the end, by its number); what is absent --
a timelock, an audit, a bounty -- has no line to point to, and is said plainly. Nothing here promises
that capital is kept, that it earns anything, or that anyone can leave at once. The lines cited are main's,
the contracts of v3.0.1, which no deployment runs yet; section 3 says which code each deployment runs, and
gives a pool owner's powers in each.

## 1. Enforced controls

- **The trading key cannot send an account's money elsewhere.** A challenge account, and a pool in its
  funded stage, trades through an agent key the account approves on Hyperliquid (S1). Hyperliquid lets an
  agent move money only inside the account itself: an agent-signed transfer must name the account as its
  destination (S2). That is Hyperliquid's permission model, not a check in these contracts. The key can
  still trade the money away, including against a counterparty it chooses.
- **The trader never holds that key.** In this demo the gateway holds the agent keys and signs with them
  (S3). The trader's wallet signs requests to the gateway.
- **A key is bound only to an account that asked for one.** In main an account asks the key binder for a
  key when it needs one: at a challenge's start, a funded stage's opening, a renewal, a spoiled key's
  replacement (S96). The binder binds a key only on the publisher's signature over that account's request,
  its trader and the key (S97), never a key that has a HyperCore account (S98), and the account approves
  it in the same transaction (S99). No list of keys waits on chain to be handed out.
- **Capital stays on the pool's account.** A sale moves only the challenge's capital to the challenge's
  new account on HyperCore (S4); the funded capital stays on the pool's own account. In main the price and
  the platform's fee are both paid into the pool contract (S5); the fee goes to its recipient when the
  challenge starts (S106), and both come back to the buyer if it never does (S6).
- **A stop and a take go on the book before an opening order.** Before the gateway submits an order that
  may open or grow a position, it places a market stop and a take on Hyperliquid as one `positionTpsl`
  group (S7, S8). If they cannot be placed, the order is not sent. The stop fires 6 basis points of the
  price before the rule's line: room for the taker fee, 4.5 basis points, and a fill 1.1 past the
  trigger, as measured on a stop on 29 September 2026 (S95). A fill more than 1.5 basis points past the
  trigger lands beyond the line (section 2).
- **The rules are on chain and do not change.** Daily loss, drawdown, leverage and the list of markets
  are written when a pool is created and copied into each challenge it sells (S9); nothing writes them
  afterwards (S10). Whether an account breaks a rule is read on chain (S11). A stop (`breach`) is
  refused unless a rule is broken (S12); it cuts the account's key (S13) and records the reason in the
  `Stopped` event (S14). The stops so far are on the site's "What happened on chain" page (S15).
- **The shared pool's seats are fixed.** Once the operator seals the book of seats, no seat can be added
  (S16). The pool the site takes deposits into is sealed (S17).
- **The platform's starting shares in the shared pool never leave it** (S18).
- **A pool's owner takes capital out only while no trader holds it.** `withdrawOnCore` works only while
  the pool is idle (S19).

## 2. Trading and exits

- **No rule holds through a price gap.** The rules are checked against the account's equity. A price that
  jumps past the stop triggers it as a market order, which fills where the market is. The 6 basis points
  the stop leaves before the line pay for the fee and a fill 1.1 basis points past the trigger; the two
  stops the exchange fired on testnet on 12 and 14 October 2026 filled 1.6 to 4.5 past theirs, and a gap
  goes further. Such a fill lands beyond the line, and only within 10% of the trigger, where the order's
  own price is set (S66): a gap wider than that can leave the position partly or wholly open. What a challenge can lose is bounded by its account's capital, the only money the account holds
  (S4), not by its rule. A funded stage trades the funded capital on the pool's own account.
- **Hyperliquid's liquidation and auto-deleveraging apply to every account**, whatever a pool's rules
  say (S20, S21).
- **There are windows.** The gateway's sweep, which puts back a missing stop and moves the lines, comes to
  every account each 30 seconds (S22), and each 15 seconds to an account where a position can stand without a
  stop: an order resting that may open, a position with no stop, positions or stops that changed since the
  last pass, an action just sent (S65). Each wait starts when the sweep before it has finished. A stop refused
  at every pass for 30 seconds in a row, by Hyperliquid or by the signer, is followed by a close at the market
  (S23); an answer not known to be a refusal starts the count again (S67). The close is a reduce-only order
  priced 10% through the mark that fills what it can at once and cancels the rest (S68): it too can be
  refused, or fill only in part. The keeper checks the rules every 30 seconds, plus however long a pass takes
  (S24). Between those, only what already stands on the book acts: the stop, and the take.
- **A halted market is not recognised.** A builder-deployed market (HIP-3) halted by its deployer takes
  every order off, the stop included. The gateway has no notion of a halt: its sweep sees a position
  without a stop and tries to place one at every pass (S25).
- **Leaving the shared pool takes time.** A withdrawal can be asked for from 10 minutes after the holder's
  latest deposit (S26, S27) and cannot be taken back. It is paid at a settlement point, which anyone may run
  once nothing is in motion (S28): in full if the pool's free money covers every request, otherwise the same
  fraction of each (S69), and nothing if it has none (S70). What is not paid stays in the queue, so a
  withdrawal may take several settlement points. Capital in a running challenge or funded stage stays there
  until it ends: only an idle seat's capital can be brought back (S29). The queue holds at most 16 holders: a
  request from a 17th holder is refused, while a holder already in it may ask for more (S30).

## 3. Operator powers

None of the powers below waits for a delay or a vote: there is no timelock anywhere. Two of them are keys
we hold, the one that publishes agent keys (the key registry's operator on the deployments, the key binder's
publisher in main) and the gateway's host: whoever has either can trade accounts that are not theirs.

- **The factory's operator**, the deploy key on demo2 (S31, S32), can list or delist markets for new
  pools, while pools already created keep theirs (S33); change the challenge fee and its recipient for
  later purchases, with no cap (S34); set a builder and the most it may charge, which accounts approve
  when they start (S35), and there is none on demo2 (S36). In main the role moves in two steps, the new
  operator accepting (S37); the deployed factories hand it to any address in one call (S38).
- **The key registry's operator, on the deployments.** demo2, the first deployment and demo3 take their keys
  from a key registry: its operator publishes the agent keys (S40), and each new challenge and funded stage
  is given the next free one (S84). On all three the operator is the deploy key, the key that deployed them
  (S39, S85, S86): one hot key, not a multisig. Whoever holds it decides which agent addresses the next
  accounts get, and on demo3, whose code is the commit its record names (S75), a renewed (S83, S94) or re-keyed (S92)
  account takes the next free one too. With addresses of their own published, they could trade every account given
  a key after that, within its rules, as the gateway's host can (below). A published key cannot be
  withdrawn, by the operator or anyone: only the account it is given to retires it (S87), and a key that
  gains a HyperCore account of its own is skipped, and anyone may clear it (S88). Each key published, each
  key given and each handover of the role is an event on chain (S89, S90, S91). The role moves in two steps,
  the new operator accepting (S41).
- **The key binder, in main,** which no deployment runs yet. Its owner, meant to be a multisig, appoints
  the publisher with the hashes of the enclave's measurements and of its attestation document, which the
  event carries (S100) and neither of which may be zero (S101); appoints the guardian; sets a daily limit on
  bindings and renewal commitments (S102) and the delay of a renewal's key; and alone lifts a pause (S103). The publisher only
  signs: a binding needs its signature over one account's open request (S97). The guardian can only stop:
  pause every binding (S107), lower the limit, and revoke a bound key, which its account cuts as a stop
  does and asks for a fresh one (S104). A renewal's key is committed by its hash and shown, and approved,
  only after the delay (S105): before it works the guardian can pause, or revoke the account's current key,
  which voids the commitment. Whoever holds the publisher's key
  could bind keys of their own to accounts that are asking, up to the daily limit, until the guardian
  pauses; whoever holds the guardian's key could stop every binding and cut every key, and take nothing.
  Who holds each, and whether the publisher's key lives in an enclave, is the owner's decision and not
  in this code.
- **A pool's owner**, in every version, withdraws the pool's capital while it is idle (S19) and the
  challenge prices it has earned at any time (S42); the owner, or the funded trader, can end a funded stage
  without a breach (S43); and the owner cannot change the pool's rules or terms (S10). That is all a pool's
  owner can do on demo2, the site's live deployment, whose contracts are the ones before v3, built from the
  commit its record names (S73); the seats of its shared pools are clones of the same code (S74). In v3 --
  the v3 deployment, demo3, built from commit 23772e2d and tagged `v3-freeze-23772e2d` (S75), and main --
  the owner also can, each only while the pool is idle: send its spot to any address, not only to itself
  (S76); and choose its traders itself, for good, after which the pool sells no challenge (S77, S78). Only an
  owner the factory knows as a MultiPool, a contract, can then fund a trader of its choosing on the pool's
  capital (S79), and that trader can end the stage only if the owner allowed it (S80). In v3 a funded stage's
  agent key is approved for 179 days (S81) and, in its last 14 days (S82), anyone may renew it with a fresh
  key (S83); in main the renewal asks the key binder for that key (S93).
- **The shared pool's operator and platform** (S44) started the pool, added its seats and sealed it;
  after the seal it can add nothing (S16). It holds the starting shares, which never leave (S18). This
  pool takes no fee on profit (S45).
- **Whoever holds the gateway's host** holds the agent keys of the accounts it serves, and can sign
  anything Hyperliquid lets an agent sign for them (S3) until the account is stopped. The rules still
  apply after the fact through `breach`.
- **The keepers** hold wallets for gas only. Every call they make is open to anyone (S46, S47).
- **A paper exam's filler.** An exam started with a filler takes its orders' fills, inside their window, from
  that one address, never the trader's agent (S108), so the trader picks no block. The filler does: whoever holds it chooses the block
  in the window, and where the pool's creator is the platform that also runs the filler, the platform
  chooses for its own pool. What holds it is that it shows: every fill says how many blocks after the first
  possible one it came (S109), for anyone to count a pool's fills by. If the filler stops, an order that only
  makes a position smaller is anyone's, the trader's too, sixty blocks after its window (S110). Such an exam
  may fill at the book, the trader's side of the chain's best bid and offer, never better for the trader
  than the oracle price, so a book the trader moves gives nothing, and never more than its ceiling worse
  (S111); every fill says where its price came from (S109).
- **A simulated pool's checker.** A payout of a simulated pool waits 48 hours (S112). Until then the factory's
  checker, and nobody else, may stop it once, with a reason the chain keeps: the other account and the
  transactions that show the opposite positions, and for linked wallets the transactions that link them
  (S113, S114). A stopped payout's money stays in the pool, which is its creator's, and in our first pools the
  creator is the platform that also runs the checker. What holds it is that it shows: every stop and every
  lift carries the pool's counts of stops, lifts and payouts, the checking script is to be published with the
  keeper (`ops/sim_check.py`, not in this change), and the checker
  lifts a stop that was wrong (S115). The filler and the checker are two keys, and neither opens a pool (S116).
- **A simulated pool's creator** withdraws only what stays above three payouts of the largest size and above
  every payout owed and every payout set aside for a trader whose transfer was refused (S117), and stops
  nobody's payout.
- **What none of us can do:** send a challenge account's money anywhere but to its trader's share and
  back to its pool (S48, S49); change the rules of an account that is running (S10); or upgrade a pool or
  a challenge, each a minimal clone of a fixed implementation with no upgrade path (S50, S51).

## 4. Closing

- **A challenge** ends by `breach` when a rule is broken, by anyone (S12); by `expire` after its
  deadline, by anyone (S52); by `forfeit`, its trader alone (S53); or by `graduate`, by anyone, once the
  target is met with nothing open and no rule broken, in time (S54). If its capital never arrived, or, in
  main, its key was asked for and has not come within the hour, `abort` refunds the price and the
  platform's fee (S6). On the deployed factories `abort` refunds the price, and the fee, paid at the sale,
  stays paid.
- **Then `settle`**, open to anyone, closes what is open. A trader who passed is paid their share of the
  profit (S48); everything else goes back to the pool (S49). A loss is the pool's.
- **A funded stage** opens after a pass, when the key it asks for arrives (S72). If it has not opened within
  7 days, anyone may release the pool (S55), but only once its key was asked for and has not come for 7 days
  (S71), or, on a builder dex, once the capital sent there has not shown. Until the key is asked for, the pool
  waits for `openFundedStage`, which anyone may call. It ends by `breach`, by the owner
  or the funded trader (S43), or, on a shared-pool seat, once its published term has run, by anyone (S56).
  `settleFunded` then closes the positions. The funded trader is paid a share of the profit only if the stage
  ended without a breach (S57); the capital stays in the pool, which goes back to idle (S58).
- **A pool** has no closing call of its own: its owner withdraws its spot while it is idle (S19). While a
  challenge runs, the pool's capital waits for it to end, up to its deadline.

## 5. Availability and recovery

- **If the host stops,** the stops and takes already on Hyperliquid stay where they are. Nothing moves
  them or places new ones, and no keeper calls `breach`, until it is back. Those calls are open to
  anyone in the meantime (S46, S47).
- **The host reads Hyperliquid on one budget per IP,** 1,200 of weight a minute (S59). Past it, reads are
  refused, and an order that needs one is refused before anything is sent.
- **Money sent straight to a contract.** USDC sent to a pool's address on HyperEVM stays there as a token
  balance: on testnet the bridge credited nothing to a contract bridging to itself (S60), and the owner
  can withdraw only the challenge prices the pool earned (S42). USDC sent to a challenge account on
  HyperCore counts as equity, and can even reach the target without trading (S61).
- **Monitoring can miss things.** Nothing here guarantees that a problem is noticed, or noticed in time.

## 6. Audits and reports

- An internal audit went through these contracts between 25 and 28 September 2026. Each finding and the
  test that pins it is in `docs/AUDIT-FINDINGS.md` (S62). Three are open: a pass can be bought by
  depositing, by design (S61); a pool whose order cap sits under Hyperliquid's minimum (S63); and the
  factory's one-step operator handover on the deployed factories (S38), which main hands on in two steps.
- No external audit, and no third-party scan, of this code has been published.
- The shared pool contracts are testnet code, outside the reviewed core (S64).
- To report a problem, open an issue in this repository. There is no bug bounty.

## Sources

`path:line` is a line of this repository, and the last column is text on that line. An address is on
HyperEVM testnet (chain 998), read with the call in the last column on 6 Nov 2026, and S85 and S86 on 9 Nov 2026.

| # | Where | What is there |
|---|---|---|
| S1 | `src/RuledAccount.sol:231` | `function _setAgent(address key) internal` |
| S2 | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint | `Destination must match the source address` |
| S3 | `gateway/demo_signer.py:8` | `whoever holds the host holds these keys` |
| S4 | `src/Pool.sol:323` | `CoreOps.sendUsdc(ch, _terms.capital * Units.SPOT_PER_PERP);` |
| S5 | `src/Pool.sol:319` | `f.usdc().safeTransferFrom(msg.sender, address(this), price + fee);` |
| S6 | `src/Pool.sol:343` | `safeTransfer(challengeTrader, back)` |
| S7 | `gateway/server.py:216` | `The stop and the take go first: an order that may open a position is never` |
| S8 | `gateway/protect.py:600` | `"grouping": "positionTpsl"` |
| S9 | `src/PoolFactory.sol:281` | `pool.rules(), pool.terms()` |
| S10 | `src/RuledAccount.sol:99` | `internal onlyInitializing` |
| S11 | `src/RuledAccount.sol:142` | `function violation(uint32[] memory extraAssets) public view returns (Breach)` |
| S12 | `src/ChallengeAccount.sol:217` | `if (reason == Breach.None) revert NoBreach();` |
| S13 | `src/RuledAccount.sol:314` | `function _cutAgent(bytes32 salt) internal` |
| S14 | `src/ChallengeAccount.sol:74` | `event Stopped(Status indexed status, Breach indexed reason, int64 equity);` |
| S15 | `app/data/evidence.json:62` | `breachReason DailyLoss` |
| S16 | `src/shared/SharedPool.sol:271` | `if (seatsSealed) revert SeatsClosed();` |
| S17 | `0x348F9814061B0d26c2aBbdd2FE19ca807ceDa8a4` | `seatsSealed() → true` |
| S18 | `src/shared/SharedPool.sol:378` | `(msg.sender == platform ? seedShares : 0)` |
| S19 | `src/Pool.sol:193` | `function withdrawOnCore(uint64 amount1e8) external onlyOwner inStage(Stage.Idle)` |
| S20 | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations | `Liquidations` |
| S21 | https://hyperliquid.gitbook.io/hyperliquid-docs/trading/auto-deleveraging | `Auto-deleveraging` |
| S22 | `gateway/server.py:69` | `PROTECT_EVERY_S = 30.0` |
| S23 | `gateway/protect.py:748` | `CLOSE_AFTER_S = 30.0` |
| S24 | `ops/host/colosseum-keeper.service:17` | `--every 30` |
| S25 | `gateway/protect.py:588` | `a market halted by its deployer takes every order off` |
| S26 | `src/shared/SharedPool.sol:382` | `if (block.timestamp < until) revert Locked(until);` |
| S27 | `deployments/testnet-shared-demo2c.json:16` | `"lock": 600,` |
| S28 | `src/shared/SharedPool.sol:423` | `function settle(address[] calldata recognize) external started` |
| S29 | `src/shared/SharedPool.sol:331` | `revert SeatBusy(seat);` |
| S30 | `src/shared/SharedPool.sol:84` | `MAX_QUEUE = 16;` |
| S31 | `deployments/testnet-demo2.json:10` | `"deployer": "0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4",` |
| S32 | `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222` | `operator() → 0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4` |
| S33 | `src/PoolFactory.sol:126` | `Delisting doesn't touch` |
| S34 | `src/PoolFactory.sol:163` | `function setChallengeFee(uint256 fee, address recipient) external onlyOperator` |
| S35 | `src/ChallengeAccount.sol:160` | `CoreOps.approveBuilder(builder_, maxFee);` |
| S36 | `0x5CbCAF8829eD955c4a8aDA2B28Bf75f8ba867222` | `builder() → (0x0000000000000000000000000000000000000000, 0)` |
| S37 | `src/PoolFactory.sol:119` | `function acceptOperator() external` |
| S38 | `docs/AUDIT-FINDINGS.md:35` | `\| A-20 \| Low \|` |
| S39 | `0x53AF27F65Dd7473c890f633aC0025b261307779e` | `operator() → 0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4` |
| S40 | `src/KeyRegistry.sol:103` | `function publish(address[] calldata keys) external onlyOperator` |
| S41 | `src/KeyRegistry.sol:94` | `function acceptOperator() external` |
| S42 | `src/Pool.sol:207` | `function withdrawEarned() external onlyOwner` |
| S43 | `src/Pool.sol:496` | `if (msg.sender != owner && msg.sender != fundedTrader) revert NotAllowed();` |
| S44 | `deployments/testnet-shared-demo2c.json:19` | `"platform": "0x962Cf63A030A6caB62DEa2BdfBcAD11e4CFC188F",` |
| S45 | `deployments/testnet-shared-demo2c.json:13` | `"fee_bps": 0,` |
| S46 | `src/ChallengeAccount.sol:208` | `Stops the challenge if a rule is broken right now. Anyone may call it.` |
| S47 | `src/ChallengeAccount.sol:133` | `Anyone may call it.` |
| S48 | `src/ChallengeAccount.sol:359` | `CoreOps.sendUsdc(trader, pay);` |
| S49 | `src/ChallengeAccount.sol:375` | `CoreOps.sendUsdc(address(pool), spot);` |
| S50 | `src/PoolFactory.sol:246` | `pool = Clones.clone(poolImpl);` |
| S51 | `src/PoolFactory.sol:5` | `import {Clones} from "@openzeppelin/contracts/proxy/Clones.sol";` |
| S52 | `src/ChallengeAccount.sol:227` | `if (block.timestamp <= deadline) revert TooEarly();` |
| S53 | `src/ChallengeAccount.sol:236` | `if (msg.sender != trader) revert NotTrader();` |
| S54 | `src/ChallengeAccount.sol:259` | `function graduate(bytes32 salt) external inStatus(Status.Active)` |
| S55 | `src/Pool.sol:106` | `AWAIT_KEY_WINDOW = 7 days;` |
| S56 | `src/shared/SharedPool.sol:354` | `function endFundedTerm(address seat` |
| S57 | `src/Pool.sol:536` | `fundedPayoutOwed = fundedEndReason == Breach.None && result > fundedStart` |
| S58 | `src/Pool.sol:628` | `stage = Stage.Idle;` |
| S59 | https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits | `aggregated weight limit of 1200 per minute` |
| S60 | `src/Pool.sol:31` | `the USDC bridge credited nothing to a contract bridging to itself` |
| S61 | `docs/AUDIT-FINDINGS.md:20` | `\| A-05 \| Medium \|` |
| S62 | `docs/AUDIT-FINDINGS.md:3` | `between 25 and 28 September 2026` |
| S63 | `docs/AUDIT-FINDINGS.md:32` | `\| A-17 \| Low \|` |
| S64 | `src/shared/SharedPool.sol:29` | `Testnet only; not part of the reviewed core.` |
| S65 | `gateway/server.py:74` | `PROTECT_HOT_EVERY_S = 15.0` |
| S66 | `gateway/protect.py:101` | `MARKET_SLIPPAGE = Decimal("0.1")` |
| S67 | `gateway/protect.py:1319` | `Only a "no" counts, Hyperliquid's or the signer's` |
| S68 | `gateway/protect.py:583` | `"tif": "Ioc"` |
| S69 | `src/shared/SharedPool.sol:576` | `the pool's free money covers it, otherwise the same fraction of each.` |
| S70 | `src/shared/SharedPool.sol:612` | `if (have6 == 0) return;` |
| S71 | `src/Pool.sol:449` | `if (keyAskedAt == 0 \|\| block.timestamp <= uint256(keyAskedAt) + AWAIT_KEY_WINDOW) revert KeyAvailable();` |
| S72 | `src/Pool.sol:380` | `_askKey(fundedTrader, KeyBinder.Purpose.Funded);` |
| S73 | `deployments/testnet-demo2.json:9` | `"commit": "e4287926ad0bb13a0e401b45118215195ecb5589",` |
| S74 | `deployments/testnet-shared-demo2c.json:14` | `"implementations_from": "e4287926ad0bb13a0e401b45118215195ecb5589",` |
| S75 | `deployments/testnet-v3-demo3.json:23` | `"commit": "23772e2dbf9f4913dc265efa68768030ab7d4d2a",` |
| S76 | `src/Pool.sol:201` | `function sendOnCore(address to, uint64 amount1e8) external onlyOwner inStage(Stage.Idle)` |
| S77 | `src/Pool.sol:220` | `function runByOwner(bool traderMayStop_) external onlyOwner inStage(Stage.Idle)` |
| S78 | `src/Pool.sol:292` | `if (ownerRun) revert SalesClosed();` |
| S79 | `src/Pool.sol:238` | `if (!ownerRun \|\| !IPoolFactory(address(factory)).isMultiPool(owner)) revert NotOwnerRun();` |
| S80 | `src/Pool.sol:497` | `if (msg.sender != owner && ownerRun && !traderMayStop) revert NotAllowed();` |
| S81 | `src/RuledAccount.sol:214` | `AGENT_TERM = 179 days;` |
| S82 | `src/RuledAccount.sol:216` | `RENEW_WINDOW = 14 days;` |
| S83 | `src/Pool.sol:261` | `function renewAgent() external inStage(Stage.Funded)` |
| S84 | `src/KeyRegistry.sol:120` | `function assign(address trader) external returns (address key)` |
| S85 | `0x6b256B983b849934e0AA500cF2e3Ca176B0d35BA` | `operator() → 0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4` |
| S86 | `0x8e2E527aed19a0969aEa49e16aE1Abbc7B84Cc85` | `operator() → 0x00d014dF2b4Ffdb0654ea079e4792fd15a350Fd4` |
| S87 | `src/KeyRegistry.sol:159` | `revert NotBoundHere(key, msg.sender);` |
| S88 | `src/KeyRegistry.sol:141` | `function purgeSpoiled(uint256 max) external returns (uint256 purged)` |
| S89 | `src/KeyRegistry.sol:51` | `event KeyPublished(address indexed key);` |
| S90 | `src/KeyRegistry.sol:52` | `event KeyBound(address indexed key, address indexed account, address indexed trader);` |
| S91 | `src/KeyRegistry.sol:48` | `event OperatorTransferStarted(address indexed from, address indexed to);` |
| S92 | `src/Pool.sol:275` | `function rekey() external inStage(Stage.Funded)` |
| S93 | `src/RuledAccount.sol:291` | `_askKey(trader_, KeyBinder.Purpose.Renew);` |
| S94 | `src/ChallengeAccount.sol:245` | `_renewAgent(trader);` |
| S95 | `gateway/protect.py:74` | `STOP_BUFFER_BPS = Decimal(6)` |
| S96 | `src/KeyBinder.sol:240` | `function request(address trader, Purpose purpose) external returns (uint64 id)` |
| S97 | `src/KeyBinder.sol:303` | `if (ECDSA.recover(digest(account, r.id, r.trader, keyHash), signature) != publisher) revert BadSignature();` |
| S98 | `src/KeyBinder.sol:320` | `if (CoreOps.exists(key)) revert KeyExistsOnCore(key);` |
| S99 | `src/KeyBinder.sol:333` | `IBoundAccount(account).onKeyBound(key, purpose);` |
| S100 | `src/KeyBinder.sol:94` | `event PublisherSet(address indexed publisher, bytes32 pcrHash, bytes32 attestationDocHash);` |
| S101 | `src/KeyBinder.sol:170` | `if (pcrHash == bytes32(0) \|\| attestationDocHash == bytes32(0)) revert ZeroHash();` |
| S102 | `src/KeyBinder.sol:312` | `if (bindsToday >= maxBindsPerDay) revert DailyLimit();` |
| S103 | `src/KeyBinder.sol:191` | `function unpause() external onlyOwner` |
| S104 | `src/KeyBinder.sol:226` | `function revoke(address key, bytes32 salt) external onlyGuardianOrOwner` |
| S105 | `src/KeyBinder.sol:297` | `if (block.timestamp < r.activatesAt) revert TooEarly(r.activatesAt);` |
| S106 | `src/Pool.sol:334` | `emit ChallengeFeePaid(challengeTrader, heldFeeTo, fee);` |
| S107 | `src/KeyBinder.sol:212` | `function pause() external onlyGuardianOrOwner` |
| S108 | `src/exam/PaperExam.sol:330` | `if (filler != address(0) && msg.sender != filler) revert NotFiller();` |
| S109 | `src/exam/PaperExam.sol:364` | `emit Filled(o.asset, o.isBuy, o.sz, px, cost, block.number - from, source);` |
| S110 | `src/exam/PaperExam.sol:98` | `uint16 public constant FALLBACK_BLOCKS = 60;` |
| S111 | `src/exam/PaperExam.sol:519` | `if (worse * Units.BPS > uint256(oracle) * maxWorseBps) revert TooFarFromOracle(px, oracle);` |
| S112 | `src/sim/SimPoolFactory.sol:36` | `uint32 public constant PAYOUT_DELAY = 48 hours;` |
| S113 | `src/sim/SimPool.sol:419` | `if (block.timestamp >= p.dueAt) revert NotDue();` |
| S114 | `src/sim/SimPool.sol:158` | `event PayoutStopped(` |
| S115 | `src/sim/SimPool.sol:440` | `function liftStop(uint256 id) external onlyChecker {` |
| S116 | `src/sim/SimPoolFactory.sol:151` | `if (msg.sender == filler \|\| msg.sender == checker) revert BadRole();` |
| S117 | `src/sim/SimPool.sol:262` | `uint256 keep = owed + held + (finished ? 0 : uint256(largestPayout) * factory.FLOOR_PAYOUTS());` |
