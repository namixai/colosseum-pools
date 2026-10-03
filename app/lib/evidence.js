// "What happened on chain" (#/evidence): the records of docs/EVIDENCE.md and docs/EVIDENCE-SHARED-POOL.md,
// each with the transaction and the call or event that checks it. The rows are app/data/evidence.json, and
// app/tests/evidence.test.mjs holds every row to the two documents as they are written. Nothing in this file
// reads the chain: the page does, only when a reader asks, and these helpers put what the node answered
// next to what the row says.

import { deploymentNamed, isArchive } from "./deployments.js";
import { esc } from "./ui.js";

export const DOCS = "https://github.com/namixai/colosseum-pools/blob/main/";

// The events a row may claim, as the contracts declare them (src/Pool.sol, src/ChallengeAccount.sol; an enum is a
// uint8 in the ABI). app/tests/evidence.test.mjs holds these lines to the sources.
export const EVENTS = [
  "event FundedResult(address indexed trader, int64 realized, uint64 payout)",
  "event FundedPayoutSent(address indexed trader, uint64 amount)",
  "event Stopped(uint8 indexed status, uint8 indexed reason, int64 equity)",
];

// Every sentence the page says in its own words. The rows' records and notes are the documents' words.
export const TEXT = {
  title: "What happened on chain",
  intro: [
    "The other pages read what the contracts hold now. A stop, a pass or a payout that is over lives on only in "
      + "its transactions and events, so this page lists them: one row per thing that happened, with the account, "
      + "the transaction and how to check it yourself.",
    "Every row comes from docs/EVIDENCE.md or docs/EVIDENCE-SHARED-POOL.md. Its record is quoted from them, and a "
      + "test in the repository holds each row to what they say. To check a row without trusting this page, ask the "
      + "node: the Read the receipt button does it from your browser, and the last column names the call or event.",
    "HyperEVM testnet and mock USDC. Every wallet in these records is ours. Where the documents say how a record "
      + "was reached, staged or not and who sent it, the row quotes them.",
    "Each pool and challenge is marked with the deployment it belongs to. The first deployment is kept as an "
      + "archive: its records stand and can be checked, and it sells nothing. The second is the live one, which the "
      + "gateway and the keepers serve. A rehearsal deployment is neither, and this site does not read it. A shared "
      + "pool carries no such mark: it is not a factory's account, and the table of the three pools names the factory "
      + "its seats come from.",
  ],
  columns: ["What happened", "Account", "Transaction", "How to check it"],
  pools: "The three shared pools",
  poolColumns: ["Pool", "Factory of its seats", "Open it in the app", "What it showed"],
  limits: "What none of this shows",
  receipt: "Read the receipt",
  fills: "Read the fills",
  asking: "Asking…",
  open: "Open it in the app",
  onHyperliquid: "fills on Hyperliquid",
  noAccount: "not named in the document",
};

/** The rows in the documents' order, one group for each section of a document. */
export function groups(rows) {
  const out = [];
  for (const row of rows) {
    const last = out[out.length - 1];
    if (last && last.doc === row.doc && last.section === row.section) last.rows.push(row);
    else out.push({ doc: row.doc, section: row.section, rows: [row] });
  }
  return out;
}

/** A quote as the page shows it: a cut at either end is marked, so a fragment does not pass for a sentence. */
export function quoted(text) {
  const t = String(text).trim();
  const head = /^[A-Z]/.test(t) ? "" : "…";
  const tail = /[.!?]$/.test(t) ? "" : "…";
  return `“${head}${t}${tail}”`;
}

/** Where the app itself reads an account, or null. The verify page asks the factories of the deployments the site
 *  reads (app/config.js), live and archive, so an account of either gets a link; one of another deployment (a
 *  rehearsal) gets none: that page would call it "not ours". */
export function appLink(row) {
  if (!row.account) return null;
  if (row.kind === "shared pool") return `#/shared/${row.account}`;
  if (deploymentNamed(row.factory) && ["challenge", "pool", "seat"].includes(row.kind)) return `#/verify/${row.account}`;
  return null;
}

/**
 * The deployment a row's account belongs to, as the page marks it: the archive, the live one, or a rehearsal the
 * site does not read. Empty for a shared pool (its seats come from a factory the table of pools names) and for a
 * row of a deployment this page has no word for.
 */
export function deploymentBadge(row) {
  if (row.kind === "shared pool") return "";
  if (row.factory === "rehearsal") return "rehearsal deployment";
  const named = deploymentNamed(row.factory);
  if (!named) return "";
  return isArchive(named) ? "first deployment, archive" : "live deployment";
}

/**
 * The account cell of a row, as HTML: its kind, the deployment it belongs to, the account and the link into the
 * app; or, for a row with no account, the deployment and a word saying so. Here and not in the view, so a test
 * can read what the page actually prints.
 */
export function accountCell(row) {
  const link = appLink(row);
  // Which deployment the row is about, said on the row: the first is an archive, the second is live.
  const where = deploymentBadge(row);
  const mark = where ? `<span class="badge">${esc(where)}</span>` : "";
  return row.account
    ? `${row.kind ? `<span class="badge">${esc(row.kind)}</span> ` : ""}${mark}${mark ? "<br>" : ""}<code>${esc(row.account)}</code>`
      + (link ? `<br><a href="${esc(link)}">${esc(TEXT.open)}</a>` : "")
    : `${mark}${mark ? "<br>" : ""}<span class="muted">${esc(TEXT.noAccount)}</span>`;
}

