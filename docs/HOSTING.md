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
  `pools-api.usenami.io`, behind Cloudflare. While the gateway does not listen (a restart), nginx
  answers for it from `127.0.0.1:8786`: 429 `gateway_restarting`, `sent: false`,
  `Retry-After: 30` (docs/GATEWAY.md, Answers). An order the gateway took and then lost still gets
  nginx's 502.
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
| `/etc/colosseum/deployment.env` | written by `install.sh` from that label's record: label, factory, registry, and the gateway's limits where the record has them; read after `gateway.env` and `keeper.env`, so it wins | root, 644 |
| `/etc/colosseum/tls/origin.key` | TLS key for `pools-api.usenami.io`, made on the host | root, 600 |
| `/etc/colosseum/tls/origin.csr`, `origin.crt` | its request and Cloudflare's Origin CA certificate, both public | root, 644 |
| `/var/lib/colosseum-gw/keys` | the demo's agent keys, one `*.key` file each | `colosseum-gw`, 700 and 600 |
| `/var/lib/colosseum-keeper/secrets` | `keeper.key` and `keeper.addr`, the keeper's gas wallet | `colosseum-keeper`, 700 and 600 |
| `/var/lib/colosseum-keeper/keeper-<label>.json` | the keeper's progress; rebuilt from the chain if lost | `colosseum-keeper` |
| `/etc/colosseum/shared-keeper.env` | the shared pool its keeper serves, its key directory, its node; no secret | root, 644 |
| `/var/lib/colosseum-keeper/shared/secrets` | `shared-keeper.key` and `.addr`, the shared pool keeper's gas wallet | `colosseum-keeper`, 700 and 600 |
| `/opt/colosseum-watch/releases/<sha>` | the watchdogs of one release, copied by `install.sh` from `ops/host/watchdogs` | root, read-only to the watchdogs |
| `/opt/colosseum-watch/current` | link to the newest of them; the watchdogs' units run from it, never from a pools release | root |
| `/var/lib/colosseum-watch` | one `<name>.alarm` file for each check that has a finding standing | `colosseum-watch`, 700 |
| `/etc/colosseum/watch.env` | optional: the alert command and node overrides for the watchdogs; written by hand if at all | root, 644 |

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
2. From the operator's machine: `ops/host/deploy.sh <commit> <user@host>`, which also builds the venv.
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

   A stopped account whose position cannot be closed no longer costs a settle step every pass.
   A step closes with a reduce-only IOC priced 5 % off the mark (`CLOSE_SLIPPAGE_BPS`), and a thin
   testnet book often has nothing there: on demo3 on 7 Nov 2026 the keeper sent one every 35 s,
   151 of 174 were rejected, about 0.001 HYPE of gas an hour. Now, before a step on an
   open position, it reads the book (`l2Book`, weight 2 a coin). With nothing inside the band it
   sends nothing and logs `settle_held` with the levels and the time of its next look; after a step
   that left the position as it was, it waits twice as long as before (`settle_backoff`), up to an
   hour. A changed position, or a level inside the band, puts it back on every pass
   (`settle_resumed`). A flat account is never held back.

       sudo journalctl -u colosseum-keeper -n 200 --no-pager | grep -E '"settle_(held|backoff|resumed)"'

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
- checks the release's watchdogs and puts them in place, before it switches ("The host's watchdogs");
- switches `current`;
- restarts the services that are enabled, then runs each watchdog once and prints what it said;
- keeps the three newest releases.

The deployment record the services use has to be committed: `install.sh` reads it from the
release.

Without ssh, through AWS Systems Manager:

    COLOSSEUM_SSM_BUCKET=<bucket> ops/host/deploy-ssm.sh <commit> <instance id>

The same gate, the same archive and the same `install.sh`. The archive is about 2.4 MB and one Run
Command carries at most 64 KB, so it goes through a private S3 bucket, under a key of that run, and a
presigned link. The host checks the archive's sha256 before it unpacks it. The agent has 10 minutes
to take the command (`--timeout-seconds`, else SSM ends it as TimedOut), the link lives those 10
minutes and curl's 5, the install has 25 minutes, and the script waits for the sum and 2 more, the
wait counted from before the link is made. The object is removed once the command has ended. If the
command may still run (the wait ran out, its status could not be read, send-command failed after the
request went out, the script was stopped), the script leaves the object and says where; only when the wait runs out does it
also ask SSM to cancel the command. It needs
the instance's role with `AmazonSSMManagedInstanceCore` (no S3 permission) and the SSM agent
registered; the operator's aws CLI needs `s3:PutObject`, `s3:GetObject` and `s3:DeleteObject` on the
bucket, `ssm:SendCommand`, `ssm:GetCommandInvocation` and `ssm:CancelCommand`. The script prints what
the host printed and, on a failure, the reason SSM gives in `StatusDetails`.

### How the host is reached, 5 November 2026

