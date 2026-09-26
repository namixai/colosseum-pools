# Pool stress test: what the rules actually do in a cascade

The pool's rules are simple: a seat may lose **3 %** of its equity snapshot taken at UTC midnight,
**6 %** of its starting capital in total, and may hold at most **5×** leverage. A keeper polls the
chain every 30 seconds and closes the position with a market IOC order when a line is crossed.

This package answers one question with data anybody can re-download: **do those rules hold on real
crash days?**

The short answer, measured, not modelled:

> The 3 % / 6 % rule holds on an ordinary crash day — the pool's median loss per entry is
> **2.97–3.33 %** across the eight worst days since 10 Oct 2025. It does **not** hold in a cascade
> like 10 Oct 2025, and how badly depends on which coins the seats sit on: on the default list
> (BTC, ETH, SOL) the pool loses **24 % to 100 %** of seat capital, on a wider list with alts
> **84–94 %**, and there the exchange liquidates three of five seats before the keeper can act.
> The upper ends are the worst price inside the keeper's delay window with a liquidated seat
> losing everything; the lower ends are the open of the next minute with the maintenance margin
> kept. On the default list it is the **short** side that gets liquidated — the SOL seats, by the
> bounce of 21:21 — not the long.

A number without the name of the coin list is meaningless here. Every table below names its list.

**All bars are Bybit USDT perpetuals, not Hyperliquid.** See *Limits* — this matters, and it is the
first thing to say, not a footnote.

## What is measured

| | |
|---|---|
| Days | the 8 worst since 2025-10-10 by average intraday drawdown |
| Symbols | ADA, AVAX, BNB, BTC, DOGE, ETH, LINK, SOL, SUI, XRP (USDT perps) |
| Entries | every minute of every day, each entry an independent seat |
| Position | notional = 5 × equity, one coin per seat, long and short measured separately |
| Exit line | whichever comes first: daily `snapshot × (1 − 3 %)` or static `capital × (1 − 6 %)` |
| Keeper delay | `--lag 0` (line price, perfect keeper) · `--lag 1` (open of the next minute) · `--lag 2` (one poll missed) |
| Execution | `--exec open` — the price the keeper sees · `--exec worst` — the worst price inside the delay window |
| Liquidation | the exchange closes first if equity falls below `--mm` (2 %) of notional. What the seat keeps is a bracket, not a number: at the lower bound (`--exec open`) it keeps the maintenance margin, at the upper bound (`--exec worst`) it loses everything. Hyperliquid's own words: a liquidation through the book leaves "any remaining collateral" with the trader; a backstop liquidation (equity below 2/3 of the maintenance margin) does not return it |
| Fees | taker 4.5 bps on the whole notional at the close |

**`--lag 1` at the open is not an upper bound.** The open of the next minute sometimes lands on a
bounce and the seat exits *better* than the line — that is what BTC did on 10 Oct. That is why every
range in this file is the same bracket: **the open of the next minute (`--lag 1 --exec open`, a
liquidated seat keeps its maintenance margin) to the worst price inside that minute's delay window
(`--lag 1 --exec worst`, a liquidated seat loses everything)**. The real keeper sits between them.
`--lag 0` — closing exactly at the line, the perfect keeper — is kinder than both ends; it is
published in `results/` for completeness and is never quoted as a bound. If a line is crossed in
the last minute of a day, the exit is that minute's close, the last price the keeper would see.

**Overshoot is measured here, not assumed.** The economics model assumes a keeper overshoots the
line by 0.5 % of equity. Measured over 115 200 entries per side: mean **0.46 pp** at the open,
**1.41 pp** at the worst price of the window; p99 **2.96 pp** and **8.02 pp**; maximum 89 pp at
the open and 97 pp at the worst price (a liquidated seat losing everything). The assumption is
right in the middle of an ordinary day and far too kind in a cascade.

## Ordinary crash days hold; the cascade does not

Pool of $100 000, five seats of $50k / $25k / $5k / $5k / $5k, all entering the same minute.
Coins: ADA, AVAX, BNB, BTC, DOGE — the **wide list with alts**. Long side shown; the ranges are
open → worst.

*Bybit bars, not Hyperliquid.*

