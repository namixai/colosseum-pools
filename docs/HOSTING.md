# Hosting the app, the gateway and the keeper

Status, 23 September 2026: the gateway and the keeper both run on the host against the
rehearsal deployment, each `active` under systemd, and the gateway is public as
`pools-api.usenami.io` behind Cloudflare with `/v1/health` answering. The app at
`pools.usenami.io` is the `app/` folder of commit `7855198`.

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
  trader's run short, so the timing is theirs. The keeper's own header says so. So a second keeper has exactly
  the powers of the first, and needs no permission from it or from us.

  What it needs is its own wallet and its own state file:

      COLOSSEUM_KEY_DIR=<its own key directory> \
      COLOSSEUM_RPC_URL=https://rpcs.chain.link/hyperevm/testnet \
      python -m ops.keeper --deployment demo --state <its own file>.json --every 30

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
  - the gateway reads the chain through `rpcs.chain.link/hyperevm/testnet`, which allows 1000
    calls per IP in five minutes. It throttles in bursts: on 24 Sep it refused three `eth_call`s
    inside one second and served the same reads five times over seconds later, so the gateway
    retries five times and answers 429 when it still cannot read (`gateway/chain.py`);
  - the keeper reads event logs, and **Hyperliquid's own node refuses `eth_getLogs` from this
    host**: `-32602 invalid block range` for any range, 60 blocks back or 100 000, while
    `eth_blockNumber` answers and the same call from a laptop goes through. `rpcs.chain.link`
    serves those logs from the host, so that is what the keeper uses.
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

It says whether the commit is on `origin/main`, uploads its tracked files and runs that
commit's `ops/host/install.sh` on the host. Without `COLOSSEUM_SSH_KEY`, ssh uses its own
configuration. That script:

- builds the venv when the requirements changed;
- installs the units, and the nginx site once there is a certificate;
- writes `deployment.env`;
- switches `current`;
- restarts the services that are enabled;
- keeps the three newest releases.

The deployment record the services use has to be committed: `install.sh` reads it from the
release.

🔴 **By hand on a host set up before 24 September 2026:** `/etc/colosseum/keeper.env` still says
`COLOSSEUM_RPC_URL=https://rpc.hyperliquid-testnet.xyz/evm`, and that node refuses the keeper's
log reads from this host (above). `bootstrap.sh` writes that file only when it is missing, so
deploying does not change it. The fix is one line and a restart, and it is a change to the
host's configuration — it happens on the operator's word, not as part of a deploy:

    sudo sed -i 's|^COLOSSEUM_RPC_URL=.*|COLOSSEUM_RPC_URL=https://rpcs.chain.link/hyperevm/testnet|' \
        /etc/colosseum/keeper.env
    sudo systemctl restart colosseum-keeper
    journalctl -u colosseum-keeper -n 5     # pass_done, and next_block climbing towards the head

## Choosing the deployment and starting

    echo rehearsal | sudo tee /etc/colosseum/deployment      # on the host
    ops/host/deploy.sh <commit> <user@host>                   # from the Mac
    sudo systemctl enable --now colosseum-gateway colosseum-keeper

`install.sh` refuses a label without a complete testnet record in the release.

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