There is one way to reach the host: ssh, with the operator's key, from an address in the security
group's rule for port 22. `deploy.sh` uses it, and so do the checks after a release. Nothing on the
host pulls releases by itself. A timer that would install whatever commit a tag names was weighed on
2 November and dropped on 3 November, because it would let anyone who can move a tag in this public
repository run code as root on the host.

The operator's address is a home connection, and its provider changes it. When that happens, port 22
is closed to the operator and no release can go out until someone with rights in the AWS console adds
the new address to the rule. On 5 November 2026 one release went out at 13:00 UTC. By the evening the
address had changed, and the next release waited for the rule. Before a release, check the road
first: `nc -z <host> 22`. A timeout there usually means the rule, or another filter between the
operator and the host. A refusal means the road is open and the host turns the connection away:
look at `sshd` and the host's own firewall, not at the rule.

`deploy-ssm.sh` above is the way around that, but on 5 November 2026 it has nowhere to run:

- the instance has no instance profile, so no role;
- the SSM agent ships with Amazon Linux 2023 and was running when last read, on 3 November. It is
  not registered, because it has no role to register with: `aws ssm describe-instance-information`
  lists nothing for the instance;
- there is no bucket for the archive.

To set it up, as decided on 3 November 2026, the AWS account's owner does three things by hand in the
console. Each one grants a permission.

1. A role that EC2 may assume, with only the managed policy `AmazonSSMManagedInstanceCore`, and its
   instance profile attached to the instance (EC2 → the instance → Actions → Security → Modify IAM
   role). Within minutes the agent shows up in `aws ssm describe-instance-information`.
2. A private bucket in us-east-1, with public access blocked. Its name lives only in the operator's
   environment, as `COLOSSEUM_SSM_BUCKET`.
3. For the operator's credentials, the permissions listed above: on that bucket, and the three SSM
   calls.

After that, `deploy-ssm.sh` can replace `deploy.sh`. Keep the port-22 rule until one release has gone
through `deploy-ssm.sh` from end to end: the agent is registered and answers, the host fetches the
archive from the bucket, and the release installs and passes its checks. Removed earlier, a break on
either road, to SSM or to the bucket, leaves the operator with no way in.

The checks after a release then read the host through SSM. The public `/v1/health`, and
`scripts/verify-gateway-deploy.sh`, which reads it, say how the gateway is and nothing about the
keeper: its journal (`pass_done`, `pass_failed`), its progress file and its read failures can be read
only on the host, through SSM once the rule is gone.

### What the record sets

A deployment whose record has `gateway_limits` (`deployments/testnet-v3-demo3.json` since 10 November 2026)
takes its gateway's limits from the release instead of from a line by hand. `install.sh` writes
`/etc/colosseum/deployment-<name>.env` (`deployment.env` for the first instance) from
`python -m ops.host_instances deployment-env <label>` at every deploy, and that file now carries, besides the
addresses, `GATEWAY_SIZE_CAPS`, `GATEWAY_LEVERAGE_CAPS` and `GATEWAY_PERP_DEXES` for the gateway and
`COLOSSEUM_PERP_DEXES` for the keeper, all from the record's list of perps (name, order number, size per
order, leverage cap) and builder dexes.

**The record wins.** The gateway and keeper units read the record's file after the instance's own
`gateway-<name>.env` / `keeper-<name>.env`, and systemd keeps the last value it reads: a line by hand for one
of these variables is ignored once the record has `gateway_limits`. A perp is added, taken off or given
another cap by a change to the record, its review and a deploy, not by an edit on the host. A deployment
whose record has none, as demo2's today, renders the addresses alone, and its `gateway.env` still says its
limits as below. `ops/tests/test_host_instances.py` holds the rendered lines to what the gateway reads and
each leverage cap to the default `ops/leverage.py` gives the same market.

