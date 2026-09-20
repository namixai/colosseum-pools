// The one number that travels from the Economics page to the "new pool" form: what one challenge
// of the pool being created costs the pool, below which selling it loses the investor money.
//
// Only this one. The calculator describes a pool of several seats with a shared reserve, and the
// form makes a single-seat pool; putting the rest of its output next to the form would say the
// numbers describe that pool. This one does describe it.
import { seatEconomics, cell, cellKey } from "./calc.js";

/** The model's own assumption about the challenge account: a tenth of the seat. */
export const CHALLENGE_RATIO = 0.1;

export function gridText(tables) {
  const seen = { dd: new Set(), daily: new Set(), target: new Set() };
  for (const key of Object.keys(tables.cells)) {
    const [, , dd, daily, target] = key.split("|");
    seen.dd.add(Number(dd) / 100); seen.daily.add(Number(daily) / 100); seen.target.add(Number(target) / 100);
  }
  const list = (s) => [...s].sort((a, b) => a - b).map((v) => `${v}%`).join(", ");
  return `drawdown ${list(seen.dd)}; daily loss ${list(seen.daily)}; target ${list(seen.target)}`;
}

/**
 * `terms` is what the form holds, in whole units: fundedCapital and capital in USDC, dd/daily/target
 * as fractions, share as a fraction. Returns `{ price }` when the model has a cell for those rules,
 * and `{ offGrid: true }` when it does not — never a guess at the nearest cell, because a floor the
 * investor is shown has to be the floor of their own rules.
 */
export function minPrice(tables, terms, scenario = "base", mode = "real") {
  const { fundedCapital, dd, daily, target, share } = terms;
  if (!(fundedCapital > 0)) return { offGrid: true };
  if (!Object.prototype.hasOwnProperty.call(tables.cells, cellKey(scenario, mode, dd, daily, target))) {
    return { offGrid: true };
  }
  const c = cell(tables, scenario, mode, dd, daily, target);
  const sc = tables.scenarios[scenario];
  const e = seatEconomics(tables, c, fundedCapital, {
    mode, share, price_pct: 0, fee_pct: 0, fee_flat: 0,
    take_profit: sc.take_profit, dd, overshoot_mean: sc.overshoot_mean,
  });
  return { price: e.min_price };
}

/**
 * How far the form's challenge capital is from the ratio the model assumes. The model prices a
 * challenge account of a tenth of the seat; a form that sets the two independently can be well away
 * from that, and then the floor above is about a different pool than the one being created.
 */
export function ratioOff(terms, tolerance = 0.02) {
  const { capital, fundedCapital } = terms;
  if (!(fundedCapital > 0) || !(capital >= 0)) return null;
  const ratio = capital / fundedCapital;
  return Math.abs(ratio - CHALLENGE_RATIO) > tolerance ? ratio : null;
}
