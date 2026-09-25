// "What happened on chain" (#/evidence): the records of docs/EVIDENCE.md and docs/EVIDENCE-SHARED-POOL.md,
// each with the transaction and the call or event that checks it. The rows are app/data/evidence.json, and
// app/tests/evidence.test.mjs holds every row to the two documents as they are written. Nothing in this file
// reads the chain: the page does, only when a reader asks, and these helpers put what the node answered
// next to what the row says.

export const DOCS = "https://github.com/namixai/colosseum-pools/blob/main/";

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

/** Where the app itself reads an account, or null. The verify page asks the demo's factory, so an account of
 *  another deployment gets no link: that page would call it "not ours". */
export function appLink(row) {
  if (!row.account) return null;
  if (row.kind === "shared pool") return `#/shared/${row.account}`;
  if (row.factory === "demo" && ["challenge", "pool", "seat"].includes(row.kind)) return `#/verify/${row.account}`;
  return null;
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

/** What the node's receipt says, and whether that is what the row says. `receipt` is what ethers returns:
 *  null for a transaction the node does not know. */
export function receiptVerdict(row, receipt) {
  if (!receipt) return { ok: false, text: "The node does not know this transaction." };
  const said = `block ${receipt.blockNumber}, from ${receipt.from}, to ${receipt.to}, `
    + (Number(receipt.status) === 1 ? "succeeded" : "reverted");
  const off = [];
  if (Number(receipt.status) !== 1) off.push("the transaction reverted");
  if (row.block && Number(receipt.blockNumber) !== Number(row.block)) off.push(`the document names block ${row.block}`);
  if (row.from && !same(receipt.from, row.from)) off.push(`the document names the sender ${row.from}`);
  if (row.account && !same(receipt.to, row.account)) off.push(`it went to another account than ${row.account}`);
  return off.length
    ? { ok: false, text: `The node says: ${said}. That is not what the row says: ${off.join("; ")}.` }
    : { ok: true, text: `The node says: ${said}. That is what the row says.` };
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