**The keeper's nodes come from the record too** (`keeper_nodes`, demo2's and demo3's since 11 November 2026):
the node the keeper reads everything from and its scan's window, `COLOSSEUM_RPC_URL` and `COLOSSEUM_LOG_WINDOW`
(Hyperliquid's, 50 blocks), and the second node its scan takes a window to when the first refuses it,
`COLOSSEUM_RPC_BACKUP_URL` and `COLOSSEUM_BACKUP_LOG_WINDOW` (thirdweb's, 1000 blocks). They are rendered into
the same file, so keeper.env's `COLOSSEUM_RPC_URL` by hand is overridden. The record is public: a node whose
address carries a key does not go in it, and `deployment-env` refuses an address with anything but https, a
host and a path. A deploy of this needs nothing on the host by hand: `install.sh` renders the file and
restarts the keepers.

demo3's record says what its gateway had by hand on 10 November 2026, and xyz:GOLD besides:

    GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4", "750000": "0.015", "750003": "0.1"}
    GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5", "750000": "8", "750003": "20"}
    GATEWAY_PERP_DEXES=xyz
    COLOSSEUM_PERP_DEXES=xyz

🔴 **By hand before the first release with HIP-3 (the platform's list as a setting, 21 October
2026), for a deployment whose record has no `gateway_limits`:** the demo signer no longer holds the platform's perps in code. It reads them, with a size
cap for each, from `GATEWAY_SIZE_CAPS` in `/etc/colosseum/gateway.env`, and the gateway refuses to
start without it rather than sign nothing. `bootstrap.sh` writes that file only when it is missing,
so a running host needs the line added before the restart. The testnet's list and caps since
17 September, BTC, ETH and SOL:

    GATEWAY_SIZE_CAPS={"3": "0.005", "4": "0.15", "0": "4"}
    GATEWAY_PERP_DEXES=

A perp of another dex (HIP-3) goes on the list by its order number, and its dex's name into
`GATEWAY_PERP_DEXES`, for example `xyz` with `"750000": "0.015"` for `xyz:XYZ100` on testnet, or
`"750003": "0.1"` for `xyz:GOLD` (about 400 USDC at a mark near 4,000; 110003 on mainnet).
Adding or taking a perp off is a change to this file and a restart, not a release (for a deployment
whose record has `gateway_limits`, a change to the record and a deploy: above). The keeper reads
the same dexes from `COLOSSEUM_PERP_DEXES` in `/etc/colosseum/keeper.env` (comma-separated, none by
default): with `xyz` there it sees an account's positions and orders on xyz, checks their stop and
names them to a stop, and logs `hip3_open_over_weekend` for a position still open from Friday 16:30
New York time. It holds no key and closes nothing itself.

Each dex costs Hyperliquid weight on the host's one budget of 1,200 a minute per IP. Weights as
Hyperliquid publishes them: `clearinghouseState` and `allMids` 2, `metaAndAssetCtxs`, `meta`,
`frontendOpenOrders` and `perpDexs` 20. For one live account, worked out from those weights (the counts
the gateway and the keeper now keep themselves are the measure, docs/GATEWAY.md, "The host's minute"):

| | Hyperliquid's own dex | with xyz |
|---|---|---|
| gateway, an order that opens (markets 20 + state 2 + orders 20, a dex) | 42 | 84 |
| gateway sweep, every 30 s (markets 20 + state 2, a dex; the orders 20 every pass on a dex with a position, once a minute on one without) | 84 a minute | 148 a minute |
| keeper, every 30 s (meta 20 + state 2, a dex; the orders the same way) | 84 a minute | 148 a minute |
| the two together | 168 a minute | 296 a minute |

The account holds a position: on Hyperliquid's own dex in the first column, on xyz alone in the second,
as demo3's challenge did until it was stopped on 7 November 2026. Each further account holding a
position adds, on its dex, 22 to every sweep and every keeper pass: 88 a minute. An account holding
none has its orders read once a minute, or when its positions changed: 24 a minute to each that reads it.
Before 5 Nov 2026 the sweep ran every 15 seconds and both read every account's orders at every pass: 252
and 504 a minute for the same account.

A stopped account can keep a position no settle step closes. demo3's challenge was stopped on
7 November 2026 at 17:43:37 UTC with a short of 0.0004 `xyz:XYZ100`; at 12:40 UTC on 8 November 0.0001
of it was still open. A settle step closes 5 % off the mark, and the book's only ask, 33,000, was
further off than that. By then 963 closes had gone out since the stop: one filled 0.0003 at 28,579
(17:44:13 UTC), 933 found nothing to fill against, 28 were refused for a limit too far from the oracle
and one was still being answered (Hyperliquid's `historicalOrders` and `userFillsByTime` for the
account). Such a position closes once a level comes inside the band. Until then a keeper with the
hold-back in step 4 above sends no step and logs `settle_held`.

🔴 **By hand before the first release with the leverage check:** each perp's own leverage cap, by
order number, in the same file. The gateway refuses to start without it, and with the demo signer
when it leaves out a perp `GATEWAY_SIZE_CAPS` lists. The caps are the platform's defaults, the ones
`ops/leverage.py` gives a pool's rules (`ASSET_LEVERAGE_X100`): BTC 10x, ETH 10x, SOL 5x, HYPE 10x,
SP500 10x, XYZ100 8x, CL 3x, and xyz:GOLD 20x, which is the leverage rule v1.3's number for it
(2 November 2026). BRENTOIL has none: withdrawn by the leverage rule v1.3 (2 November 2026), a pool
that names it is refused, and it has no line here. A perp left out here opens nothing on the gateway
(`no_leverage_cap`). On testnet, for the list above:

    GATEWAY_LEVERAGE_CAPS={"3": "10", "4": "10", "0": "5"}

with `"750000": "8"` and `"750003": "20"` when `xyz:XYZ100` and `xyz:GOLD` go on the list. On
mainnet the same perps have other numbers (BTC 0, ETH 1, SOL 5, HYPE 159; xyz is dex 1, so
`xyz:XYZ100` is 110000, `xyz:CL` 110029, `xyz:SP500` 110052, read on 19 and 21 October 2026;
`xyz:GOLD` 110003, read on 10 November 2026; `xyz:BRENTOIL`, 110049, is withdrawn and left out):

    GATEWAY_LEVERAGE_CAPS={"0": "10", "1": "10", "5": "5", "159": "5", "110052": "10", "110000": "8", "110029": "3", "110003": "20"}

A cap is a ceiling the platform sets; the pool's own leverage rule still holds the whole account.

Flat over the weekend is every pool's default: from Friday 16:00 New York time the gateway sends
reduce-only IOC closes for the HIP-3 positions of its accounts, at every pass until the close at 17:00
and at most every five minutes after it, and takes off their resting HIP-3 orders, both until Sunday
18:05 (docs/GATEWAY.md). The closes fill only into
a book with a level near the mark: on testnet on 6 November 2026 none of 202 did, and the position
stayed open over the weekend (docs/GATEWAY.md has the numbers). A pool that keeps them over the
weekend is named, by address, in the same file; none is unless set. `xyz:GOLD` is closed on Friday
on every pool, named here or not (`ALWAYS_FLAT_OVER_WEEKEND` in `gateway/assets.py`):

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

After a deploy, from the operator's machine:

    ./scripts/verify-gateway-deploy.sh

It asks `/v1/health` which commit the gateway runs, checks that the commit is on `origin/main`, and
runs on that commit the tests of the leverage check before the signature; its last line is PASS or
FAIL. It reads and runs tests only; it changes nothing on the host.

## Choosing the deployment and starting

    echo rehearsal | sudo tee /etc/colosseum/deployment      # on the host
    ops/host/deploy.sh <commit> <user@host>                   # from the operator's machine
    sudo systemctl enable --now colosseum-gateway colosseum-keeper

`install.sh` refuses a label without a complete testnet record in the release.

A mainnet deployment is planned, not sent, from the operator's machine, with no key:
`spike/.venv/bin/python ops/deploy_v3.py --label <label> --network mainnet --dry-run --deployer <the address the
owner gives>` prints every address, the perp numbers, the gas and the record it would write
([DEPLOYMENTS.md](DEPLOYMENTS.md), What stays on testnet).

`deployments/LIVE.json` also records what the host serves ([DEPLOYMENTS.md](DEPLOYMENTS.md)). When the host starts
or stops serving a deployment, update its entry there.

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
| gateway | `127.0.0.1:8787`, `/v1/order`, `/v1/health` | `127.0.0.1:8788`, `/demo3/v1/order`, `/demo3/v1/health`, `/demo3/v1/faucet` |
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
    # 3. its keys: the agent key files from the operator's machine, owned by colosseum-gw, mode 600, into
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
weights above, "the two together"): with one account holding a position on each and no order, 168 a
minute for demo2's and 296 for demo3's, which reads xyz as well, 464 in all.

**How many traders at once.** Worked out from Hyperliquid's published weights until the host is read
(below). Each demo2 account that holds a position costs 88 a minute: 44 to the sweep and 44 to the
keeper, 2 for its positions and 20 for its orders at every pass of each; its orders are read every
time because a stop the exchange took off, with the position as it was, shows only in a list read
now (docs/GATEWAY.md, "The sweep"). An active account with nothing open costs the keeper's 24 alone,
and the sweep lets it go; a hot account (docs/GATEWAY.md, "The sweep") costs 44 more if it holds a
position and 4 if not, and the marks of the hot passes 40 once. On 5 Nov 2026 demo2 had one active
account with nothing open and demo3 one with a position on xyz, so m demo2 accounts holding positions
cost 400 + 88m a minute: nine are 1,192, with no order (ten are 1,280); four are 1,192 with five
opening orders a minute (88 each, the read two seconds after the order included; five are 1,280).
Before 5 Nov 2026, when every pass read every account's orders and the sweep ran every 15 seconds, the
same was 668 + 132m: four accounts with no order (1,196), none with five opening orders a minute (one
is 1,240). A second keeper on the host (`colosseum-keeper2`, above) adds its own 40, and 24 an active
account holding nothing or 44 one holding a position; it is to be turned off. Past the 1,200, Hyperliquid refuses the host's reads: an order is refused before it is
sent (`sent: false`) and a sweep tries again at the next one, so a stop that a sweep owes a position comes
that much later. nginx's 15 orders a minute are not what binds first.

To read the host instead: `/v1/health` of each gateway says `weight_per_min` (the last minute's weight
it sent, by request type) and `accounts_with_position`, and each gateway and keeper writes the same to its
log once a minute or once a pass:

    sudo journalctl -u colosseum-gateway -u colosseum-gateway@demo3 -u colosseum-keeper -u colosseum-keeper@demo3 \
        --since -10min --no-pager | grep weight_per_min | tail -8

The host's minute is the sum of the four latest `total`s. Each process counts what it sent, refused or not,
as Hyperliquid counts it (gateway/weight.py).

### The demo3 faucet

The CTO's decision of 27 October 2026: testers from outside take one exam's dUSDC and its gas from
`/demo3/v1/faucet` (`gateway/faucet.py`, and docs/GATEWAY.md for the request). The wallet is the one
the demo3 token lets mint, `0xBF05…5F3a` (`faucet` in `deployments/testnet-v3-demo3.json`). Until its
key is set the path answers 404 `no_faucet`, so the code can go out first.

    # 1. the key, from the operator's machine, owned by colosseum-gw, mode 600, NOT into keys/:
    #    the demo signer would take a key there for an agent's
    sudo install -m 600 -o colosseum-gw -g colosseum-gw faucet.key /var/lib/colosseum-gw/instances/demo3/faucet.key
    # 2. one line in /etc/colosseum/gateway-demo3.env (GATEWAY_FAUCET_USDC, _HYPE and _PER_IP change
    #    the grant, 1.7, 0.001 and 3 unless set)
    GATEWAY_FAUCET_KEY_FILE=/var/lib/colosseum-gw/instances/demo3/faucet.key
    # 3. HYPE on the faucet's wallet, by the deployment's owner: a grant takes 0.001 and about 0.000012 for its own gas
    # 4. the restart, after 30 minutes' notice to the traders' operator
    sudo systemctl restart colosseum-gateway@demo3
    curl -s https://pools-api.usenami.io/demo3/v1/health   # "faucet": {"usdc": "1.7", "hype": "0.001", "per_ip": 3}

The gateway will not start with a key that is not the record's faucet, or one the token on chain does
not mint for. The day's grants are kept in `/var/lib/colosseum-gw/faucet-demo3/state.json`, which
systemd makes for the unit (`StateDirectory`): the only place a gateway writes. Every grant that sent
anything is a line of `grants.jsonl` beside it: the address, the time, the amounts and the two
transactions. A client's address is kept only as a hash salted for the day.

Pool owners' contacts (`gateway/contacts.py`) are kept by each gateway in a directory systemd makes for
its unit: `/var/lib/colosseum-gw/contacts` for the first deployment's (`colosseum-gateway.service`, the
one place it writes; the site reads it), and `/var/lib/colosseum-gw/contacts-<name>` for an instance's,
beside its faucet's. Every record kept is a line of `contacts.jsonl` there, never rewritten, and a pool's
last record is read back from it at the start; what a crash left after the last whole line is written
over by the next record. `/v1/health` says `"contacts": true` once it is on. A gateway without such a
directory answers 404 `no_contacts_here`; `GATEWAY_CONTACTS_DIR` names one for a gateway run by hand. A
faucet never takes the contacts' directory. nginx sends `/v1/contacts` and `/<name>/v1/contacts` to the
gateways: a read costs the node nothing and is limited by a network at 60 a minute; a write by a network
at 2 a minute and by everyone together at 6, since each may cost the node a read.