| day | median loss | p99 (open) | p99 (worst) | worst entry | seats hit | liquidated |
|---|---|---|---|---|---|---|
| 2025-10-10 | 3.26 % | 10.06 % | 24.84 % | **83.7–94.1 %** at 21:18 | 5/5 | **3** |
| 2026-02-05 | 3.33 % | 6.03 % | 7.98 % | 8.5–10.1 % at 17:30 | 5/5 | 0 |
| 2026-01-31 | 3.14 % | 5.56 % | 8.88 % | 20.3–21.7 % at 18:42 | 5/5 | 0 |
| 2025-11-03 | 3.13 % | 4.48 % | 7.42 % | 21.7–25.2 % at 15:30 | 5/5 | 0 |
| 2026-06-05 | 3.18 % | 4.57 % | 6.45 % | 5.2–7.3 % at 15:56 | 5/5 | 0 |
| 2025-11-04 | 3.15 % | 4.60 % | 7.57 % | 6.4–10.9 % at 18:40 | 5/5 | 0 |
| 2025-11-21 | 3.11 % | 4.47 % | 11.71 % | 11.5–30.7 % at 07:33 | 5/5 | 0 |
| 2025-12-01 | 2.97 % | 4.24 % | 5.25 % | 6.5–9.0 % at 00:05 | 5/5 | 0 |

Read the median column as "the rule works": half the entries of a crash day cost the pool about
3 %, which is what the daily line promises. Read the last two columns as "and here is the tail":
**the worst entry beats the 6 % cap on seven of the eight days, and 20 % on three of them.** The
cascade is the extreme of a tail, not a freak event standing alone.

On 10 Oct the pool loses **$75 299 to $84 712 of $90 000** in seat capital at the 21:18 entry
(open of the next minute → worst price in the window). The rules allowed 6 %, i.e. $5 400. That is
fourteen to sixteen times the permitted loss, and three seats are gone before any rule can be
applied to them — at the lower bound they keep their maintenance margin, at the upper bound nothing.

## The same cascade on the default list (BTC, ETH, SOL)

The tail depends on the asset list far more than on the rule. Same day, same minute, same pool —
only the coins under the five seats change.

*Bybit bars, not Hyperliquid.*

| seat layout | pool loses | liquidated at the open | liquidated at the worst price |
|---|---|---|---|
| big seats on the calm coins (BTC 50k, ETH 25k, SOL 3×5k) | 23.9–40.0 % | none | the three SOL seats (short) |
| the big seat on the risky coin (SOL 50k, ETH 25k, BTC 3×5k) | 31.7–70.0 % | none | the SOL seat (short) |
| all five seats on SOL | 45.2–100 % | none | all five (short) |

Same bracket as everywhere in this file: open of the next minute → worst price inside the delay
window. For all three layouts the upper end is the **short** side caught by the 21:21 bounce, not
the long, and at that bounce the exchange liquidates the SOL seats. **On the long side no seat of
the default list is liquidated; on the short side, at the worst price, every SOL seat is.** The
alts are what bring the exchange in on the long side, and SOL brings it in on the short side.

Per coin on 10 Oct, worst entry of the day, open → worst. *Bybit bars, not Hyperliquid.*

| coin | long | short |
|---|---|---|
| BTC | 25.18 → 34.79 % | 13.75 → 22.96 % |
| ETH | 23.72 → 30.94 % | 34.11 → 38.22 % |
| SOL | 45.24 → 72.85 % | 34.07 → 100.0 % |

## Limits — read these before quoting a number

1. **Bybit bars, not Hyperliquid.** The pool trades on Hyperliquid, where liquidation is driven by
   an oracle price, not by the last trade. The direction of every result here is unaffected; the
   figure "three of five seats liquidated" can be. Quote the ranges, not a single number.
2. **Hyperliquid's own minute history for 10 Oct 2025 is not publicly retrievable.** The public
   `candleSnapshot` endpoint keeps minutes for days, not a year, and returns nothing for that date.
   Closing this gap needs a paid archive; we did not buy one, and we say so rather than implying
   the numbers are HyperCore's.
3. **A minute grid cannot resolve a 30-second poll.** A line crossed inside a minute is visible
   only through that minute's low (long) or high (short). That is why the answer is a bracket of
   delay and execution modes and never one number.
4. **Entries every minute are a stress sweep, not a strategy.** Each entry is an independent seat
   opening at that minute; the distribution answers "how bad can an entry be", not "what would a
   trader have earned".
