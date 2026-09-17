# Trading pools on Hyperliquid, with keys the trader never holds

Working title. Built for the Colosseum Crypto World's Fair, Hyperliquid track. Testnet only:
HyperEVM testnet (chain 998) and mock USDC.

An investor opens a pool. The pool is a contract with its own HyperCore account, and it
holds the capital and the rules: daily loss, drawdown, leverage, and which perps the account
may trade. A trader, a person or an AI agent, pays for a challenge and gets an account of its
own with a slice of that capital. The trader signs orders with their wallet and never holds
the key that signs for Hyperliquid. In this demo those keys are testnet keys held by our pool
gateway, which checks the platform's caps before it signs.

When a rule is broken, anyone can call `breach()`. The contract swaps the account's agent key
for an address nobody controls, cancels the open orders the caller lists and closes the
positions. If the trader reaches the target in time, with no rule broken and no position left
open, `graduate()` moves them on to the pool's own capital under a new key. Anyone may call
that one too.

Usenami Signer, our enclave signing service, is older than this event and takes no part in
this demo. [CONTINUITY.md](CONTINUITY.md) separates what existed before 14 September from what was
built in the window. [AI-USE.md](AI-USE.md) describes how we used AI tools.

## How an order travels

1. The trader's wallet signs the order's own fields with EIP-712: account, perp, side, price,
   size, reduce-only, time in force, nonce and expiry. The wallet shows those fields, not a
   hash.
2. The pool gateway reads the chain. The account has to come from our factory and be in a
   trading state, its agent key has to be bound to this trader in `KeyRegistry`, and the perp
   has to be on the account's list. The gateway also refuses an expired request and a nonce it
   has already seen.
3. The gateway checks the order against the platform's caps (a list of perps, a size cap for
   each, 400 USDC per order) and signs the Hyperliquid action with the account's key, which it
   holds. An order over a cap is refused before anything is signed.
4. The gateway checks that the signature recovers to the account's key, then submits it to
   the Hyperliquid testnet.

The caps don't know the account's equity, so they can't enforce rules that depend on it. The
contracts enforce daily loss, drawdown and leverage, reading HyperCore through its read
precompiles, and anyone may trigger the stop.

More detail: [docs/DESIGN.md](docs/DESIGN.md) for the contracts and
[docs/GATEWAY.md](docs/GATEWAY.md) for the gateway.

## Checking it yourself

Every account in the app has a page that shows the evidence and says where each piece comes
from:

- the account's agent key and its state in `KeyRegistry`, read from the chain;
- the account's fills on Hyperliquid, set against the pool's rules;
- who holds the keys: in this demo, our gateway. Nothing on the page can prove that, and the
  page says so.

Where the checks stop, and what we don't claim:

- The page checks trades. A signature that never reached the exchange can't be seen from
  outside, so we don't say the gateway has never signed an order against the rules.
- The gateway checks the trader's signature, and we run the gateway, so the operator could,
  technically, submit an order that fits the rules without the trader asking for it.
- In this demo the gateway holds the agent keys. Whoever controls its host can trade those
  accounts, within the caps or not, until someone stops them.
- After a stop, the gateway could still sign for the old key. Hyperliquid refuses the order,
  because that key is no longer the account's agent.
- The investor's limits aren't checked before signing. The gateway applies one platform
  policy, and each pool's rules live in its contract, which enforces them after the fact by
  stopping the account.

## Repository

| path | contents |
|---|---|
| `src/` | contracts: `PoolFactory`, `Pool`, `ChallengeAccount`, `KeyRegistry`, and `lib/CoreOps.sol` for the HyperCore calls |
| `test/` | Foundry tests on the HyperCore simulator from `hyper-evm-lib`, offline |
| `gateway/` | the pool gateway (Python) |
| `app/` | the web app: static files, no build step |
| `agents/` | the trader's gateway client and its command line, the limits every agent trades under (`desk.py`), a scripted demo trader, the prompt for the trading window, and an AI trader for the Claude API |
| `ops/` | the testnet deployment script and the keeper |
| `spike/` | scripts that check HyperCore behaviour, in the simulator and on live testnet |
| `scripts/` | the hygiene gate, the commit identity check and the mutation check of the tests |
| `docs/` | design notes |