## The shared pool's keeper

`colosseum-shared-keeper.service` runs `ops/shared_keeper.py` for the shared pools that
`/etc/colosseum/shared-keeper.env` names: `COLOSSEUM_SHARED_DEPLOYMENT` is one label, or several joined
by commas with no space, such as `shared-demo2b,shared-demo2c`. One process keeps them all with the one
key. A pass goes through the pools one after another, so their calls never race each other for the key's
nonce, and a pass that breaks off on one pool is logged with that pool's address while the next pool's
still runs. `install.sh` checks every label before a release switches: each a complete record with a
`SharedPool`, none named twice. Every call the keeper makes is open to anyone, and each is tried with
`eth_estimateGas` first:
- `armSeat`, for an idle seat whose capital a challenge has used: each sale spends 1 USDC of the
  seat's spot on the challenge's new account, and a loss spends more;
- `noteFunded` and, once the seat's published term has run, `endFundedTerm`, which ends a funded
  stage. Nobody else ends it on time;
- `settle` for a ticket that holds a deposit, and for the withdrawal queue;
- `releaseSeat` while the queue needs more than the pool has free.

The seats' challenges are pools of the factory, and `colosseum-keeper.service` already activates,
checks and stops them. Without this unit, everything in the list above is done by hand.

It sends with a key of its own, in `/var/lib/colosseum-keeper/shared/secrets`: the first keeper's would
race it for nonces. All the keepers run as `colosseum-keeper`, so the units keep the keys apart both ways:
this one cannot read the first keeper's key or an instance's, and neither of those can read or replace its
own (`InaccessiblePaths` in each). It keeps no state and writes nothing. A pass on a pool of three seats reads the node
about forty times (each seat's stage, challenge, need, readiness and spot, twice; the queue; the closed
tickets), so the unit passes every 300 seconds, about eight reads a minute. Each pool adds its own: a pool
of ten seats reads about 120 times a pass, so a pool of three and a pool of ten together are about 160 a
pass, about 32 a minute. Every 30 seconds would be
about eighty, most of the minute of a node that allows about 100 (`nginx-pools-api.conf.in`) and that
the keepers watching the rules read too. Five minutes late is nothing for a top-up, a term of a day or a
deposit; a depositor can also run a settlement point from the site.