5. **A number without the name of the coin list is wrong.** "The pool loses 85 %" is true of the
   wide list and false of the default one. Always carry the list.
6. **Nothing here is claimed beyond the measurement.** No SLA, no promise about a future cascade,
   no Hyperliquid-specific liquidation claim.

## Reproduce it

```bash
PKG="$(pwd)"                                         # run from the package directory
python3 fetch_bybit_minutes.py --out /tmp/fresh     # public Bybit API, no key
(cd /tmp/fresh && shasum -a 256 -c "$PKG/data/SHA256SUMS")   # the fresh bars must match the snapshot
python3 test_anchor.py                               # re-runs everything over data/ and compares
```

`test_anchor.py` is the one that matters: it re-runs the four published configurations and the
layout run over the snapshot in `data/`, compares **every field of every section** — the rule
block included — with the files in `results/`, and then checks the headline numbers by name (83.7,
94.1, 45.24, 100.0, the three layouts, the medians, seven of eight days over the cap). If the
exchange restates a candle or a refactor shifts a number, this test goes red instead of letting a
stale claim survive in this README. `make_results.py` is the command that produced `results/`.

A single run, for a single day:

```bash
python3 pool_stress.py --days 2025-10-10 --entries minute --seats-report --lag 1 --exec worst
python3 pool_stress.py --days 2025-10-10 --entries minute --seats-report \
        --symbols BTCUSDT,ETHUSDT,SOLUSDT --seat-coins SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT,SOLUSDT
```

Requirements: Python 3.10+ and numpy (the stress test uses it; the downloader is stdlib only).

## Files

| file | what it is |
|---|---|
| `pool_stress.py` | the stress test — the backtester's tool, plus `--data-dir`, CSV input, `--seat-coins`, and the three fixes of the 25 Sep 2026 review (see `CHANGES.md`) |
| `test_pool_stress.py` | 29 checks of the rules on synthetic data, by the same author |
| `mut_pool_stress.py` | 32 deliberate breakages; each one must turn a check red |
| `test_anchor.py` | the anchor: the snapshot must still produce the published numbers |
| `make_results.py` | the one command that writes every file in `results/` |
| `CHANGES.md` | what the review of 25 Sep 2026 changed, number by number |
| `fetch_bybit_minutes.py` | downloads the exact bars from Bybit's public API |
| `data/` | the snapshot: 10 symbols × 8 days × 1440 minutes, with `SHA256SUMS` |
| `results/` | the four runs (lag 0 / 1 / 2 at the open, lag 1 at the worst price) and the layout run, produced on 25 Sep 2026 by `make_results.py`; the 20 Sep 2026 figures they replace are in `CHANGES.md` |

The tool's comments and its JSON keys are in the author's language (Russian). They are kept as they
are: this package publishes a measurement, and rewriting 400 lines of audited code to translate it
would risk the numbers it exists to carry. The keys used above:

`сутки` day · `монета` coin · `сторона` side (`лонг` long, `шорт` short) · `убыток_пула_медиана_%`
median pool loss · `убыток_пула_p99_%` p99 · `худший_убыток_%_капитала_мест` worst entry as % of
seat capital · `худший_вход` the minute of it · `мест_пробито_в_худшем` seats that crossed a line ·
`мест_ликвидировано_в_худшем` seats liquidated by the exchange · `перелёт_*` overshoot past the
line, in percentage points · `пробито_%` share of entries that crossed a line · `ликвидаций_%`
share liquidated.

## Who measured this

The stress test, its checks and its breakage stand are the work of the **backtester department**,
reviewed and accepted on 20 Sep 2026 — including the correction, made the same day by the author,
that closing at the next minute's open is not an upper bound. The decision to publish this tail,
and the wording of what may be claimed about it, was taken on 20 Sep 2026 as well. This package — the data-path
parameter, the public downloader, the snapshot, the anchor test and this README — was assembled by
the **operations department** on 25 Sep 2026 for publication. A bot review of the same day found
four defects (the last minute of a day, the liquidation residual, seats aligned by bar index, and
a bracket sentence that did not match the tables); the backtester fixed them the same day, and the
numbers moved as `CHANGES.md` lists — the short-side liquidations had never been flagged.