/** Which factory to ask about a row's account, "pool" or "challenge", or null when the site reads no deployment the
 *  row names, or the row's account is not a pool or a challenge. */
export function factoryQuestion(row) {
  if (!row.account || !deploymentNamed(row.factory)) return null;
  if (row.kind === "challenge") return "challenge";
  return ["pool", "seat"].includes(row.kind) ? "pool" : null;
}

/**
 * The receipt's verdict with the deployment checked too: each row is held to the deployment it names, the first or
 * the second. `madeBy` is the deployment whose factory made the row's account (chain.deploymentOf), null when
 * neither did, undefined when the row was not asked about (factoryQuestion said null).
 */
export function withDeployment(verdict, row, madeBy) {
  if (madeBy === undefined) return verdict;
  const named = deploymentNamed(row.factory);
  if (madeBy && named && madeBy.label === named.label) {
    return verdict.ok ? { ok: true, text: `${verdict.text} The account was made by the ${named.label} factory, as the `
      + "row says." } : verdict;
  }
  const said = madeBy ? `the ${madeBy.label} factory made it` : "neither factory this site reads made it";
  return { ok: false, text: `${verdict.text} The row names the ${row.factory} deployment, but ${said}.` };
}

/** How to check a row without this page, in the order a reader would go. */
export function howToCheck(row) {
  const ways = [];
  if (row.tx) ways.push("eth_getTransactionReceipt for the transaction");
  for (const c of row.check || []) ways.push(c);
  if (row.fills?.length) ways.push(`userFills for ${row.account} on Hyperliquid's testnet info API`);
  return ways;
}

const same = (a, b) => Boolean(a && b && String(a).toLowerCase() === String(b).toLowerCase());
const listed = (words) => (words.length < 2 ? words.join("") : `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`);

/** An event as a row claims it: "FundedResult(realized 10012723, payout 1017840)". */
export function eventText(event) {
  return `${event.name}(${Object.entries(event.values).map(([k, v]) => `${k} ${v}`).join(", ")})`;
}

/** What the node's receipt says, and how much of the row that bears out. `receipt` is what ethers returns (null
 *  for a transaction the node does not know); `events` are its logs decoded with EVENTS, as { address, name, args }.
 *  A row that names an event has it looked for, from the row's account and with the row's values. For any other
 *  row the answer says that the record itself was not read, so a green line never stands for more than was
 *  compared. */
export function receiptVerdict(row, receipt, events = []) {
  if (!receipt) return { ok: false, text: "The node does not know this transaction." };
  const said = `block ${receipt.blockNumber}, from ${receipt.from}, to ${receipt.to}, `
    + (Number(receipt.status) === 1 ? "succeeded" : "reverted");
  const off = [];
  if (Number(receipt.status) !== 1) off.push("the transaction reverted");
  if (row.block && Number(receipt.blockNumber) !== Number(row.block)) off.push(`the document names block ${row.block}`);
  if (row.from && !same(receipt.from, row.from)) off.push(`the document names the sender ${row.from}`);
  if (row.account && !same(receipt.to, row.account)) off.push(`it went to another account than ${row.account}`);
  const claimed = row.event ? eventText(row.event) : "";
  const carried = row.event && events.some((e) => e.name === row.event.name && same(e.address, row.account)
    && Object.entries(row.event.values).every(([k, v]) => String(e.args?.[k]) === String(v)));
  if (row.event && !carried) off.push(`its logs carry no ${claimed} from ${row.account}`);
  if (off.length) return { ok: false, text: `The node says: ${said}. That is not what the row says: ${off.join("; ")}.` };
  const agreed = [row.block && "block", row.from && "sender", row.account && "account", row.event && `the event ${claimed}`]
    .filter(Boolean);
  const verdict = agreed.length
    ? ` ${listed(agreed).replace(/^./, (c) => c.toUpperCase())} ${agreed.length === 1 ? "is" : "are"} as the row says.`
    : "";
  return { ok: true, text: `The node says: ${said}.${verdict}${row.event ? "" : " This button does not read the record itself."}` };
}

/** Hyperliquid's fills for the account (userFills), next to the fills the row names. */
export function fillsVerdict(row, fills) {
  const named = row.fills || [];
  const found = (fills || []).filter((f) => named.some((h) => same(h, f.hash)));
  const missing = named.filter((h) => !found.some((f) => same(h, f.hash)));
  if (missing.length) {
    return { ok: false, text: `Not among the account's fills on Hyperliquid: ${missing.join(", ")}.` };
  }
  const said = found.map((f) => `${f.side === "B" ? "bought" : "sold"} ${f.sz} ${f.coin} at ${f.px}`).join("; ");
  const which = named.length === 1 ? "the fill" : named.length === 2 ? "both fills" : `all ${named.length} fills`;
  return { ok: true, text: `Hyperliquid lists ${which}: ${said}.` };
}