Once, on the host:

    sudo bash /opt/colosseum-pools/current/ops/host/bootstrap.sh   # makes the directory and shared-keeper.env if missing
    cd / && sudo -u colosseum-keeper /opt/colosseum-pools/venv/bin/python \
        /opt/colosseum-pools/current/ops/make_demo_keys.py --wallet shared-keeper --dir /var/lib/colosseum-keeper/shared/secrets
    # testnet HYPE on the address it prints (the deployment's owner): a call fits a small block, 2M gas, 0.0002 HYPE at 0.1 gwei
    sudo systemctl enable --now colosseum-shared-keeper

`bootstrap.sh` writes `shared-keeper.env` only when it is missing, and the line it writes names both
shared pools, `shared-demo2b,shared-demo2c`: the pool the site takes deposits into and the earlier one,
whose holders its points still pay. A host set up earlier keeps the line it has, so a pool added later is
added to that line by hand -- the label after a comma, nothing else on the line, then
`sudo systemctl restart colosseum-shared-keeper` -- which is how `shared-demo2c` joins the host that was set
up for the first pool. A release does not touch the line; `install.sh` checks it as it stands.

`install.sh` installs the unit with the others and restarts it with them when it is enabled. Before
anything switches, it refuses a release when `shared-keeper.env` does not name the pool on one line of its
own, `COLOSSEUM_SHARED_DEPLOYMENT=<label>` with nothing around it (no spaces, quotes or comment), or when
that pool's record is missing or has no `SharedPool`: the label it checks is then the one systemd passes.

