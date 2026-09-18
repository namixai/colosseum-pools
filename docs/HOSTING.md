# Hosting the gateway and the keeper

Status, 18 September 2026: the host is set up, and the gateway and the keeper run there
against the rehearsal deployment. The public name waits for the Cloudflare steps below.

## Where things run

- **The pools host.** EC2 `t3.small` in us-east-1, Amazon Linux 2023, no instance profile,
  tagged `TerminateAfter=2026-12-06`. Its address isn't written here: the operator passes it
  to `deploy.sh`.
  - Its security group lets in 443 only from Cloudflare's ranges and 22 only from the
    operator's address.
  - It runs the gateway and the keeper, each as its own user.
- **The gateway** (`colosseum-gw`) listens on `127.0.0.1:8787`. nginx serves it on 443 as
  `pools-api.usenami.io`, behind Cloudflare, which blocks visitors from the US.
- **The keeper** (`colosseum-keeper`) has no port. It polls the chain every 30 seconds.
- **The app** is the static `app/` folder on Cloudflare Pages, `pools.usenami.io`, with the
  same US block. It calls the gateway from the browser; `GATEWAY_ALLOW_ORIGIN` names it.
- **RPC** (CTO, 17 Sep):
  - the gateway reads the chain through `rpcs.chain.link/hyperevm/testnet`, which allows 1000
    calls per IP in five minutes;
  - the keeper uses Hyperliquid's public node, which allows 100 per minute.

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
without capabilities; only the keeper may write, and only to its own directory.

Whoever holds root on this host holds the agent keys and can trade every account whose key is
on it, until someone stops that account. The keys are testnet keys for mock USDC, made for the
demo and used nowhere else.

## One-time setup

1. On the host, as root: `ops/host/bootstrap.sh`. It makes the users, directories and env
   files, the TLS key and its CSR, and prints the CSR. Running it again adds only what is
   missing.
2. From the Mac: `ops/host/deploy.sh <commit> <user@host>`, which also builds the venv.
3. The agent keys, on the host, as the gateway's user. Only the addresses leave the host,
   for `ops/deploy_testnet.py --keys-file`:

       cd / && sudo -u colosseum-gw /opt/colosseum-pools/venv/bin/python \
           /opt/colosseum-pools/current/ops/make_demo_keys.py --out /var/lib/colosseum-gw/keys --count 20

4. The keeper's key, on the host, as the keeper's user. It prints the address, which then
   needs testnet HYPE for gas:

       cd / && sudo -u colosseum-keeper /opt/colosseum-pools/venv/bin/python \
           /opt/colosseum-pools/current/ops/make_demo_keys.py --wallet keeper --dir /var/lib/colosseum-keeper/secrets

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

## Choosing the deployment and starting

    echo rehearsal | sudo tee /etc/colosseum/deployment      # on the host
    ops/host/deploy.sh <commit> <user@host>                   # from the Mac
    sudo systemctl enable --now colosseum-gateway colosseum-keeper

`install.sh` refuses a label without a complete testnet record in the release.

## Checking

- `curl -s 127.0.0.1:8787/v1/health` on the host: the chain, the signer mode and the number of keys.
- `journalctl -u colosseum-gateway`: one JSON line per request.
- `journalctl -u colosseum-keeper`: `pass_done` and `pass_failed` lines.
- The keeper's progress file changes on every pass. Older than five minutes means it stopped.
- Through Cloudflare: `https://pools-api.usenami.io/v1/health`, from outside the US.

## By hand, and by whom

- Cloudflare (Alex, in the dashboard):
  - the `pools-api` DNS record, proxied, to the host's address;
  - the US block on both names;
  - the Pages project;
  - the Origin CA certificate from the CSR above.
- The SSH rule: the operator's current address, dated in the rule's description. A rule
  whose address the provider has since changed is removed.
