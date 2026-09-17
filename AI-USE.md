# Where AI is used in this project

We read Colosseum's official rules, its Code of Ethics and its Terms of Service on
16 September 2026. None of them mentions AI tools. The FAQ says Colosseum has backed founders
who built their MVPs entirely with AI coding tools, and that the repository review looks for
work done by the team rather than by a third party. So here is how the work is actually done.

## Who does what

Claude Code (Anthropic) writes this repository. It runs as several sessions, one per area of
the company, and the session assigned to this event writes the contracts, the gateway, the
app, the tests and these documents.

Alex, the founder, makes the decisions: what gets built and what gets cut, what may be said
in public, when this repository goes public, and anything that costs money. Alex also
presents the pitch and the demo, on camera and in Alex's own voice.

Review is done by another session, not by a second person. One GitHub account carries every
commit and every merge, and it is the founder's. The session that writes a change never
merges it; a separate coordinating session reads it, argues with it and decides. In our
earlier projects that gate has sent work back more than once. It is still a model, though,
and we won't call it a human reviewer.

## Claude inside the demo

One of the demo's traders is Claude itself: a Claude Code window that Alex opens for the
recording, with the prompt in `agents/WINDOW-TRADER.md`. It works only through
`python -m agents.client`, which can list the pools and buy one challenge, read the account
and a market, place or cancel an order, close a position, and ask the contract to pass the
challenge. It trades through the same gateway as a person, with its own testnet wallet, and
never sees an account's key.

The limits aren't left to the prompt. `agents/desk.py` refuses an order for a perp that isn't
on the account's list, one under Hyperliquid's minimum or over 100 USDC, and one that would
take the account past a margin under its leverage rule. The client counts four orders a day
per account and one purchase a day per wallet, and no command-line option raises those
numbers. Behind the client, the gateway and the contracts check again.

`agents/ai_trader.py` gives the same desk to Claude over the Anthropic API, with a model-turn
cap and a spending budget on top. We kept it, but the video doesn't use it, and as of
17 September no session has run it against the API.

## What stays out of this repository

- Keys. The demo wallets, and the agent keys the pool gateway holds, are testnet-only keys
  generated for this project. They are never committed, never printed in reports, and never
  used for anything else.
- Infrastructure details: server addresses, instance names, internal paths.
- Task orders. The sessions work from written orders in Russian, posted in an internal
  channel that also carries other teams' business. Publishing them is the founder's call,
  and as of 16 September that call hasn't been made. If the answer is yes, they will go
  under `plans/`, with a note on anything that was redacted.

## The count

As of 16 September, every file in this repository except `LICENSE` was written by a Claude
Code session. `LICENSE` is the Apache License 2.0, word for word. We will count again before
submitting and put the new number here, with its date.