## The host's watchdogs

A service that is "active" with its logs going says nothing about the work being done: on 25 September 2026 a
keeper had followed no pool for days and fallen 175 thousand blocks behind with nobody told. So two checks
look at the RESULT, from outside the keeper's and the gateway's own files, through public chain reads only.
Each runs every quarter of an hour as a systemd timer, as its own unprivileged user `colosseum-watch`, which
cannot see `/var/lib/colosseum-keeper` or `/var/lib/colosseum-gw`:

| unit | what it looks at | when |
|---|---|---|
| `colosseum-watch-keys` | the free agent keys of the registry: their count against the threshold (16, strict: the alarm is at 15), and for each free key whether its address has gained an account on HyperCore, which spoils it | `*:00/15` |
| `colosseum-watch-keeper` | for each live account of the factory, what the keeper is bound to do: stop a breach, close at the deadline, activate within 15 minutes of the purchase | `*:07/15` |
| `colosseum-watch-keys-demo3`, `colosseum-watch-keeper-demo3` | the same two, for the instance demo3 | `*:03/15`, `*:11/15` |

The files are `ops/host/watchdogs`: `keys_watch.py`, `keeper_watch.py`, their one retry ladder
`watch_backoff.py`, the wrapper `watchdog-run.sh`, and a service and a timer for each unit.

**They come with the release, and nothing is put on the host first.** `install.sh` checks that the release
carries them, copies them to `/opt/colosseum-watch/releases/<sha>`, compares the copy with the release's own
directory, installs the units, runs `daemon-reload`, points `/opt/colosseum-watch/current` at the new copy and
restarts the timers, all before it switches `current` of the pools: a release that cannot carry its watchdogs
does not switch. The units name `/opt/colosseum-watch/current`, which is never inside a pools release, so a
rollback to a release from before the watchdogs leaves them running. An instance gets its pair when the release
ships units named for it and `deployment-<name>.env` is on the host; the first deployment's pair needs
`deployment.env`. What an earlier, hand-made install left in `/opt/colosseum-watch` (`watchdog-run.sh` and
`watchdogs/`) is removed, and the user `colosseum-watch` is made if it is missing. After the services restart,
`install.sh` starts each watchdog once and prints the last line it said: a node that cannot be read shows up in
the deploy's output, as a warning that never fails the release.

**What they read.** The registry (`GATEWAY_REGISTRY`) and the factory (`GATEWAY_FACTORY`) come from
`/etc/colosseum/deployment.env`, or `deployment-demo3.env`, which `install.sh` writes from the deployment's
record and the services read: a watchdog looks at the deployment the host serves, not at whatever the record on
a branch says today, and it needs no address of its own. Neither has a default; with none named the check
exits 3. The nodes: the keys check reads `998.rpc.thirdweb.com` and falls back to Hyperliquid's node
(`POOLS_WATCH_KEYS_RPC`, `POOLS_WATCH_KEYS_RPC_FALLBACK` in `watch.env` move them); the keeper check reads
Hyperliquid's node (`POOLS_WATCH_RPC`) and falls back to the keeper's backup node,
`COLOSSEUM_RPC_BACKUP_URL` of the deployment's env file when the record names one (`POOLS_WATCH_RPC_FALLBACK`
overrides it). No node is written into the keeper check as its fallback: a record that names none leaves it on
its first node alone, and an exhausted day's allowance there ("More than 3000 archived blocks queried in one
day", which a pause does not lift) is then exit 3. A node that is merely busy gets the ladder of 63 seconds, a
node that is broken or out of allowance is left for the fallback at once, and the output says it read the fallback.

