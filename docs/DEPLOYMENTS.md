# Deployments

`deployments/LIVE.json` lists every deployment this repository holds a record for, with the role each one plays
today and what serves it. `app/tests/deployments.test.mjs` checks the list against the records, against the
networks in `spike/hlspike/network.py` and against `app/config.js`: change a deployment in one place and not the
others, and CI goes red. Anything that watches the deployments from outside the repository should read this list
instead of keeping a copy.

## An entry

| field | what it holds |
|---|---|
| `label` | the name the tools and the host use (`--deployment`, `COLOSSEUM_DEPLOYMENT`) |
| `network` | `testnet` or `mainnet`, as named in `spike/hlspike/network.py` |
| `chainId` | that network's HyperEVM chain id; the record's `chain_id` matches it |
| `role` | what the deployment is for today, one of the roles below |
| `record` | `deployments/<network>-<label>.json`, written by the deploy |
| `factory` | the record's `PoolFactory`; for a shared pool, the factory its seats come from |
| `pool` | the record's `SharedPool`; `null` for a factory's deployment, which has many pools |
| `gateway` | where the deployment's own gateway answers, as in `<gateway>/v1/health`; `null` if it has none |
| `deployer` | the record's `deployer` |
| `keeper` | the address the keeper tending it on the host sends from; `null` if nothing tends it |

`v3-demo3` is the odd one out: its record was written by hand and says `"label": "demo3"`. The tools, the host and
this list go by the file name.

## Roles

- `live`: the deployment the site sells challenges on (`role: "live"` in `app/config.js`). Its gateway is the host's
  first, the one the site sends orders to. A network has one at most.
- `testers`: served beside it on the host by its own gateway and keeper, with a test dollar and a faucet
  ([TESTNET-TRADER.md](TESTNET-TRADER.md), [TESTNET-INVESTOR.md](TESTNET-INVESTOR.md)). The site doesn't read it.
- `shared`: a shared pool tended by the shared pool's keeper ([HOSTING.md](HOSTING.md#the-shared-pools-keeper)). It
  has no gateway of its own: its seats are pools made by `factory`, so their challenges trade through that
  deployment's gateway. `sharedPoolRecord` in `app/config.js` picks the one `#/shared` opens.
- `staged`: deployed and its record committed, but nothing serves it yet and the site doesn't read it: no gateway,
  no keeper. A new deployment stays `staged` until its gateway and keeper run.
- `archive`: the site reads it and sells nothing on it (`role: "archive"` in `app/config.js`).
- `retired`: nothing serves it and the site doesn't name it. The record stays, since the evidence pages still point
  at what it made.

## Adding or retiring a deployment

One edit to `deployments/LIVE.json`: in the commit that adds the record, or as soon as the host changes.

- To add one, the deploy writes `deployments/<network>-<label>.json`; add an entry with its role and what serves
  it, `staged` until its gateway and keeper run. The site reads `live` and `archive` entries of its own network:
  for those, write `app/config.js` again with `python3 scripts/app_config.py --out app/config.js`. The tests name
  any field that is missing.
- To retire one, set `role` to `retired` and `gateway` and `keeper` to `null`. Keep the record.
- When a gateway or keeper starts or stops on the host, change `gateway` or `keeper` here. This file doesn't
  configure the host: `/etc/colosseum/deployment`, the instances beside it and `shared-keeper.env` decide what it
  serves ([HOSTING.md](HOSTING.md)).

Then run `node --test app/tests/deployments.test.mjs`; it names every place that disagrees.

## Mainnet records

There are none yet. A mainnet deployment will be listed the way a testnet one is, under its own network: its record
is `deployments/mainnet-<label>.json` with `"chain_id": 999` and `"network": "mainnet"`, and its entry here says
`"network": "mainnet"`, `"chainId": 999` and that `record`. 999 is mainnet's chain id in `spike/hlspike/network.py`.

The record has the fields of `deployments/testnet-v3-demo3.json` (the contracts, `block`, `commit`, `deployer`,
`operator`, `platform_assets`, `published_keys`, `tx`, `codehash`, `initcode_keccak`) except testnet's own test
dollar and faucet: on mainnet `usdc` is Circle's, `usdc_erc20` in network.py, and there is no `faucet`.

Where each value comes from:

