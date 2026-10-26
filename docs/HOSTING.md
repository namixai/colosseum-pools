# Hosting the app, the gateway and the keeper

Status, 14 October 2026: the gateway and the keepers run on the host against the second
deployment (`deployments/testnet-demo2.json`, since 1 October), from release `4900bcb1` (since
7 October, 09:33:59 UTC), each `active` under systemd, and the gateway is public as
`pools-api.usenami.io` behind Cloudflare with `/v1/health` answering. The app at
`pools.usenami.io` is, on 14 October, the `app/` folder of commit `5ac46e18`: every file compared
with the site, byte for byte, apart from the one script Cloudflare adds to `index.html` before
`</body>`.

On 18 October 2026 the keepers' node was changed by hand, from Chainlink's to Hyperliquid's,
after Chainlink's went down and both keepers missed a day's snapshot. Both units also run with
`--max-windows 5`. The release is unchanged. [The keepers' node, 16–18 October
2026](#the-keepers-node-1618-october-2026) has what happened and what it reopened.

## Where things run

- **The pools host.** EC2 `t3.small` in us-east-1, Amazon Linux 2023, no instance profile,
  tagged `TerminateAfter=2026-12-06`. Its address isn't written here: the operator passes it
  to `deploy.sh`.
  - Its security group lets in 443 only from Cloudflare's ranges and 22 only from the
    operator's address.
  - It runs the gateway and the keeper, each as its own user.
- **The gateway** (`colosseum-gw`) listens on `127.0.0.1:8787`. nginx serves it on 443 as
  `pools-api.usenami.io`, behind Cloudflare.
- **The keeper** (`colosseum-keeper`) has no port. It polls the chain every 30 seconds.

  🔴 **Since 18 October 2026 the two share one node again, Hyperliquid's, and the problem this
  paragraph describes is open again.** Chainlink's node went down and no other node tried could
  serve the keeper (below). What follows is why they were separated, and it still holds.

  **The two used different nodes on purpose.** Both
  nodes rate-limit per IP and this host is one IP, so while the gateway and the keeper shared
  a node they shared a budget — and the gateway is public. An outsider's order costs the
  gateway five chain reads *before* it is refused, so a stream of refusable orders spends the
  keeper's budget from the outside, and a keeper that is being throttled does not stop a trader
  who has broken the rules. The investor pays for that delay. So, from 25 September to 17 October
  2026, the keeper read `rpcs.chain.link` and the gateway `rpc.hyperliquid-testnet.xyz`.

  The keeper had been kept off Hyperliquid's node because that node refused its `eth_getLogs` *from
  this host* — `invalid block range` for any range, however near the head, while the same call
  from elsewhere went through (measured 24 Sep 2026). On 18 October it served them near the head
  from the host and timed old ranges out; the refusal of 24 September did not come back. The gateway can, because it makes no `eth_getLogs` call at all: it reads state with
  `eth_call` and nothing else. Check before trusting this on a new host, from the host itself
  and not from a laptop — the refusal above is what a laptop does **not** reproduce:

      curl -s -X POST https://rpc.hyperliquid-testnet.xyz/evm -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"<registry>","data":"0xa6f48c90"},"latest"]}'

  `0xa6f48c90` is `freeCount()`; a number back means the gateway's reads work from there.

  These per-IP limits are also why `ops/host/nginx-pools-api.conf.in` throttles: that budget
  protects the gateway's reads. It does not protect the keeper: from 18 October the keeper reads the
  same public node from the same host, so the two share one per-IP budget (audit M-1, accepted on
  testnet, to be separated before mainnet).

  ✅ **Done on the running host on 25 September 2026**, by hand, and checked rather than
  assumed. What was seen there: the new node answered `eth_call` **from the host** (`freeCount()`
  came back `0x…0005`); after the swap `GET /v1/health` returned
  `{"ok": true, "chain_id": 998, "signer": "demo", "keys": 32}`; and — the check that matters,
  because a gateway can start and still fail its first chain read — a throwaway key asking about
  an address that is not a pool account was refused `not_an_account`, which is only reachable by
  reading the chain. nginx went to 15 requests a minute with a burst of 10 in the same pass; the
  `pools_ip` line was left alone. Both files were copied first and the copies are still there as
  `*.bak-0925`.

  🔴 **Keep the key directory reconciled with the registry.** On that same visit the directory
  held twenty `*.key` files while the registry had only ever published sixteen addresses: four
  usable keys had been sitting there unpublished since the start. Nobody knew, because nothing
  compares the two. Comparing the two counts directly does NOT work, and the
  first version of this rule said to: `GET /v1/health` counts every key the gateway holds,
  including ones already bound to an account, while `freeCount()` counts only what is still on the
  registry's free list. A bound key is held and not free, so the two differ in normal operation
  and a gap means nothing on its own. What has to be compared is the SET of addresses the gateway
  holds against every address the registry has ever published, in any state — a gateway address
  the registry does not know is a key nobody can be given, and a published address the gateway
  does not hold is a key that makes a trader pass and then find nothing able to sign. The gateway
  does not list its addresses today, only their number, so this is a reconciliation that needs a
  way to ask it; until then the count is a hint and not a check.

  🔴 **A keeper that has fallen behind is not watching, and it takes real time to come back.**
  The keeper reads `eth_getLogs` 50 blocks at a call and the unit runs the defaults, 50 calls a
  pass: 2,500 blocks each time. **The 50 is ours, not the node's.** Measured 25 September 2026 on
  a range old enough that the chain's tip could not interfere: both nodes answer a 1,000-block
  query, and Hyperliquid's refuses 2,000 with `-32602 query exceeds max block range 1000`. Our
  own `MAX_LOG_WINDOW = 50` in `ops/keeper.py` caps the `--log-window` flag, so an operator
  cannot widen it without a code change — and the comment on that constant, which calls 50 what
  HyperEVM accepts, is wrong. At the node's own limit a catch-up would be twenty times shorter. The 30-second wait comes *after* a pass, not instead of it, so a
  cycle is 30 seconds plus however long the pass took and the ceiling of 5,000 blocks a minute
  never arrives. Measured on 25 September 2026, walking 177,422 blocks took 44 minutes —
  **about 4,000 blocks a minute**, with single cycles between 34 and 35 seconds. The chain makes
  **61 a minute** — about one block a second, measured 65206003 at 10:16:59Z against 65209897 at
  11:20:49Z, which is 3,894 blocks in 3,830 seconds — so catching up is quick per minute of downtime and slow per day of it.
  Keep the two apart: **4,000 a minute is how fast the keeper walks, one a second is how fast the
  chain is made.**

  🔴 **A past block number does not buy you the past.** `eth_call` with an old block silently
  answers from the current state on both nodes: `status()` on challenge `0xf4d98de2…` at block
  65200000 answers `8` (Settled) although it was only settled in block 65204639, with no error
  and no warning. So nothing can be reconstructed by asking an old block, and a binary search
  over past state returns the same answer at every point. Whoever needs history reads events.

  While it walks it has not yet seen the events that name the pools, so `following` climbs
  towards its true number rather than starting there, and it enforces nothing on the pools it has
  not reached yet — silently, because every pass still logs `pass_done` and looks like a healthy
  one.

  What to read: `pass_done` carries `next_block` and `latest`. A gap that is **shrinking** means
  it is catching up — divide the gap by 4,000 for the minutes left. A gap that stays level means
  it is keeping pace with the chain but **not catching up**, which at 57 blocks a minute against
  2,500 a pass should not happen and means something is eating the passes. A gap that grows, or
  `scan_stopped` above the line, means it is **stuck** rather than slow. `--max-windows`
  (default 50) is the lever if a catch-up ever has to go faster, at the cost of more calls per
  pass against a node that rate-limits.
- **A second keeper is a copy of the first, not a special build.** Every call the keeper makes is
  open to anyone: on a challenge `activate`, `abort`, `checkpoint`, `breach`, `expire`, `settle`
  and `recut`; on a pool `breach`, `settleFunded`, `checkpoint` and `recut`. The only two calls
  with a caller check are `forfeit` (the trader alone) and `stopFunded` (the investor or the
  funded trader), and the keeper makes neither. `graduate` is open to anyone as well, and the
  keeper still leaves it alone on purpose — passing the moment the target is touched cuts the
  trader's run short, so the timing is theirs. The keeper's own header says so. Open to anyone
  also means a stranger can make that call: an account that is flat and at its target can be
  passed at a moment its trader did not choose. The call ends the challenge at the equity it has
  then: the trader's share of its profit is fixed from that equity, and later gains cannot add to
  it. It is paid at settlement, out of what the account then holds, so it can come to less. The
  pass is recorded for the trader on record, not for the caller; opening the funded stage is a
  separate call, `openFundedStage`. So a second keeper has exactly
  the powers of the first, and needs no permission from it or from us.

  What it needs is its own wallet and its own state file:

      COLOSSEUM_KEY_DIR=<its own key directory> \
      COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm \
      python -m ops.keeper --deployment demo2 --state <its own file>.json --every 30 --max-windows 5

  The second keeper's unit, `colosseum-keeper2`, was written on the host by hand and is not in
  this repository: a deploy installs `colosseum-keeper.service` only, so a change to the first
  unit has to be made to the second one by hand as well.

  The wallet is a separate `<name>.key`/`<name>.addr` pair with its own gas (a stop cost 194,818
  gas at 0.1 gwei on 25 Sep 2026, about 0.0000195 HYPE). The state file must not be shared: it
  holds `next_block` and the pools being followed, and two processes writing one file would
  corrupt each other's place.

  🔴 **Do not let a second keeper catch up beside a working one on the same host.** Measured
  25 September 2026, starting one with an empty state file: within a minute the FIRST keeper —
  the one actually enforcing — logged `pass_failed: eth_blockNumber: rate limited 6 times in a
  row`. Two keepers on one host share one address at the RPC, and a catch-up spends fifty
  `eth_getLogs` a pass against a node that counts them. The newcomer does not join the watch; it
  blinds the incumbent for as long as it walks.

  Seed it instead. Copy the working keeper's state file **whole** into the new one before
  starting it, so it begins at the head with the pools already known. Copy it whole and not in
  part: `Keeper.__init__` reads all three keys, and a hand-written file carrying only
  `next_block` and `live` dies on startup with a bare `KeyError: 'factory'`. (A `factory` that
  is present but belongs to another deployment is caught properly — `<path> belongs to another
  factory`, and the keeper refuses to start rather than enforcing the wrong pools.)

      sudo systemctl stop colosseum-keeper2
      sudo cat /var/lib/colosseum-keeper/keeper-demo.json   # all three keys: factory, next_block, live
      printf '%s\n' '<that json, entire>' | sudo tee /var/lib/colosseum-keeper/keeper2-demo.json >/dev/null
      sudo chown colosseum-keeper:colosseum-keeper /var/lib/colosseum-keeper/keeper2-demo.json
      sudo chmod 600 /var/lib/colosseum-keeper/keeper2-demo.json
      sudo systemctl start colosseum-keeper2

  (`sudo -u colosseum-keeper install -m 0600 /dev/stdin …` looks tidier and does not work:
  dropping to that user makes the heredoc unreadable, and the write fails with `Permission
  denied`.) Seeded that way both keepers sat at the head, each following the same two pools,
  with **no rate-limit failure on either in the three minutes after** — the steady-state load of
  two fits where a catch-up does not. Two keepers have run on this host since 25 September 2026:
  `0xD6F07317fC5f12302776b03A7206B1614FD49021` and
  `0xcbd5C0299669e0C686D375cc6C07584Ad5C4fECa`.

  **What happens when both reach the same account in the same block.** `breach` re-reads the
  violation from the chain and refuses if there is none (`NoBreach`), so neither keeper can stop
  an account that is inside its rules — the worst a second one can do is duplicate work. If both
  send a stop for the same account, one lands and the other reverts on the stage or status
  guard (`BadStage`, `BadStatus`); the loser pays for a reverted transaction, which is gas
  without an effect, and the account is stopped exactly once. `settle` and `settleFunded` do not
  revert while there is settling left to do: the second call reads the state the first left and
  either takes the next step or returns early. Again, wasted gas and nothing else.

  🔴 **What the code does not give you:** there is no coordination, no leader election and no
  de-duplication between keepers. "Redundant" here means two independent callers racing, and the
  loser pays every time they collide. That is the whole cost, and it is small — but if it matters,
  stagger the two rather than expecting the chain to sort it out: the second keeper's `--every`
  and its start time are the only levers, and neither is a guarantee.

- **The app** is the static `app/` folder, served by a Cloudflare Worker with static assets
  (`raspy-violet-594d`) on `pools.usenami.io`. There is no build step and no server code: every
  page is a `#/…` route of the one `index.html`. It calls the gateway from the browser;
  `GATEWAY_ALLOW_ORIGIN` names it.
- **RPC** (CTO, 17 Sep; measured again on the host 24 Sep):
  - **until 25 September** the gateway read the chain through
    `rpcs.chain.link/hyperevm/testnet` — it now reads `rpc.hyperliquid-testnet.xyz`, and the
    paragraph at the top of this file says why. What follows is about that old node, which allows 1000
    calls per IP in five minutes. It throttles in bursts: on 24 Sep it refused three `eth_call`s
    inside one second and served the same reads five times over seconds later, so the gateway
    retries five times and answers 429 when it still cannot read (`gateway/chain.py`);
  - the keeper reads event logs, and **Hyperliquid's own node refuses `eth_getLogs` from this
    host**: `-32602 invalid block range` for any range, 60 blocks back or 100 000, while
    `eth_blockNumber` answers and the same call from a laptop goes through. `rpcs.chain.link`
    served those logs from the host, so that is what the keeper used until 17 October;
  - **from 18 October** the keeper reads `rpc.hyperliquid-testnet.xyz` too. Chainlink's node
    stopped answering, drpc's could not read the precompiles, and Hyperliquid's answered
    everything the keeper needs except old `eth_getLogs` ranges; see the section below.
  - 🔴 A 403 from `rpcs.chain.link` can be the client rather than the endpoint: plain
    `urllib` is refused there, `curl` and `requests` with a User-Agent are not. Measure with the
    client the service actually uses, or the answer is about your tool.

## Layout on the host

| path | what | owner, mode |
|---|---|---|
| `/opt/colosseum-pools/releases/<sha>` | one commit's files, from `git archive`; `REVISION` names it | root, read-only to the services |
| `/opt/colosseum-pools/current` | link to the release that runs | root |
| `/opt/colosseum-pools/venv` | link to `venv-<hash of spike/requirements.txt>`, Python 3.12 | root |
| `/etc/colosseum/gateway.env`, `keeper.env` | paths, URLs, the app's origin; no secret | root, 644 |
| `/etc/colosseum/deployment` | the label of the deployment the services use | root, 644 |
| `/etc/colosseum/deployment.env` | written by `install.sh` from that label's record: label, factory, registry | root, 644 |
| `/etc/colosseum/tls/origin.key` | TLS key for `pools-api.usenami.io`, made on the host | root, 600 |
| `/etc/colosseum/tls/origin.csr`, `origin.crt` | its request and Cloudflare's Origin CA certificate, both public | root, 644 |
| `/var/lib/colosseum-gw/keys` | the demo's agent keys, one `*.key` file each | `colosseum-gw`, 700 and 600 |
| `/var/lib/colosseum-keeper/secrets` | `keeper.key` and `keeper.addr`, the keeper's gas wallet | `colosseum-keeper`, 700 and 600 |
| `/var/lib/colosseum-keeper/keeper-<label>.json` | the keeper's progress; rebuilt from the chain if lost | `colosseum-keeper` |

Each service can't read the other's `/var/lib` directory, by owner and again by its unit
(`InaccessiblePaths`). The units are in `ops/host/` and run with `ProtectSystem=strict`,
without capabilities. The gateway writes nothing outside its private `/tmp`; the keeper
writes only to its own directory.

Whoever holds root on this host holds the agent keys and can trade every account whose key is
on it, until someone stops that account. The keys are testnet keys for mock USDC, made for the
demo and used nowhere else.

## One-time setup

1. On the host, as root: `ops/host/bootstrap.sh`. It installs Python 3.12 and nginx, makes
   the users, directories and env files, the TLS key and its CSR, and prints the CSR.
   Running it again adds only what is missing.
2. From the Mac: `ops/host/deploy.sh <commit> <user@host>`, which also builds the venv.
3. The agent keys, on the host, as the gateway's user. Only the addresses leave the host,
   for `ops/deploy_testnet.py --keys-file`:

       cd / && sudo -u colosseum-gw /opt/colosseum-pools/venv/bin/python \
           /opt/colosseum-pools/current/ops/make_demo_keys.py --out /var/lib/colosseum-gw/keys --count 20

4. The keeper's key, on the host, as the keeper's user. It prints the address, which then
   needs testnet HYPE for gas:

       cd / && sudo -u colosseum-keeper /opt/colosseum-pools/venv/bin/python \
           /opt/colosseum-pools/current/ops/make_demo_keys.py --wallet keeper --dir /var/lib/colosseum-keeper/secrets

   🔴 **Check that it has gas, and check it again after any redeploy.** This is the one failure
   that looks like nothing: a keeper with an empty wallet finds every breach correctly and sends
   none of them, and the node's refusal reads like a rate limit. A stop cost 0.0000195 HYPE on
   25 Sep 2026 (194818 gas at 0.1 gwei), so a little goes a long way — but zero goes nowhere.
   The journal now tells them apart: a `send_failed` line carries `gas_wei` and `unfunded`.
   `unfunded: true` means the wallet has dropped below the 0.001 HYPE reserve and wants topping
   up — it is a warning about the reserve, not proof that this send could not have gone through,
   because a balance just under the floor still pays for many stops. `gas_wei: 0` is the reading
   that leaves no doubt: that wallet can pay for nothing, and waiting will not change it.

       sudo journalctl -u colosseum-keeper -n 200 --no-pager | grep -F '"send_failed"'

5. TLS. Cloudflare checks the origin's certificate (the zone is set to Full, strict), so the
   host needs one from Cloudflare's Origin CA:
   - In the dashboard: SSL/TLS → Origin Server → Create Certificate. Choose "Use my private
     key and CSR", paste `/etc/colosseum/tls/origin.csr` and keep the hostname
     `pools-api.usenami.io`.
   - The certificate it shows is public. Save it as `/etc/colosseum/tls/origin.crt`, then
     deploy again: `install.sh` fills in Cloudflare's ranges, checks the config with
     `nginx -t` and turns nginx on.

## Deploying a commit

    COLOSSEUM_SSH_KEY=<key file> ops/host/deploy.sh <commit> <user@host>

It sends only a commit that is on `origin/main` as this clone knows it, and stops before anything
leaves the machine otherwise; until 5 October 2026 it said so and went on. An earlier commit of the
branch is on it too, which is how a release is rolled back. Then it uploads the commit's tracked
files and runs that commit's `ops/host/install.sh` on the host. Without `COLOSSEUM_SSH_KEY`, ssh
uses its own configuration. That script:

- builds the venv when the requirements changed;
- installs the units, and the nginx site once there is a certificate;
- writes `deployment.env`;
- switches `current`;
- restarts the services that are enabled;
- keeps the three newest releases.

The deployment record the services use has to be committed: `install.sh` reads it from the
release.

🔴 **By hand before the first release with HIP-3 (the platform's list as a setting, 21 October
2026):** the demo signer no longer holds the platform's perps in code. It reads them, with a size
cap for each, from `GATEWAY_SIZE_CAPS` in `/etc/colosseum/gateway.env`, and the gateway refuses to
start without it rather than sign nothing. `bootstrap.sh` writes that file only when it is missing,
so a running host needs the line added before the restart. The testnet's list and caps since
17 September, BTC, ETH and SOL:

    GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4"}
    GATEWAY_PERP_DEXES=

A perp of another dex (HIP-3) goes on the list by its order number, and its dex's name into
`GATEWAY_PERP_DEXES`, for example `xyz` with `"750000": "0.015"` for `xyz:XYZ100` on testnet.
Adding or taking a perp off is a change to this file and a restart, not a release. The keeper reads
the same dexes from `COLOSSEUM_PERP_DEXES` in `/etc/colosseum/keeper.env` (comma-separated, none by
default): with `xyz` there it sees an account's positions and orders on xyz, checks their stop and
names them to a stop, and logs `hip3_open_over_weekend` for a position still open from Friday 16:30
New York time. It holds no key and closes nothing itself.

Each dex costs Hyperliquid weight on the host's one budget of 1,200 a minute per IP. Weights as
Hyperliquid publishes them: `clearinghouseState` and `allMids` 2, `metaAndAssetCtxs`, `meta`,
`frontendOpenOrders` and `perpDexs` 20. For one live account (counted 21 Oct 2026):

| | Hyperliquid's own dex | with xyz |
|---|---|---|
| gateway, an order that opens (markets 20 + state 2 + orders 20, a dex) | 42 | 84 |
| gateway sweep, every 15 s (markets 20 + 22 an account, a dex) | 168 a minute | 336 a minute |
| keeper, every 30 s (meta 20 + 22 an account, a dex) | 84 a minute | 168 a minute |
| the two together | 252 a minute | 504 a minute |

Each further account adds 22 a dex to every sweep and every keeper pass.

🔴 **By hand before the first release with the leverage check:** each perp's own leverage cap, by
order number, in the same file. The gateway refuses to start without it, and with the demo signer
when it leaves out a perp `GATEWAY_SIZE_CAPS` lists. The CTO's caps of 21 October 2026: BTC 10x,
ETH 10x, SOL 5x, HYPE 5x, SP500 10x, XYZ100 8x, CL 3x, BRENTOIL 2x (GOLD 6x where it is listed).
On testnet, for the list above:

    GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5"}

with `"750000": "8"` and `"750003": "6"` when `xyz:XYZ100` and `xyz:GOLD` go on the list. On
mainnet the same perps have other numbers (BTC 0, ETH 1, SOL 5, HYPE 159; xyz is dex 1, so
`xyz:XYZ100` is 110000, `xyz:CL` 110029, `xyz:BRENTOIL` 110049, `xyz:SP500` 110052; read on
19 and 21 October 2026):

    GATEWAY_LEVERAGE_CAPS={"0": "10", "1": "10", "5": "5", "159": "5", "110052": "10", "110000": "8", "110029": "3", "110049": "2"}

A cap is a ceiling the platform sets; the pool's own leverage rule still holds the whole account.

Flat over the weekend is every pool's default: from Friday 16:00 New York time the gateway closes
the HIP-3 positions of its accounts (docs/GATEWAY.md). A pool that keeps them over the weekend is
named, by address, in the same file; none is unless set:

    GATEWAY_HOLD_OVER_WEEKEND=

The address is the pool's whose account trades: a challenge goes by its pool. A seat of a multi-trader
pool (v3) is a pool of its own, with its own address, and that address is the one to name here, not
the multi-trader pool's. A name that matches nothing leaves the pool flat over the weekend, the safe
side.

The gateway reads an account again two seconds after an order that may open went: one more
`clearinghouseState`, `frontendOpenOrders` and `metaAndAssetCtxs` per dex, about 42 of
Hyperliquid's weight without xyz and 84 with it, per such order.

🔴 **By hand on a host set up before 18 October 2026:** `/etc/colosseum/keeper.env` says
`COLOSSEUM_RPC_URL=https://rpcs.chain.link/hyperevm/testnet`, the node that went down on
17 October. `bootstrap.sh` writes that file only when it is missing, so deploying does not change
it. The running host was changed by hand on 18 October; a host built from an older copy of this
file needs the same line, on the operator's word:

    sudo sed -i 's|^COLOSSEUM_RPC_URL=.*|COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm|' \
        /etc/colosseum/keeper.env
    sudo systemctl restart colosseum-keeper colosseum-keeper2
    journalctl -u colosseum-keeper -u colosseum-keeper2 -n 10   # pass_done, next_block near the head

A deploy installs `colosseum-keeper.service` from the release, and from this commit on that unit
runs `--max-windows 5`; `colosseum-keeper2` is not installed by a deploy and keeps what was
written into it by hand.

🔴 **By hand on any host set up before 25 September 2026:** `/etc/colosseum/gateway.env` still
says `GATEWAY_RPC_URL=https://rpcs.chain.link/hyperevm/testnet`, which is the node the keeper
reads. While the two share it, anyone outside can spend the keeper's budget through the public
gateway (above), so until this is done that hole is open on the running host no matter what the
repository says. `bootstrap.sh` writes this file only when it is missing, so deploying does not
change it either. Check from the host FIRST — a laptop does not reproduce what this host sees:

    curl -s -X POST https://rpc.hyperliquid-testnet.xyz/evm -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"<registry>","data":"0xa6f48c90"},"latest"]}'

A number back (`freeCount()`) means the gateway's reads work from there. Only then:

    sudo sed -i 's|^GATEWAY_RPC_URL=.*|GATEWAY_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm|' \
        /etc/colosseum/gateway.env
    sudo systemctl restart colosseum-gateway
    journalctl -u colosseum-gateway -n 5

Deploy the nginx file in the same pass: the gateway's new node allows about 100 calls a minute
against the old one's 200, and the limits were recut for that. Running the old limits against the new
node makes the gateway refuse honest traders with `upstream_busy` out of its own budget.

After a deploy, from the Mac:

    ./scripts/verify-gateway-deploy.sh

It asks `/v1/health` which commit the gateway runs, checks that the commit is on `origin/main`, and
runs on that commit the tests of the leverage check before the signature; its last line is PASS or
FAIL. It reads and runs tests only; it changes nothing on the host.

## Choosing the deployment and starting

    echo rehearsal | sudo tee /etc/colosseum/deployment      # on the host
    ops/host/deploy.sh <commit> <user@host>                   # from the Mac
    sudo systemctl enable --now colosseum-gateway colosseum-keeper

`install.sh` refuses a label without a complete testnet record in the release.

## A further instance: a second deployment on the same host

The CTO's decision of 23 October 2026: demo3 (factory v3) is served beside demo2, whose live
challenge keeps its own gateway and keeper. The first deployment is what `/etc/colosseum/deployment`
names, served by `colosseum-gateway` and `colosseum-keeper`, as before. A further one is an
*instance* with a short name, `demo3`:

| | the first | instance `demo3` |
|---|---|---|
| deployment | `/etc/colosseum/deployment` | `/etc/colosseum/deployment-demo3` |
| units | `colosseum-gateway`, `colosseum-keeper` | `colosseum-gateway@demo3`, `colosseum-keeper@demo3` |
| settings | `gateway.env`, `keeper.env` | `gateway-demo3.env`, `keeper-demo3.env` |
| gateway | `127.0.0.1:8787`, `/v1/order`, `/v1/health` | `127.0.0.1:8788`, `/demo3/v1/order`, `/demo3/v1/health` |
| agent keys | `/var/lib/colosseum-gw/keys` | `/var/lib/colosseum-gw/instances/demo3/keys` |
| keeper key | `/var/lib/colosseum-keeper/secrets` | `/var/lib/colosseum-keeper/instances/demo3/secrets` |

Each gateway and each keeper is kept out of the other's keys by its unit (`InaccessiblePaths`), and
the two keepers send with two keys: one key shared would have them race for its nonces. The
instances' gateways all run as one user, and so do their keepers, so an instance's unit shows it
its own directory alone under `instances/` (`TemporaryFileSystem` over it, read-only, and its own
bound back with `BindReadOnlyPaths`), and hides other users' processes (`ProtectProc=invisible`,
`ProcSubset=pid`).