**What a run says.** `watchdog-run.sh` sorts the check's exit code and does nothing else:

| code | journal | flag `/var/lib/colosseum-watch/<name>.alarm` | the unit |
|---|---|---|---|
| 0, quiet | `quiet: ...`, and `RECOVERED` if a flag stood | removed | succeeds |
| 1, a finding | `ALARM` and the finding | written, with the time | succeeds (`SuccessExitStatus=1`) |
| 3, could not check | the reason, "the flag is untouched" | not touched either way | fails |
| any other | `UNKNOWN CODE` | not touched | fails |

Code 3 leaves the flag alone on purpose: "could not check" is neither "all is well" nor "all is wrong", and
clearing a standing alarm out of ignorance or raising a false one is each worse than saying so. A check that
crashes is 3 too, never a finding. A read that fails AFTER a finding does not take the finding with it: it is
reported with an `INCOMPLETE` line (the pools or keys not reached) and the exit is 1; with no finding made, the
same failure is 3. A flag that cannot be written (a full disk, say) leaves the one that stood as it was, and the
wrapper exits 4 after it has put the finding in the journal and sent it to the channel, so the unit fails. There is no alert channel on the host: an alarm is the flag and the journal
line, and the wrapper says so on every alarm. A channel is one line in `/etc/colosseum/watch.env`,
`COLOSSEUM_ALERT_CMD='...'`, which the units read (it gets the finding on stdin, and a command that fails is
logged as not having taken it).

Reading them, on the host:

    systemctl list-timers 'colosseum-watch-*'            # when each runs next
    journalctl -u colosseum-watch-keys -n 20 --no-pager  # what the free-key check said
    ls -l /var/lib/colosseum-watch                       # a file here is an alarm that stands

To take them off: `systemctl disable --now colosseum-watch-*.timer`, then remove `/etc/systemd/system/colosseum-watch-*`
and run `systemctl daemon-reload`. The state directory is left, because a flag in it may be a live alarm.

## Publishing the app

`deploy.sh` does not touch the app. A new version is a new deployment of the Worker, uploaded
in the Cloudflare dashboard:

1. Pack `app/` of a commit on `origin/main`: `./scripts/pack-site.sh <commit>`. It copies the
   folder file by file from the commit's blobs into `~/Downloads/pools-app-<first 8 of the commit>`,
   checks every file against its blob, adds `build.json` with the commit, and writes beside the
   folder, not into it, `pools-app-<first 8>.sha256`: a sha256 for every file of the pack.
2. If the contracts were deployed again, first give the new record its entry in
   `deployments/LIVE.json`, write `app/config.js` again with `python3 scripts/app_config.py --out
   app/config.js` (docs/DEPLOYMENTS.md) and merge that. An
   app that reads contracts of another version fails on every page that reads them, with
   "could not decode result data".
3. Upload it as a new deployment — **drag the `pools-app-<first 8>` folder itself into the upload
   box, not the files inside it**. Selecting the files loses the directories they live in, so
   `lib/`, `views/` and `data/` never arrive and every page fails on its first import; dragging the
   folder makes the browser walk it and keeps the paths. Done right the whole tree goes up; the
   top level is only a handful of the files. (Measured on 24 September 2026, when a deploy made
   this way arrived with four.)
4. Open `/`, `#/new`, `#/economics` and `#/verify/<a challenge>` on `pools.usenami.io`.
5. Check the site against the pack:
   `python3 scripts/verify-live.py ~/Downloads/pools-app-<first 8>.sha256 https://pools.usenami.io`.
   It reads every file of the list from the site and exits 0 only if all of them match. A file
   that differs or cannot be read is named, and the exit is 1. Cloudflare inserts two tags into
   `index.html` before `</body>`, its challenge script and its Web Analytics beacon. The check cuts
   those two, each only in the exact form Cloudflare serves it, where the ids and tokens are the only
   parts that vary, and nothing else: anything more on the page is a mismatch, and so is a new form of
   the snippet until the pattern in the script follows it. `scripts/test-verify-live.sh` holds it to
   that on a copy of the site on disk.

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
- A window the keeper's node refuses, by any error, a timeout or a status, goes once to the second node
  (`keeper_nodes`, above) in that node's own window, and the rest of the pass reads the second node:
  `scan_on_backup`. A scan further behind the head than one pass of the keeper's node reads (5 windows of
  50) catches up on the second node, 1000 blocks a call. Neither node is tried twice for a window, and the
  scan never waits out the 63-second ladder of every other read; a 429 from the second node is waited out 2
  seconds at a time, 10 at most a pass. Both refusing: `scan_stopped`, and the next pass tries again.
  Caught up means within one window of the head with every block before it read; the keeper says so once,
  `scan_caught_up`. Measured 11 Nov 2026: Hyperliquid's testnet node has no blocks 69038325 to 69071492 (8
  Nov, 02:49:51 to 11:54:26 UTC), refuses any window in them with `invalid block range`, and the scan stood
  on 69038325 from then; thirdweb's has them, and the whole 280,643 blocks from there to the head held no
  `ChallengeCreated` of either factory.

