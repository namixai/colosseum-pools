// How the platform earns: the charges planned for mainnet, and what the team's model says they come to.
//
// The rates are the plan, not what the testnet contracts do: there a withdrawal, an order and a new pool cost
// nothing, a challenge carries a flat fee on top of its price, and a shared pool takes the share of a holder's
// profit it was deployed with (0 for the current one, 10% for the earlier ones). The yearly figures are the model's,
// at the base scenario, run on 5 Oct 2026. Two of the three parts follow from the calculator on the Economics page
// -- the share of the challenge price and the charge on a withdrawal -- and app/tests/platformfees.test.mjs
// recomputes them. The third, the builder fee, rests on a trading volume the calculator's table does not carry and
// nobody has measured, and how it divides between the traders and the pool is not computed.
// No browser globals here, so node's test runner loads it.

/** The charges, as agreed on 4 and 5 Oct 2026. First rates: they may go down, not up. */
export const RATES = {
  challenge: 0.005,   // of a challenge's price, taken out of the price: the trader pays nothing on top
  withdrawal: 0.005,  // of the value an investor withdraws
  builder: 0.0005,    // of an order's notional, on Hyperliquid
  poolHype: 1,        // HYPE, once, for creating a pool
};

/**
 * A year at the base scenario, in dollars, by pool size: what each of the three percentage charges brings the
 * platform. Creating a pool is not in them. `investorReturn` is the calculator's, before any charge.
 */
export const MODELLED = [
  { pool: 100_000, challenge: 127, withdrawal: 582, builder: 891, investorReturn: 0.166 },
  { pool: 1_000_000, challenge: 1_592, withdrawal: 6_032, builder: 11_218 },
];

/** What the three charges bring the platform from one pool, a year. */
export const platformIncome = (m) => m.challenge + m.withdrawal + m.builder;

/**
 * What two of the charges cost the pool's investor, as a share of the capital a year: the share of the challenge
 * price and the charge on a withdrawal. The builder fee is left out. The model books it to the traders and does not
 * split it, yet it is taken from the trading account, which holds the pool's capital, so part of it can fall on the
 * pool. How much is not computed here.
 */
export const investorCost = (m) => (m.challenge + m.withdrawal) / m.pool;

/** The builder fee's part of the platform's income. */
export const builderShare = (m) => m.builder / platformIncome(m);

const COUNT = ["No", "One", "Two", "Three", "Four", "Five"];
const rate = (v) => `${Number((v * 100).toFixed(4))}%`;
const dollars = (v) => `$${Math.round(v).toLocaleString("en-US")}`;
const about = (v) => dollars(Math.round(v / 100) * 100);
const points = (m) => (investorCost(m) * 100).toFixed(1);

/** The section's paragraphs, as plain text. The count of charges is taken from RATES, so the two cannot differ. */
export function earnsText() {
  const [small, large] = MODELLED;
  return [
    "The platform charges for operations. It takes no share of anyone's profit, neither an investor's nor a trader's.",
    `${COUNT[Object.keys(RATES).length]} charges are planned for mainnet. A challenge costs its price and nothing on `
      + `top: ${rate(RATES.challenge)} of that price goes to the platform, and the pool receives the rest. When an `
      + `investor withdraws, ${rate(RATES.withdrawal)} of the value withdrawn is charged. Orders placed through the `
      + `platform on Hyperliquid carry a builder fee of ${rate(RATES.builder)} of the notional. Creating a pool costs `
      + `${RATES.poolHype} HYPE. These are first rates, meant to go down, not up.`,
    `In the model, at the base scenario, the first three bring the platform about ${about(platformIncome(small))} a `
      + `year from a ${dollars(small.pool)} pool. The first two fall on its investor, and cost about ${points(small)} `
      + `points of a ${(small.investorReturn * 100).toFixed(1)}% return. For a ${dollars(large.pool)} pool it is about `
      + `${about(platformIncome(large))} and ${points(large)} points. Those points leave the builder fee out. The `
      + "model books that fee to the traders and does not split it, but it is taken from the trading account, which "
      + "holds the pool's capital, so part of it can fall on the pool. It is more than half of the platform's income "
      + "here, and it rests on a trading volume nobody has measured yet.",
    "This is the design for mainnet. The testnet contracts charge nothing for a withdrawal, for an order or for "
      + "creating a pool, and the challenge fee in the factory is a flat placeholder, paid on top of the price. The "
      + "shared pool's contract can still take a share of a holder's own profit: the current shared pool was deployed "
      + "with that share at zero, and the earlier ones carry 10%.",
  ];
}