| value | source |
|---|---|
| `factory` | the deploy writes `PoolFactory` into the record as its transaction lands |
| `pool` | for a shared pool, `ops/deploy_shared.py` writes `SharedPool`; a factory's deployment has none |
| `deployer` | the deploy key's address, which the deploy writes into the record |
| `gateway` | the host instance that serves it ([HOSTING.md](HOSTING.md#a-further-instance-a-second-deployment-on-the-same-host)); only here, not in the record |
| `keeper` | the keeper key made on the host for that instance (`ops/make_demo_keys.py --wallet keeper`) and given HYPE; only here |
| perp numbers | `platform_assets` in the record, numbered as orders number them: a HIP-3 perp is 100000 + dex × 10000 + its index in that dex's `meta` (`gateway/assets.py`, `app/lib/hl.js`) |

Mainnet's perp numbers, read on 19 and 21 October 2026, are in [HOSTING.md](HOSTING.md): BTC 0, ETH 1, SOL 5,
HYPE 159; xyz is dex 1, so `xyz:XYZ100` is 110000. Read them again from mainnet's `perpDexs` and `meta` just before
`setPlatformAssets`, since a dex's list can grow; `ops/deploy_testnet.py` still holds testnet's (BTC 3, ETH 4,
SOL 0).

`ops/deployments.py` reads the record of the network `COLOSSEUM_NETWORK` names (`spike/hlspike/network.py`):
`deployments/<network>-<label>.json`. The record's `chain_id` must be that network's, and its `network` field, where
it has one, must name it. The keepers load their deployment through it, so a keeper started on mainnet reads
mainnet's records and never testnet's. `scripts/settled_accounts.py` reads the selected network's `live` and
`testers` entries here, and marks the seats of the shared pool the site opens. `scripts/app_config.py` writes the
app's settings from the same entries (below).

This list and its test take a mainnet record too. They check a mainnet entry against mainnet's chain id from
network.py, allow one `live` deployment per network, and hold `app/config.js` only to the entries of the app's own
network, the one whose chain id is `CONFIG.chainId`. A mainnet example inside the test, not in `deployments/`, goes
through the same checks.

## What stays on testnet

Some tools keep refusing any network but testnet, on purpose. Each says why where it refuses:

- `ops/deploy_testnet.py` and `ops/create_live_pool.py` deploy the contracts before v3, with four-field rules, and
  open pools on them. Mainnet's deployment is v3's.
- `ops/deploy_v3.py` and `ops/deploy_multi.py` deploy v3. On mainnet they print the plan and send nothing
  (`--network mainnet --dry-run`): the network's chain id, RPC, API and USDC from `spike/hlspike/network.py`, each
  contract's address when `--deployer` names the address the owner will deploy from, mainnet's perp numbers read
  from its meta, the gas at today's prices (deploy_v3, through `scripts/mainnet_gas.py`), and the record they would
  write. A mainnet run without `--dry-run` is refused: sending to mainnet is a decision of its own.
  `PLATFORM_ASSETS` in `ops/deploy_testnet.py` are testnet's perp numbers; mainnet's are in Mainnet records, above.
- Either v3 deploy deploys a key binder (`src/KeyBinder.sol`, docs/KEY-BINDING.md) with the flags of
  `ops/binder_deploy.py`: `--publisher` with `--pcr-hash` and `--attestation-hash`, `--guardian`,
  `--max-binds-per-day` (testnet 50, no default on mainnet), `--renew-delay` (24 hours). Given `--safe`, its last
  steps name that Safe as the binder's owner (`transferOwnership`) and the factory's operator (`transferOperator`);
  the Safe accepts each itself (`acceptOwnership`, `acceptOperator`), and until then the deployer holds both. The
  record says so in `key_binder` and `factory_operator` (`from` the deployer, `to` the Safe, `state` pending), and
  the weekly watch (`scripts/settled_accounts.py`) reads the binder's `owner()`, `pendingOwner()`, `publisher()`,
  `guardian()` and `paused()`, and the factory's `operator()` and `pendingOperator()`, beside it. No agent key is
  published. The deployments before the binder handed their registry over with `--operator` and say so in
  `registry_operator`, which `ops/deployments.py` and the watch still read.
- `ops/deploy_shared.py` and `ops/shared_run.py` work with SharedPool, a testnet layer outside the reviewed core.
  The run pays in testnet USDC.
- The agents in `agents/` are testnet tools.

Two more pieces of the way to mainnet are decided apart from this code, and nothing here changes them:

- The host. A mainnet instance needs its own RPC, keys, gateway and keeper settings: on the host itself, and in
  `ops/host/` and `ops/host_instances.py`. Setting it up is the host owner's work and decision.
- The signer, in its own repository. Signing pool orders under `hyperliquid_main`, and checking those signatures,
  is its own change and decision.

## The app on either network

`app/config.js` is written by `scripts/app_config.py` from `spike/hlspike/network.py` and from that network's
entries here: the `live` one, then the `archive` ones in this file's order, each with its record, and the first
`shared` entry on the live factory, which `#/shared` opens. The committed file is the testnet output, and
`app/tests/config.test.mjs` holds the two equal. After a change to network.py or to this file, run
`python3 scripts/app_config.py --out app/config.js` and commit both. `app/lib/hlsend.js` signs spot transfers for
the config's `hyperliquidChain`.

`scripts/pack-site.sh --network mainnet <commit>` packs the same commit for mainnet. Its `config.js` is the
script's mainnet output, written from that commit's files, and its `build.json` carries `"network": "mainnet"`.
Every other file is checked against its blob, as for testnet, and `scripts/verify-live.py` checks the site against
the pack's own list as before. The config goes into the pack instead of being fetched when the page loads, so a
mainnet site cannot fall back to testnet's values because a file failed to arrive. While this file has no live
mainnet entry, which is the case today, the script stops and nothing is packed.

Two of the app's values are not in this file. `liveRunPool` is a pool opened on testnet's live deployment after it
was deployed; the script keeps it by network and label. `assetLeverage` is set when the record names a
`PaperExamImpl`, that is, for a v3 factory.

What a mainnet site still needs is copy. The pages say testnet in their own words: the title and the gate in
`app/index.html`, the about text in `app/app.js`, "Testnet. Mock USDC. Unaudited." in `app/views/pools.js`, the
shared pool, verify, economics and evidence pages, and the safety and evidence data in `app/data/`. Each needs
mainnet wording before a mainnet pack is uploaded.