## Running it

You need Foundry 1.7.1, Python 3.12 or newer (we use 3.14) with
[uv](https://docs.astral.sh/uv/), and Node 20 or newer.

```bash
git clone --recurse-submodules <this repository>
forge build && forge test
uv venv spike/.venv --python 3.14
uv pip install --python spike/.venv/bin/python -r spike/requirements.txt -r agents/requirements.txt
spike/.venv/bin/python -m unittest discover -s gateway/tests -t .
spike/.venv/bin/python -m unittest discover -s agents/tests -t .
node --test app/tests/*.test.mjs
python3 scripts/mutations.py   # breaks one guarantee at a time; every break must turn a test red
```

Everything below touches the testnet. The scripts refuse any RPC whose chain id isn't 998.
Wallet keys are read from outside the repository: `COLOSSEUM_KEY_DIR` holds one `<name>.key`
(mode 600) and one `<name>.addr` per wallet. The public testnet RPC rate-limits after a
handful of calls; `COLOSSEUM_RPC_URL` (scripts, keeper, agents) and `GATEWAY_RPC_URL` (gateway)
point them at another one.

```bash
# the demo's agent keys: owner-only files in a new directory, and their addresses
spike/.venv/bin/python ops/make_demo_keys.py --out <new directory outside the repo> --count 20

# deploy the contracts and publish those addresses; writes deployments/testnet-<label>.json
spike/.venv/bin/python ops/deploy_testnet.py --label rehearsal --keys-file <that directory>/addresses.txt

# the app: copy the factory, registry and first block from that record into app/config.js
python3 -m http.server 8790 --bind 127.0.0.1 --directory app

# the gateway, signing with those keys and checking the caps first
GATEWAY_FACTORY=0x... GATEWAY_REGISTRY=0x... \
GATEWAY_SIGNER=demo GATEWAY_KEYS_DIR=<that directory> \
GATEWAY_ALLOW_ORIGIN=http://127.0.0.1:8790 \
spike/.venv/bin/python -m gateway.server

# the keeper: list what is due, then send it
spike/.venv/bin/python -m ops.keeper --deployment rehearsal --once --dry-run
spike/.venv/bin/python -m ops.keeper --deployment rehearsal --once

# a scripted trade through the gateway, signed by the trader's wallet
spike/.venv/bin/python -m agents.bot rest --deployment rehearsal --account 0x...

# the command line the AI trader uses; --dry-run sends nothing and needs no key
spike/.venv/bin/python -m agents.client --deployment rehearsal pools
spike/.venv/bin/python -m agents.client --deployment rehearsal account 0x...
spike/.venv/bin/python -m agents.client --deployment rehearsal --dry-run order 0x... BTC buy 0.0005 76000
```

In the demo the AI trader is a Claude Code window that works only through that command line,
with the prompt in [agents/WINDOW-TRADER.md](agents/WINDOW-TRADER.md). The limits are in the
client (orders per day and their size, counted in `agents/state/`), the gateway and the
contracts, not in the prompt. `agents/ai_trader.py` gives the same desk to Claude over
the API (`--dry-run` prints the request and calls nothing). The video doesn't use it, and as of
17 September no session has run it against the API.

## Status and limits

Nothing is deployed yet. [CONTINUITY.md](CONTINUITY.md) tracks each piece by date.

The code is unaudited. We reviewed the contracts internally and fixed what we found; the
limits that remain are listed in [docs/DESIGN.md](docs/DESIGN.md#what-the-design-does-not-do).
The hackathon version leaves out several traders per pool, pool shares, a leaderboard,
collusion detection, a token and mainnet.

Not available to US persons. The app asks each visitor to confirm this and says it again in
its terms.

## License

Apache-2.0. See [LICENSE](LICENSE).