When the keepers' node is down for long:

1. **Read before you change anything.** On the host:
   - `systemctl status colosseum-keeper colosseum-keeper2`;
   - `journalctl -u colosseum-keeper -u colosseum-keeper2 --since "<UTC time> UTC"`. Look for
     `head_read_failed`, `pool_failed` and `scan_behind`;
   - the node each keeper reads: `grep COLOSSEUM_RPC /etc/colosseum/deployment.env
     /etc/colosseum/deployment-demo3.env`, the record's (`keeper_nodes`), which systemd reads last; only
     for a deployment whose record has none, `grep COLOSSEUM_RPC_URL /etc/colosseum/keeper.env`;
   - one `eth_blockNumber` to that URL with `curl`, and to the second node's.

   From a laptop, the keepers' nonces against the last `DaySnapshot` show whether anything was
   sent. A failed send still uses a nonce; a send that never left does not.
2. **Try a replacement node from the host, never from a laptop alone.** A node has to answer
   all three of these, or the keeper is blind on it:
   - `eth_call` to a precompile — the account's margin, `0x…080F`;
   - `eth_getLogs` on a 50-block range near the head;
   - `eth_blockNumber`.

   `998.rpc.thirdweb.com`, tried from a laptop on 11 Nov 2026, answered all three (`markPx` `0x…0806` the
   same as Hyperliquid's node), served logs back to demo2's deployment block and through the blocks
   Hyperliquid's lacks, and took windows of up to 1000 blocks; wider ones it refuses with `-32005` ("Log
   response size exceeded"), the code a rate limit has elsewhere. About five calls in a row went through,
   then HTTP 429 for a few seconds. How much its free use allows a day or a month is not known. It is the
   keepers' second node in the records; from the host, it answered a read at the head on 10 Nov 2026.
3. **Change the node where it is set.** For a deployment whose record has `keeper_nodes` (demo2 and
   demo3 since 11 Nov 2026), that is the record: change `primary` or `backup` there, have it reviewed and
   deployed, and `install.sh` renders the env file and restarts the keepers. A line in keeper.env changes
   nothing for them, since the record's file is read after it. If the node is down and no deploy can wait,
   the same line in the rendered `/etc/colosseum/deployment.env` or `deployment-<name>.env` can be edited
   by hand, keeping a copy, and both units restarted; the next deploy writes the record's node back, so
   the record has to follow. For a deployment whose record has none: `COLOSSEUM_RPC_URL` in
   `/etc/colosseum/keeper.env`, a copy kept, both units restarted.
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
testnet (the pools team, 20 October 2026). The keeper says so at its start (`node_shared_with_gateway`).
On mainnet it does not start on the gateway's node: keeper.env names a node of its own first. Which
provider answers the HyperCore precompiles and recent `eth_getLogs` for it is still to be chosen;
drpc's did not read the precompiles. What that node has to do (11 Nov 2026):

1. serve HyperEVM's mainnet, 999, and testnet, 998;
2. answer `eth_call` to the HyperCore precompiles `0x…0800` to `0x…080F`: the keeper reads every rule off
   them;
3. answer `eth_getLogs` over windows of 1000 blocks or more, back to the factory's deployment block, with
   no blocks missing and no daily limit on old blocks below about 100,000 (the chain makes about 87,700 a
   day: 1.015 a second, measured 8 to 11 Nov 2026);
4. take 5 requests a second without a 429, under a key of the keeper's own, apart from the gateway's
   (audit M-1);
5. refuse a window that is too wide with a code or words of its own, not the `-32005` of a rate limit;
6. answer a window within 5 seconds, old ranges included, without a 504.

An address that carries a key cannot go in the record, which is public; a deployment on such a node leaves
`keeper_nodes` out of its record, so that keeper.env's line holds.

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

- Cloudflare (the deployment's owner, in the dashboard):
  - the `pools-api` DNS record, proxied, to the host's address;
  - the Worker that serves the app, and each new deployment of it;
  - that Worker's `workers.dev` address, turned off, so the app has one public name;
  - the Origin CA certificate from the CSR above.
- The SSH rule: the operator's current address, dated in the rule's description. A rule
  whose address the provider has since changed is removed.
