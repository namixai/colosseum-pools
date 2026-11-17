// A made-up simulated pool's menu as the chain would answer it: 24 lines, six sizes by four tiers, and `lineState` as
// SimPool computes it from the owner's switches, the free money and whether the pool is closed. Used by the tests of
// app/lib/simlines.js and by the preview of the two panels; none of it is a real pool's.
const USDC = 1_000_000n;
export const SIZES = [5_000n, 10_000n, 25_000n, 50_000n, 100_000n, 250_000n].map((n) => n * USDC);
// 3 / 9, 4 / 10, 5 / 10 and 6 / 10 %, each with the loss line that trails up to the start (drawdownKind 1).
export const TIERS = [[900, 300, 1], [1000, 400, 1], [1000, 500, 1], [1000, 600, 1]];
// Prices are the fixture's own: a share of the size that rises with the tier.
const PRICE_BPS = [90n, 110n, 130n, 150n];

/** The pool's `menu()` and `tiers()`, in the order of its lines: the sizes in turn, the tiers within each. */
export function menu() {
  const m = [];
  const t = [];
  for (const size of SIZES) {
    TIERS.forEach((tier, i) => {
      m.push([size, size * PRICE_BPS[i] / 10_000n]);
      t.push(tier);
    });
  }
  return { menu: m, tiers: t };
}

/** `lineState(line)` as SimPool answers it (`_short`, `lineState`): COVER_PAYOUTS is 2, a payout 8 % of the size. */
export function lineStates({ on, free, closed = false }) {
  const { menu: m } = menu();
  return m.map(([size], line) => {
    const need = size * 800n / 10_000n * 2n;
    const short = free >= need ? 0n : need - free;
    const canSwitchOn = short === 0n && !closed;
    const isOn = on.includes(line);
    return [isOn, isOn && canSwitchOn, canSwitchOn, short];
  });
}

/** The lines the first pool opens with: 5 000, 10 000 and 25 000 on, on all four tiers, the larger off. */
export const FIRST_ON = [...Array(12).keys()];
