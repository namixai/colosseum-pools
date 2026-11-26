// A made-up pool's menu as the chain would answer it: 24 lines, six sizes by four tiers, and `lineState` as SimPool
// computes it from the owner's switches, the free money and whether the pool is closed. The sizes, tiers and prices are
// the first pool's plan (deployments/plans/sim-first-pool.json; the test holds them to it). Used by the tests of
// app/lib/simlines.js and by the preview of the two panels; none of it is a real pool's.
const USDC = 1_000_000n;
export const SIZES = [5_000n, 10_000n, 25_000n, 50_000n, 100_000n, 250_000n].map((n) => n * USDC);
// The loss limit, the target and the kind of loss line (1: trailing up to the start): 3 / 9, 4 / 10, 5 / 10, 6 / 10 %.
export const TIERS = [[300, 900, 1], [400, 1000, 1], [500, 1000, 1], [600, 1000, 1]];
// The price of each line in dollars, size by size, tier by tier.
export const PRICES = [20, 30, 40, 50, 40, 60, 79, 95, 100, 150, 198, 238, 200, 300, 395, 475, 400, 600, 790, 950, 1000, 1500, 1975, 2375];

/** The pool's `menu()` and `tiers()`, in the order of its lines: the sizes in turn, the tiers within each. */
export function menu() {
  const m = [];
  const t = [];
  SIZES.forEach((size, s) => TIERS.forEach((tier, i) => {
    m.push([size, BigInt(PRICES[s * 4 + i]) * USDC]);
    t.push(tier);
  }));
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

/** The lines on in this made-up pool: 5 000, 10 000 and 25 000, on all four tiers, the larger off (the first pool itself
 * opens with 5 000 and 10 000 alone, deployments/plans/sim-first-pool.json). */
export const FIRST_ON = [...Array(12).keys()];
