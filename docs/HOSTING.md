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
  HyperEVM serves `eth_getLogs` 50 blocks at a call, and the unit runs the defaults — 50 calls a
  pass, a pass every 30 seconds — so it walks about **5,000 blocks a minute**. The chain makes
  roughly 57 a minute, so catching up is quick per minute of downtime and slow per day of it:
  measured on 25 September 2026, 175,000 blocks took about **forty minutes** to walk. Until it
  reaches the head it has not seen the events that name the pools, so `following` stays low and
  it enforces nothing on the pools it has not rediscovered yet — silently, because every pass
  still logs `pass_done`.

  What to read: `pass_done` carries `next_block` and `latest`. If the gap between them is not
  shrinking, the keeper is stuck, not busy — check for `scan_stopped` above it. If it is
  shrinking, divide the gap by 5,000 for the minutes left. `--max-windows` (default 50) is the
  lever if a catch-up ever has to go faster, at the cost of more calls per pass against a node
  that rate-limits.
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

## By hand, and by whom

- Cloudflare (Alex, in the dashboard):
  - the `pools-api` DNS record, proxied, to the host's address;
  - the Worker that serves the app, and each new deployment of it;
  - that Worker's `workers.dev` address, turned off, so the app has one public name;
  - the Origin CA certificate from the CSR above.
- The SSH rule: the operator's current address, dated in the rule's description. A rule
  whose address the provider has since changed is removed.
