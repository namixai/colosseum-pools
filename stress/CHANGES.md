# Changes

## 25 Sep 2026 — after the bot review of the public package

Six review notes; four touched the tool or the text, one the anchor, one the recipe. Every fix has a
check in `test_pool_stress.py` or `test_anchor.py` and a breakage in `mut_pool_stress.py` that turns
it red again.

1. **Last minute of a day** (`pool_stress.py`, `episode`). When the line was crossed in the last
   minute of a day and `--lag` was 1 or 2, the exit was taken at the *open* of that same minute — a
   price from before the crossing. Now the exit is that minute's close, the last price the keeper
   would see, and the path to liquidation runs through the whole minute. Moves a handful of
   late-day entries by 0.01 pp in the per-coin means and medians (2026-06-05, short side); no
   headline number.
2. **What a liquidated seat keeps** (`episode`). The tool kept the maintenance margin (about 8 % of
   the seat at 5× and 2 %, 11.8 % for a short) while the README said "the seat loses everything".
   Now it is a bracket, like every other figure here: at the lower bound (`--exec open`) the seat
   keeps the margin, at the upper bound (`--exec worst`) it loses everything. Hyperliquid's
   documentation: a liquidation through the book leaves "any remaining collateral" with the trader;
   a backstop liquidation (equity below 2/3 of the maintenance margin) "is not returned to the
   user". This is what moves the upper ends below.
3. **Seats aligned by time** (`seats_report`, new `entry_minutes`). The five seats were entered by
   bar *index*; with a minute missing in one coin that is a different moment per coin. Now the
   entries are the minutes present in every coin, matched by timestamp. The snapshot has no gaps,
   so nothing moved; the tool is now safe for data that has them.
4. **The bracket sentence.** The README claimed every range was `--lag 0` → `--exec worst`; every
   table was in fact `--lag 1 --exec open` → `--lag 1 --exec worst`. The text now says what the
   tables do, and the anchor checks the sentence.
5. **The recipe** ran `test_anchor.py` from `/tmp/fresh`, where it does not exist. Fixed; the anchor
   parses the recipe and fails if the working directory drifts again.
6. **The anchor** compared three sections field by field and skipped the rule block — the published
   `lag1` file lacked the `исполнение` key the tool now writes. The anchor compares every section,
   the field sets included, and the layout file too. `make_results.py` is the one command that
   writes `results/`, so nothing in it is hand-made.

### A defect the review did not name, found while re-running

**Short-side liquidations were never flagged.** The liquidation flag compared the seat's equity to
`mm × leverage × equity_in`, which is the residual of a *long* at its liquidation price; a short's
notional grows with the price, so its residual (11.8 %) sat above that threshold and the flag stayed
off while the loss was already capped at the liquidation price. The published "88.5 %" for "all
five seats on SOL" was that cap: five liquidated shorts with the margin counted as kept. The flag is
now set by the price path, not by a threshold.

### Numbers that moved (open → worst; the open end never moved)

| figure | 20 Sep files | 25 Sep files |
|---|---|---|
| wide list, 10 Oct, worst entry 21:18 | 83.7–87.1 % ($75 299–$78 362) | **83.7–94.1 %** ($75 299–$84 712) |
| wide list, 10 Oct, short side, worst entry 21:21 / 21:22 | 80.0–84.8 %, "0 liquidated" | 80.0–95.7 %, 3–4 liquidated |
| default list, big seats on the calm coins | 23.9–39.3 %, none liquidated | **23.9–40.0 %**, three SOL seats (short, worst price) |
| default list, big seat on SOL | 31.7–63.6 %, none | **31.7–70.0 %**, the SOL seat (short, worst price) |
| default list, all five seats on SOL | 45.2–88.5 %, none | **45.2–100 %**, all five (short, worst price) |
| per coin, 10 Oct, SOL short, worst | 88.46 % | 100.0 % |
| overshoot, maximum | 89 pp | 89 pp at the open, 97 pp at the worst price |
| liquidated share, worst price, per side | long 0.02 %, short 0.00 % | long 0.02 %, short 0.02 % |

Unchanged: every lower bound, every median, both p99 columns, "three of five seats liquidated" on
the long side of the wide list, "seven of eight days over the cap, three over 20 %".