**What one user per service leaves open, and the mainnet condition.** The first gateway and an
instance's gateway are one user, `colosseum-gw`; two keepers are one user, `colosseum-keeper`. A
gateway taken over could still reach another gateway's keys through `/proc/<pid>/root` of that
gateway's process: the kernel lets a process of the same user read it, `ProtectProc=invisible`
hides other users' processes only, and Yama's `ptrace_scope=1` (Ubuntu's default) stops attaching
to, and reading the memory of, a process that is not one's child, not `/proc/<pid>/root`. On
testnet, with testnet keys, this is accepted (the CTO, 23 October 2026). **On mainnet a host runs
one instance, or each instance has a user of its own.** The paths
of an instance are on the same name and certificate, `pools-api.usenami.io`, so no DNS record and
no certificate change; `ops/host_instances.py` reads the instances and writes their nginx locations
under the same limits as the first's.

By hand, in this order (the deployment record must be in the release before step 4):

    # 1. the release with the instance units, as usual: nothing changes for the first
    ops/host/deploy.sh <commit> <user@host>
    # 2. the instance's directories and settings, on the host (XYZ100 at 0.015 an order, 8x, xyz)
    sudo bash /opt/colosseum-pools/current/ops/host/bootstrap.sh --instance demo3 v3-demo3 8788
    # 3. its keys: the agent key files from the Mac, owned by colosseum-gw, mode 600, into
    #    /var/lib/colosseum-gw/instances/demo3/keys; a keeper wallet of its own, made as the keeper's user
    sudo -u colosseum-keeper /opt/colosseum-pools/venv/bin/python /opt/colosseum-pools/current/ops/make_demo_keys.py \
        --wallet keeper --dir /var/lib/colosseum-keeper/instances/demo3/secrets   # fund this address with HYPE
    # 4. the commit that holds deployments/testnet-v3-demo3.json: install.sh writes deployment-demo3.env
    #    and the /demo3/ locations
    ops/host/deploy.sh <commit> <user@host>
    # 5. start it
    sudo systemctl enable --now colosseum-gateway@demo3 colosseum-keeper@demo3
    curl -s https://pools-api.usenami.io/demo3/v1/health   # chain_id 998 and the commit
    cat /proc/sys/kernel/yama/ptrace_scope                 # 1: no attaching to another's process

A file in `/etc/colosseum` that names an instance badly (its name, its label, a port off
8788..8799 or one another instance has, a label the first or another instance already serves) stops
`install.sh` before nginx or any service is touched; so does a deployment, the first's or an
instance's, with no complete record in the release. Two keepers on one label would share its state
file.

The host's budgets are shared. Both gateways read the same node, and nginx's `pools_all` zone is the
same for every path, so together they stay under 15 orders a minute. Hyperliquid counts 1,200 of
weight a minute per IP: the two sweeps and the two keepers come out of it together (the table of
weights above, "the two together"): with one account on each and no order, about 252 a minute for demo2's and 504 for
demo3's, which reads xyz as well, 756 in all.

## Publishing the app

`deploy.sh` does not touch the app. A new version is a new deployment of the Worker, uploaded
in the Cloudflare dashboard:

1. Take `app/` from a commit on `origin/main`: `git archive <commit> app | tar -x -C <dir>`.
2. If the contracts were deployed again, first put the new record's addresses in
   `app/config.js` (`factory`, `registry`, `deployBlock`, `platformAssets`) and merge that. An
   app that reads contracts of another version fails on every page that reads them, with
   "could not decode result data".
3. Upload it as a new deployment — **drag the `app` folder itself into the upload box, not the
   files inside it**. Selecting the files loses the directories they live in, so `lib/`,
   `views/` and `data/` never arrive and every page fails on its first import; dragging the
   folder makes the browser walk it and keeps the paths. Done right the whole tree goes up; the
   top level is only a handful of the files. (Measured on 24 September 2026, when a deploy made
   this way arrived with four.)
4. Open `/`, `#/new`, `#/economics` and `#/verify/<a challenge>` on `pools.usenami.io`.

## Checking

- `curl -s 127.0.0.1:8787/v1/health` on the host: the chain, the signer mode and the number of keys.
- `journalctl -u colosseum-gateway`: one JSON line per request.
- `journalctl -u colosseum-keeper`: `pass_done` and `pass_failed` lines.
- The keeper's progress file changes on every pass. Older than five minutes means it stopped.
- Through Cloudflare: `https://pools-api.usenami.io/v1/health`.
- `head_read_failed` or `venue_incomplete` in the keeper's log: a read was refused, the pass went
  on without it. Neither is a failed pass; `pass_failed` is.

### What a refused read costs in time

A refused read makes its pass up to **63 seconds** longer: the backoff ladder is 1, 2, 4, 8, 16 and
32 seconds across seven attempts. A rate limit is counted over a minute, so a ladder that stops
inside one is not a backoff — the old one topped out at 31 seconds and a keeper pass died on
`eth_blockNumber: rate limited 6 times in a row` on 1 October 2026.

Separately, the keeper sleeps **30 seconds** between passes — after a pass ends, not between
accounts.

🔴 **Those two do not add up to a bound on how late a breach is found, and nothing here claims they
do.** A breach that starts just after its own account was read waits for every account behind it,
then for the next pass to come round to it; the node's answer takes time of its own; and how many
reads are refused is what the shared request budget decides. `ops/keeper.py` carries the first
number as `DELAY_ADDED_BY_A_REFUSED_READ_S` and a test holds this paragraph to it.

What a refusal does NOT cost is the rules the contract holds on its own — drawdown, daily loss and
leverage come off the margin precompile and are answered even when Hyperliquid refuses everything.
What it costs is the venue's half: the cancel list, the forbidden-asset candidates and the stop
check. The round comes back to that account before the ones it got through.

## The keepers' node, 16–18 October 2026

What happened, as it was read off the chain and the host:

- **17 Oct, 00:00–00:15 UTC.** The live challenge `0xe3fE…0dBB` got no day's snapshot. On the
  three days before, a keeper took it 4 to 18 seconds after midnight: on 14 and 16 Oct
  `0xD6F0…9021`, on 15 Oct `0xcbd5…fECa`. That night neither keeper sent anything at all. Their
  nonces stayed where the last snapshot left them, so not even a failed transaction went out.
  The contract refuses a snapshot after 00:15 (`OutsideCheckpointWindow`), so the day was lost.
  The daily-loss rule counted from the day before's snapshot until the next midnight.
- **17 Oct, 10:57 UTC.** `rpcs.chain.link/hyperevm/testnet` refused every call with
  `ErrUpstreamsExhausted` ("all upstream attempts failed"). Both keepers read it and nothing
  else, so both were blind together. The same node carries a keeper's `breach` and `expire`
  too: while it was down, nothing stopped an account that broke a rule. No account held a
  position then.
- **18 Oct, 12:35–13:19 UTC, by hand on the host.**
  - `hyperliquid-testnet.drpc.org` could not read the HyperCore precompile `0x…080F` ("failed
    to forward read precompile") and answered `eth_getLogs` with 408. The keeper reads every
    rule off those precompiles, so that node is no use to it.
  - `rpc.hyperliquid-testnet.xyz` read the pools and served `eth_getLogs` near the head, but
    timed old ranges out (`execution aborted (timeout = 5s)`).
  - So: `COLOSSEUM_RPC_URL` set to it in `/etc/colosseum/keeper.env`, and `--max-windows 5`
    added to the end of `ExecStart` in both units.
  - The scan was then standing at an old block it could not read past. `factory.pools()`
    listed 8 pools, and only `0x7006…e44A` had an exam, the one the keepers already followed.
    With that checked, `next_block` in both state files was moved to the head, 67235848.
  - Both keepers came back `active`, with clean passes and `next_block` at the head.

What it reopened. The gateway and the keepers read one node from one IP again. That is the
budget the two were separated to stop sharing (audit A-03, "Where things run" above). nginx
lets 15 requests a minute through to the gateway across everyone. At five reads an order
refused, that is up to about 75 reads a minute an outsider can spend, against the roughly 100 a
minute the node allows. Moving the keeper to a third node that serves the precompiles and old
logs, and the gateway's reads too, closes it again. None was found on 18 October.

What the code now does about a scan that cannot move (`ops/keeper.py`):

- Every `--sweep-every` passes (20 by default, about ten minutes), and on the first pass after
  a start, the keeper reads `pools()` from the factory. It asks each pool it does not follow for
  `stage()` and `challenge()`, and follows any that is not idle or has a challenge:
  `found_by_sweep`.
  - It asks about at most 40 unfollowed pools a sweep, and the next sweep carries on where that
    one stopped. Anyone can create pools for free, and this caps what that costs.
  - How soon a pool with a new exam is found, whatever the scan is doing: within one sweep (about
    ten minutes) while fewer than 40 pools on the factory are unfollowed; with more, it can take
    one sweep for every 40 of them ahead of it. An exam takes its first day's snapshot itself
    when it starts (`activate`), so a late find costs the next midnight's snapshot only if the
    exam started less than that long before midnight. More pools than that on the factory is
    the point at which `--sweep-every` wants lowering.
- When the scan stands on one block for 10 passes in a row while the head is past it, the keeper
  logs `scan_behind` with how far behind it is, and again every 10 passes after.

When the keepers' node is down for long:

1. **Read before you change anything.** On the host:
   - `systemctl status colosseum-keeper colosseum-keeper2`;
   - `journalctl -u colosseum-keeper -u colosseum-keeper2 --since "<UTC time> UTC"`. Look for
     `head_read_failed`, `pool_failed` and `scan_behind`;
   - `grep COLOSSEUM_RPC_URL /etc/colosseum/keeper.env`;
   - one `eth_blockNumber` to that URL with `curl`.

   From a laptop, the keepers' nonces against the last `DaySnapshot` show whether anything was
   sent. A failed send still uses a nonce; a send that never left does not.
2. **Try a replacement node from the host, never from a laptop alone.** A node has to answer
   all three of these, or the keeper is blind on it:
   - `eth_call` to a precompile — the account's margin, `0x…080F`;
   - `eth_getLogs` on a 50-block range near the head;
   - `eth_blockNumber`.
3. **Change `COLOSSEUM_RPC_URL` in `/etc/colosseum/keeper.env`,** keep a copy of the old file,
   and restart both units.
4. **If the scan cannot read the blocks it stands on,** check the factory's pool list first:
   every pool not idle or with a challenge must be among those followed. Only then move
   `next_block` to the head in both state files, each with its owner and mode kept, keepers
   stopped while you do it. On this commit the sweep finds such pools by itself, within the
   limits above. Moving `next_block` is still the step that brings the scan back: from the head
   it sees the next challenge in the block it is bought, instead of a sweep later, and
   `scan_behind` stops.
5. **Afterwards,** `pass_done` with `latest` near the head on both keepers, and `day()` of every
   active account equal to today after the next 00:15 UTC.

### Before mainnet: a node of the keeper's own (audit M-1)

On testnet the keeper and the gateway read Hyperliquid's node, `COLOSSEUM_RPC_URL` in keeper.env and
`GATEWAY_RPC_URL` in gateway.env the same URL. A node's budget is per IP, so a gateway busy with
orders leaves the keeper's stops and snapshots waiting, and the other way round. That is accepted on
testnet (the CTO, 20 October 2026). The keeper says so at its start (`node_shared_with_gateway`).
On mainnet it does not start on the gateway's node: keeper.env names a node of its own first. Which
provider answers the HyperCore precompiles and recent `eth_getLogs` for it is still to be chosen;
drpc's did not read the precompiles.

## If the host is lost

No service level is promised here; what follows is what the code makes true, so that whoever has
to recover knows which parts are a rebuild and which are gone.

**Nothing on chain is lost.** The pools, their capital, their rules and every record of what
happened live in the contracts. The host runs two processes that talk to them; it holds no
ledger of its own.

**The keeper's state is a cache.** `/var/lib/colosseum-keeper/keeper-<label>.json` holds
`next_block` and the pools it follows, and a keeper with no state file starts at the deployment
block and walks to the head — the catch-up above, and the reason to start a replacement before
you need it rather than after. Nothing is lost by deleting it; time is.

**The gateway keeps one thing in memory: the nonce book.** It refuses a nonce it has already
seen, and a request must expire within the next minute (`MAX_EXPIRY_MS`, 60 s). A restart
forgets those nonces, so a request captured in the last minute could be replayed once inside its
own expiry window. That is the whole exposure of a gateway restart; everything else it needs is
on disk or on chain.

**The keys are the part that does not come back.** `/var/lib/colosseum-gw/keys` holds the demo's
agent keys and `/var/lib/colosseum-keeper/secrets` the keeper's wallet. A lost keeper wallet is
replaced by making another and funding it — the keeper has no privileges, every call it makes is
open to anyone. Lost agent keys are worse only in that nobody can trade those accounts any more;
they cannot be stolen from a disk that is gone.

**Getting capital out of an account whose key is lost** goes through `recut(salt)`, which
replaces the agent with an address nobody holds, and then the ordinary settlement. `recut`
requires the account to be **stopped**, so the order matters and there is an honest edge:

- a **funded stage** can always be ended by the pool's investor (`stopFunded`), so there is no
  wait;
- a **challenge** that is inside its rules and before its deadline can be ended only by its
  trader (`forfeit`). If both the gateway's keys and the trader are gone, the capital waits for
  the challenge's own deadline, when `expire` opens to anyone. On the demo's terms that is seven
  days from the start.

That wait is a property of the contracts, not of the host, and no amount of hosting fixes it.

## By hand, and by whom

- Cloudflare (Alex, in the dashboard):
  - the `pools-api` DNS record, proxied, to the host's address;
  - the Worker that serves the app, and each new deployment of it;
  - that Worker's `workers.dev` address, turned off, so the app has one public name;
  - the Origin CA certificate from the CSR above.
- The SSH rule: the operator's current address, dated in the rule's description. A rule
  whose address the provider has since changed is removed.
