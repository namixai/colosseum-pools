# Continuity: what existed before the Crypto World's Fair, and what we build during it

The rules allow older code if it is disclosed, and they judge only the work done inside the
window, which runs from 14 September 2026, 06:00 PDT, to 12 October 2026, 23:59 PDT. This
file was started on 16 September, day three, and it grows with the work. Rows are never
deleted. A row that turns out to be wrong gets corrected where it stands, and the git history
keeps the old version.

## In one paragraph

We are building trading pools on Hyperliquid. An investor puts in capital and sets the
rules. A trader, a person or an AI agent, pays for a challenge and trades a slice of that
capital without ever being handed a key. We first planned to take those keys from Usenami
Signer, our enclave signing service, which is months older than this event. On 17 September
we dropped that for the demo: it runs on testnet without real money, so it doesn't get a key
ceremony. In the demo the keys are ordinary testnet keys held by our pool gateway, which checks
the platform's caps before it signs, and the enclave takes no part. What we write in the
window, in this repository, is everything: the contracts, the pool gateway and its signer, the
app, the page where you check our work yourself, and the demo agents.

## What existed before 14 September (not claimed as hackathon work)

Since 17 September the demo uses none of the Usenami Signer rows below. They stay, because rows
here are never deleted and the early design leaned on them.

| component | state before the window | where to check |
|---|---|---|
| Usenami Signer: keys held in an AWS Nitro Enclave, signing behind a gateway | in development since May 2026, live service | public repository `namixai/signer` |
| The exact build this project planned to use until 17 September | tag `pcr0-fbaad62f`, commit `865f418`, 10 September 2026. We are not rebuilding the enclave for this event, and since 17 September the demo doesn't use it at all | that tag in `namixai/signer` |
| Hyperliquid agent keys minted inside the enclave (`provision_agent_key`, venues `hyperliquid_main` and `hyperliquid_testnet`) | in that build | `poc/enclave/src/handler.rs` at the tag |
| Per-asset size and notional caps, checked in the enclave before it signs a Hyperliquid order (`hl_order_caps`, enforced by `enforce_hl_caps`) | in that build | same file |
| Hyperliquid testnet signing (phantom-agent `source = "b"`), orders and cancels only | in that build | same file |
| A signed receipt for every decision, refusals included, from a key bound into the attestation document | since the August 2026 rotation | `docs/CLIENT-ONBOARDING.md` in `namixai/signer` |
| An attestation endpoint, and a PCR0 registry contract on Base | live | `namixai/signer` README |
| Live orders on Hyperliquid mainnet: a full round trip, entry and exit both filled, on 19 August 2026 | product history, on our own account with our own money, no external audit | not reproduced in this repository |
| Our ETHOnline 2026 submission (`namixai/signer-ethonline2026`) | a separate event with a different scope: Permit2, Uniswap, The Graph, World ID | nothing from it is imported here unless a row below says so |

## What we learned in the first days of the window (knowledge, not code)

