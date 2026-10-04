// Whether an agent can trade a pool at all through this repository's trader client, said before anyone pays.
//
// The client (agents/desk.py) lets one order carry at most a share of the leverage rule: equity x leverage x 0.4.
// Hyperliquid takes no order under 10 USDC. On a small account the first number is under the second, and the
// client then refuses every order that would open a position: under 10 as too small, from 10 up as over its cap.
// Seen on 4 Oct 2026 on a challenge of 3 USDC at 5x: a cap of 6.00.
//
// This is the client's caution, not a rule of the contracts and not a check of the gateway: the gateway signs an
// order up to its own cap, so the trade panel on this site is not held to it. The page says whose refusal it is.
// The two numbers are the client's own; app/tests/agentcap.test.mjs reads them back from agents/.
//
// The client counts the cap on the account's equity at the moment of the order. What a page can know before a
// stage begins is its starting capital, so this is the cap at the start: a loss lowers it, and an account that
// began just over the edge can fall under it. The words say "starting capital" for that reason.
// No browser globals here, so node's test runner loads it.

/** The share of the leverage rule one order may carry: agents/client.py, WINDOW_MAX_ORDER_SHARE_OF_RULE, which the
 *  client hands to the desk's Limits.max_order_share_of_rule. */
export const ORDER_SHARE_OF_RULE = 0.4;
/** Hyperliquid's smallest order, in USDC (agents/desk.py, MIN_ORDER_USDC). */
export const MIN_ORDER_USDC = 10;

/** The most one order may carry through the client on `equity` USDC under a leverage rule given x100. Written in
 *  the client's own order of operations, so both come to the same float. */
export function agentOrderCap(equity, leverageX100) {
  return Math.max(Number(equity), 0) * Number(leverageX100) / 100 * ORDER_SHARE_OF_RULE;
}

/** The smallest capital, to the cent and rounded up, on which the client can still send an order. */
export function minCapitalForAgent(leverageX100) {
  const lev = Number(leverageX100) / 100;
  if (!(lev > 0)) return Infinity;
  const exact = MIN_ORDER_USDC / (lev * ORDER_SHARE_OF_RULE);
  // The cent above the exact figure, unless the figure is a whole number of cents already: 25 / 5 is 5.00, not 5.01.
  const cents = Math.round(exact * 100);
  return (Math.abs(exact * 100 - cents) < 1e-9 ? cents : Math.ceil(exact * 100)) / 100;
}

/**
 * What the client allows on a pool's two stages, each of which starts with its own capital as equity.
 * `capital` and `fundedCapital` are in USDC. `challenge` and `funded` say whether an agent can open a position there.
 */
export function agentTradability({ capital, fundedCapital, leverageX100 }) {
  const challengeCap = agentOrderCap(capital, leverageX100);
  const fundedCap = agentOrderCap(fundedCapital, leverageX100);
  return {
    challengeCap,
    fundedCap,
    challenge: challengeCap >= MIN_ORDER_USDC,
    funded: fundedCap >= MIN_ORDER_USDC,
    minCapital: minCapitalForAgent(leverageX100),
  };
}

const usd = (n) => Number(n).toFixed(2);
const WHOSE = "That is the client's own limit, not the contract's or the gateway's: the trade panel on this site is not held to it.";

/** The line a pool's page carries when an agent can't trade it through the client; "" when it can. */
export function agentNote(t) {
  if (t.challenge && t.funded) return "";
  const stage = !t.challenge ? "challenge" : "funded stage";
  const cap = !t.challenge ? t.challengeCap : t.fundedCap;
  return `An agent using this repository's trader client can't open a position in this pool's ${stage}: the client `
    + `caps one order at ${usd(cap)} USDC (${ORDER_SHARE_OF_RULE} of the leverage rule on the stage's starting capital), and `
    + `Hyperliquid takes no order under ${MIN_ORDER_USDC} USDC. ${WHOSE}`;
}

/** The same on a card of the list, in one short sentence. */
export function agentCardLine(t) {
  if (t.challenge && t.funded) return "";
  const stage = !t.challenge ? "challenge" : "funded stage";
  const cap = !t.challenge ? t.challengeCap : t.fundedCap;
  return `An agent using this repository's client can't trade its ${stage}: one order is capped at ${usd(cap)} USDC, `
    + `under Hyperliquid's minimum of ${MIN_ORDER_USDC}.`;
}

/** What the new-pool form says before the pool is created; "" when an agent could trade both stages. */
export function agentFormWarning(t, leverageX100) {
  if (t.challenge && t.funded) return "";
  // A form that is not filled in yet claims nothing. A cleared field reads as 0, and a capital or a leverage of 0
  // gives a cap of 0 -- with a leverage of 0 there is no smallest capital at all, and the line used to end
  // "at least Infinity USDC". The form's own limits speak for those fields when the button is pressed.
  if (!(t.challengeCap > 0) || !(t.fundedCap > 0)) return "";
  const lev = `${(Number(leverageX100) / 100).toFixed(2).replace(/\.?0+$/, "")}×`;
  const which = !t.challenge && !t.funded ? "challenge capital and the funded capital have"
    : !t.challenge ? "challenge capital has" : "funded capital has";
  return `${agentNote(t)} With ${lev} leverage the ${which} to be at least ${usd(t.minCapital)} USDC for such an `
    + "agent to trade it.";
}
