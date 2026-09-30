// What a trader pays for a challenge, and what each way it can end leaves them. Read from the pool's own terms
// and the factory's fee, and worded after src/ChallengeAccount.sol (graduate, breach, expire, forfeit, abort) and
// src/Pool.sol (onChallengeAborted refunds the price; the fee went to the platform when the challenge was bought).
import { pct, esc } from "./ui.js";

// USDC's six decimals, shown to the cent like chain.usd6; kept here so this module loads without ethers.
const usd6 = (v) => (Number(BigInt(v)) / 1e6).toFixed(2);

/** The price and the factory's fee, both in USDC's six decimals, and what they come to together. */
export function totalPrice(price, fee) {
  const p = BigInt(price), f = BigInt(fee);
  return { price: p, fee: f, total: p + f };
}

/** One line for a pool's card: the whole of what the buyer pays, and which part never comes back. */
export function totalPriceWords(price, fee) {
  const t = totalPrice(price, fee);
  return t.fee > 0n
    ? `${usd6(t.total)} USDC — the price ${usd6(t.price)} and the platform's fee ${usd6(t.fee)}, which isn't refunded`
    : `${usd6(t.total)} USDC`;
}

const share = (bps) => (Number(bps) === 0 ? "None" : pct(bps));

/** The ways a challenge ends, each with what the trader gets and what happens to what they paid. */
export function outcomes(terms, fee) {
  const t = totalPrice(terms.price, fee);
  const kept = t.fee > 0n ? "The price and the fee are not refunded." : "The price is not refunded.";
  return [
    {
      what: `You pass: within the time limit the account reaches the ${pct(terms.targetBps)} target with every `
        + "position closed and no rule broken.",
      you: `${share(terms.traderShareChallengeBps)} of the challenge's profit; then the pool funds you with `
        + `${usd6(terms.fundedCapital)} USDC and you keep ${share(terms.traderShareFundedBps).toLowerCase()} of what that `
        + "stage earns.",
      paid: kept,
    },
    {
      what: "A rule is broken — the daily loss, the drawdown, the leverage, or an asset off the list — and anyone "
        + "stops the challenge.",
      you: "Nothing.",
      paid: kept,
    },
    { what: "The time limit runs out.", you: "Nothing.", paid: kept },
    { what: "You walk away.", you: "Nothing.", paid: kept },
    {
      what: "It never starts: the capital does not reach the challenge account within an hour, or the key reserved "
        + "for you cannot be used.",
      you: "Nothing to trade.",
      paid: t.fee > 0n ? "The price comes back to your wallet; the fee does not." : "The price comes back to your wallet.",
    },
  ];
}

export function outcomesHtml(terms, fee) {
  const rows = outcomes(terms, fee).map((o) => `<tr><td>${esc(o.what)}</td><td>${esc(o.you)}</td><td>${esc(o.paid)}</td></tr>`)
    .join("");
  return `<div class="scroll"><table>
    <thead><tr><th>How it ends</th><th>What you get</th><th>What you paid</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}