| date | what |
|---|---|
| 15–16 Sep | read the track and the rules; read Hyperliquid's docs on CoreWriter, the read precompiles, API wallets and testnet USDC; queried the live testnet RPC |
| 16 Sep | settled the scope: two roles, investor and trader, a stop that anyone can trigger, testnet only |
| 16 Sep | created this repository, private for now |
| 17 Sep | read from the testnet RPC: a small HyperEVM block allows 3M gas. Storing the code of `Pool` (19 KB) or `ChallengeAccount` (16 KB) costs more than that, so the deployer switches to big blocks for the deployment |
| 17 Sep | the demo drops the Signer enclave: a testnet demo without real money gets no key ceremony (Alex's decision). The pool gateway holds the demo's agent keys and checks the platform's caps before it signs |
| 17 Sep | the spike's live run on testnet: replacing the agent from a contract cuts the old key; one agent address serves one account; USDC moves between contract accounts on HyperCore; an address that already has an account can't become an agent. The USDC bridge from HyperEVM credited only an address depositing to itself, so pool capital has to arrive on HyperCore. Details in `spike/README.md` |

## What we build in the window, as of 17 September

| piece | status |
|---|---|
| Spike, six questions: does replacing an API wallet through CoreWriter really cut off the old key; can one agent address serve two accounts; can USDC move between contract accounts and back; how a contract reads account equity; what the trader pays with; can a contract cancel orders | 16 Sep: harness contract, simulator tests and the live testnet scripts written (`spike/`); the live runs waited for testnet USDC. 17 Sep: live run on testnet, questions 1–8 answered (two added along the way), and five follow-up runs on the USDC bridge that cost 24 test USDC. Report in `spike/README.md` |
| Contracts `PoolFactory`, `Pool`, `ChallengeAccount`, `KeyRegistry` (Foundry, HyperEVM testnet) | 16–17 Sep: written, a mutation check of the tests, two internal review passes and their fixes. 17 Sep, after a bot review: the funded trader's share is computed from what closing realized, settlement waits for positions nobody named, and the factory takes an operator-set fee per challenge. These last changes have had only the author's review so far. 67 simulator tests; not deployed. Later on 17 Sep, from the live spike: no HyperEVM deposit (capital arrives on HyperCore), the 1 USDC fee for a new HyperCore account is reserved or taken from the share, and a stop records the key it cut. A second internal review pass the same day found that a published key could be spoiled by sending it USDC, and fixed it. 75 simulator tests; not deployed |
| Pool gateway, which checks on chain that a key belongs to the trader's account before an order goes to the signer | 17 Sep: written. Later that day it got its own demo signer, which the demo uses: testnet keys in owner-only files, and the platform's caps checked before signing. The mode that calls a Usenami Signer gateway stays in the code, unused. The trader's wallet signs the order's own fields (EIP-712). Offline tests, two of them against the Hyperliquid SDK's published signing vectors. Not running anywhere yet. 18 Sep: a sell counts toward the 400 USDC cap at its limit or at the market mid, whichever is higher. Counted at its limit alone, a sell priced under the market would pass the cap and fill over it; the AI trader's own per-order cap had the same gap. |
| App: investor page, list of pools, challenge page, and the check-it-yourself page | 17 Sep: written as static files and tried in a browser against testnet reads and the live Signer attestation. No contracts are deployed yet, so there is no pool to show. Later that day the Signer attestation panel was removed, since the demo doesn't use the enclave. Later that day: the investor's capital goes to a pool as a HyperCore transfer signed by the wallet, and the request the app sends was accepted on testnet |
| Demo agents: a scripted bot and an AI agent, each trading a challenge | 17 Sep: the trader's gateway client, the scripted bot and the AI trader (Claude through the Anthropic API, its limits in code) written, with offline tests. No session has run against the API yet. Later that day: the demo's AI trader became a Claude Code window that trades through the client's command line (`agents/WINDOW-TRADER.md`); the limits moved to `agents/desk.py`, and the API trader stays, unused |
| Keeper, which makes the calls anyone may make when they fall due: activate, abort, the daily checkpoint, breach, expire, settle | 17 Sep: written, not run yet. Later that day: it also cuts again a key that still trades after a stop, and aborts a challenge whose key was spoiled |
| Hosting: the gateway and the keeper on their own host, the app on Cloudflare Pages | 18 Sep: the host set up from `ops/host/`, the agent keys and the keeper's key made on it, and the rehearsal deployment published on testnet. The gateway and the keeper run there; the gateway is reached through an ssh tunnel until the public name is set up. The checkpoint set for 26 Sep passed that day: a pool with its capital, a challenge bought, its key bound, an order through the gateway filled, and an order over the cap refused before signing. The keeper activated that challenge on its own |
| No access for US residents: a geoblock plus a line in the terms, as Hyperliquid itself does | 17 Sep, in the app: a confirmation on entry and a line in the terms. The geoblock at the host comes with hosting, which isn't set up |

## Rules we hold ourselves to

- Testnet only. This project sends no mainnet transactions and uses mock USDC.
- In the demo the keys are held by our pool gateway, and our code checks the rules before it
  signs. We don't say the keys are in an enclave, and we mention Usenami Signer only as an
  existing product, with a link to what is already public.
- The investor's limits aren't checked before signing. The gateway applies one platform
  policy, an asset list and size caps, and each pool's own rules live in its contract. We
  won't describe limits signed by the investor's wallet as done.
- We don't claim the trader approved each order. We run the gateway that checks the trader's
  signature, so the operator could, technically, submit an order that fits the rules without
  the trader asking for it.
- The check-it-yourself page checks trades. It compares the account's fills on Hyperliquid
  with the pool's rules. We never write that the gateway has never signed an order against
  the rules, because a signature that never reached the exchange can't be seen from outside.
- There are no signed receipts in the demo. An order over the caps is refused by the gateway's
  own code, and we say it that way.
- After a stop, the gateway could still sign for the old key, and Hyperliquid is the one that
  refuses. The stop
  replaces the account's agent, so the exchange rejects a later order signed with the old
  key. We say it in exactly those terms.
- We are not the first. Other teams already run prop trading on Hyperliquid and record their
  rules on chain. What we want to show is narrower: the trader never holds a key, the capital
  sits in an account that a contract controls, and anyone can trigger the stop without
  trusting us.
- Dates stay put. If something existed before the window, this file says so.
