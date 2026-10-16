// HyperEVM access: a read-only provider, the connected wallet, and the contracts.
import { CONFIG } from "../config.js";
import { ensureChain } from "./wallet.js";
export { MAX_BATCH, readAll } from "./batch.js";
import { PROVIDER_OPTIONS } from "./batch.js";
import { DEPLOYMENTS, liveDeployment, pickDeployment } from "./deployments.js";
import { createGentle } from "./gentle.js";
import { MULTI_ABI, EXAM_ABI } from "./multi.js";
import { MULTICALL3 } from "./multicall.js";

const { ethers } = window;

const RULES = "(uint16 dailyLossBps, uint16 maxDrawdownBps, uint32 maxLeverageX100, uint32[] assets)";
const TERMS =
  "(uint64 price, uint64 capital, uint16 targetBps, uint32 duration, uint16 traderShareChallengeBps, "
  + "uint16 traderShareFundedBps, uint64 fundedCapital)";
const CANCEL = "(uint32 asset, uint64 oid)[]";

const RULED = [
  `function rules() view returns (${RULES})`,
  `function terms() view returns (${TERMS})`,
  "function violation(uint32[] extraAssets) view returns (uint8)",
  "function agentKey() view returns (address)",
  "function day() view returns (uint32)",
  "function dayStartEquity() view returns (int64)",
  "function drawdownBase() view returns (int64)",
  "function isStopped() view returns (bool)",
  "function checkpoint()",
  "function recut(bytes32 salt)",
  "function cutKey() view returns (address)",
  "function cutBlock() view returns (uint64)",
  "event AgentSet(address indexed key)",
  "event AgentCut(address indexed oldKey, address indexed keyless)",
];

export const ABI = {
  factory: [
    "function pools() view returns (address[])",
    "function isPool(address) view returns (bool)",
    "function isChallenge(address) view returns (bool)",
    "function isPlatformAsset(uint32) view returns (bool)",
    "function challengeFee() view returns (uint256)",
    `function createPool(${RULES} rules, ${TERMS} terms) returns (address)`,
    "event PoolCreated(address indexed pool, address indexed owner)",
    "event ChallengeCreated(address indexed challenge, address indexed pool, address indexed trader)",
  ],
  pool: [
    ...RULED,
    "function owner() view returns (address)",
    "function stage() view returns (uint8)",
    "function accountReady() view returns (bool)",
    "function challenge() view returns (address)",
    "function challengeTrader() view returns (address)",
    "function heldPrice() view returns (uint256)",
    "function earned() view returns (uint256)",
    "function fundedTrader() view returns (address)",
    "function fundedStart() view returns (int64)",
    "function fundedPayoutOwed() view returns (uint64)",
    "function fundedResultTaken() view returns (bool)",
    "function fundedResult() view returns (int64)",
    // What the contract wrote down when it ended the funded stage: the pool's own breachReason.
    "function fundedEndReason() view returns (uint8)",
    "function capitalNeeded() view returns (uint64)",
    // Stage 4 only, which a pool of the older factory never reports: when the pass was recorded, and
    // how long the pool waits for a key before anyone may release it (abandonFundedStage).
    "function passedAt() view returns (uint64)",
    "function AWAIT_KEY_WINDOW() view returns (uint64)",
    "function prepareAccount()",
    "function buyChallenge() returns (address)",
    "function withdrawOnCore(uint64 amount1e8)",
    "function withdrawEarned()",
    `function breach(${CANCEL} cancels, uint32[] extraAssets, bytes32 salt)`,
    `function stopFunded(${CANCEL} cancels, uint32[] extraAssets, bytes32 salt)`,
    `function settleFunded(${CANCEL} cancels, uint32[] extraAssets)`,
    "event ChallengeSold(address indexed challenge, address indexed trader, uint256 price)",
  ],
  challenge: [
    ...RULED,
    "function pool() view returns (address)",
    "function trader() view returns (address)",
    "function status() view returns (uint8)",
    "function breachReason() view returns (uint8)",
    "function createdAt() view returns (uint64)",
    "function startedAt() view returns (uint64)",
    "function deadline() view returns (uint64)",
    "function payoutOwed() view returns (uint64)",
    "function payoutSent() view returns (uint64)",
    "function capitalArrived() view returns (bool)",
    "function keySpoiled() view returns (bool)",
    "function activate()",
    "function abort()",
    `function breach(${CANCEL} cancels, uint32[] extraAssets, bytes32 salt)`,
    `function expire(${CANCEL} cancels, uint32[] extraAssets, bytes32 salt)`,
    `function forfeit(${CANCEL} cancels, uint32[] extraAssets, bytes32 salt)`,
    "function graduate(bytes32 salt)",
    `function settle(${CANCEL} cancels, uint32[] extraAssets)`,
  ],
  registry: [
    "function bindingOf(address key) view returns ((uint8 state, address account, address trader))",
    "function keyOf(address account) view returns (address)",
    "function isBound(address key, address account, address trader) view returns (bool)",
    "function freeCount() view returns (uint256)",
    "event KeyBound(address indexed key, address indexed account, address indexed trader)",
    "event KeyRetired(address indexed key, address indexed account)",
  ],
  erc20: [
    "function balanceOf(address) view returns (uint256)",
    "function allowance(address owner, address spender) view returns (uint256)",
    "function approve(address spender, uint256 amount) returns (bool)",
  ],
  // A pool of many traders and the paper exams it sells: drafts, taken from their source (app/lib/multi.js).
  multi: MULTI_ABI,
  exam: EXAM_ABI,
};

export const STATUS = ["None", "Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted", "Settled"];
export const STAGE = ["Idle", "Challenge", "Funded", "Closing", "PassedAwaitingKey"];
export const BREACH = ["None", "Drawdown", "Daily loss", "Leverage", "Forbidden asset"];
export const KEY_STATE = ["Unknown", "Free", "Bound", "Retired"];

// What a contract fixes when it is set up and never changes: a pool's and a challenge's rules, terms and parties,
// a shared pool's factory and its terms of entry. Their answers are kept for the session (app/lib/gentle.js).
// Nothing a rule is judged by is here: a stage, a status, a verdict and a balance are read again every time.
export const FIXED_VIEWS = Object.freeze([
  "rules()", "terms()", "owner()", "trader()", "pool()", "createdAt()", "factory()", "feeBps()", "minDeposit()", "lock()",
]);
const fixedSelectors = new Set(FIXED_VIEWS.map((signature) => ethers.id(signature).slice(0, 10)));

/** sessionStorage, or nothing where the browser keeps it from the page. */
function sessionStore() {
  try {
    const store = window.sessionStorage;
    store.getItem("pools-rpc-v1:probe");
    return store;
  } catch {
    return null;
  }
}

/** One JSON-RPC batch to the node: the HTTP status and the answer as parsed, or null when it is not JSON. */
async function postRpc(batch) {
  const response = await fetch(CONFIG.rpc, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(batch),
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    // not JSON: the status says what happened
  }
  return { status: response.status, body };
}

const waitListeners = new Set();
/** Told when the reads wait for the node's rate limit, and `null` when they go again. */
export function onRpcWait(fn) {
  waitListeners.add(fn);
}

const onWait = (wait) => waitListeners.forEach((fn) => fn(wait));

/**
 * Whether the contract at Multicall3's address is Multicall3: the code the node holds there, hashed here in the
 * visitor's browser, against the hash of the code that stands at that address on other chains. Asked once; the code
 * of a contract is kept for the session like anything else that cannot change.
 */
async function isMulticall3(ask) {
  const answer = await ask({ method: "eth_getCode", params: [MULTICALL3.address, "latest"] });
  return typeof answer.result === "string" && answer.result.length > 2
    && ethers.keccak256(answer.result) === MULTICALL3.codeHash;
}

const gentle = createGentle({
  post: postRpc, staticSelectors: fixedSelectors, store: sessionStore(), onWait,
  pack: { address: MULTICALL3.address, check: isMulticall3 },
});

// The reader of the pages that check ("Check it yourself", "Read the receipt"): as gentle, with no packs and a
// memory of its own, so that what such a page shows was asked of the node call by call, for that page.
const plain = createGentle({
  post: postRpc, staticSelectors: fixedSelectors, store: sessionStore(), storePrefix: "pools-rpc-check-v1:", onWait,
});

/** What the reads cost so far: requests and calls sent, calls answered from memory, waits for the rate limit. */
export const rpcStats = () => ({ ...gentle.stats(), check: plain.stats() });

/** After a transaction of the visitor's own, the page reads what it did, not what was kept from before it. */
export const forgetLiveReads = () => {
  gentle.forgetLive();
  plain.forgetLive();
};

/** The visitor went to another page: what the page they left still had waiting is not sent. */
export const leavePage = () => {
  gentle.leave();
  plain.leave();
};

/**
 * The read provider. ethers hands it the calls a page makes; each goes through the gentle reader, which shares,
 * keeps, gathers and, on a refusal for the rate, waits and asks again before any call fails.
 */
class GentleProvider extends ethers.JsonRpcProvider {
  constructor(reader) {
    super(CONFIG.rpc, CONFIG.chainId, PROVIDER_OPTIONS);
    this.reader = reader;
  }

  async _send(payload) {
    const list = Array.isArray(payload) ? payload : [payload];
    const answers = await Promise.all(list.map((p) => this.reader.request({ method: p.method, params: p.params })));
    // A request that never got an answer fails the way fetch fails, so the page can say the node was not reached.
    const lost = answers.find((a) => a.thrown);
    if (lost) throw lost.thrown;
    return list.map((p, i) => ({ jsonrpc: "2.0", id: p.id, ...answers[i] }));
  }
}

export const readProvider = new GentleProvider(gentle);

/** The provider of the pages that check: every call goes to the node as itself, none inside a pack. */
export const checkProvider = new GentleProvider(plain);

/** The chain's own clock: the time of its latest block, in seconds. HyperCore stamps its ledger by the same clock. */
export async function blockTime() {
  const block = await readProvider.getBlock("latest");
  if (!block) throw new Error("The chain's latest block could not be read.");
  return block.timestamp;
}

let signer = null;
const listeners = new Set();

export function onAccount(fn) {
  listeners.add(fn);
}

export function currentSigner() {
  return signer;
}

export function currentAddress() {
  return signer ? signer.address : null;
}

/** Whether the deployment (the live one unless named) has its addresses in app/config.js. */
export function deployed(deployment = liveDeployment()) {
  return Boolean(deployment && deployment.factory && deployment.registry);
}

export async function connect() {
  if (!window.ethereum) throw new Error("No browser wallet found. Install Rabby or MetaMask.");
  await window.ethereum.request({ method: "eth_requestAccounts" });
  await ensureChain(window.ethereum, CONFIG);
  const provider = new ethers.BrowserProvider(window.ethereum, "any");
  signer = await provider.getSigner();
  listeners.forEach((fn) => fn(signer.address));
  return signer.address;
}

if (window.ethereum && window.ethereum.on) {
  window.ethereum.on("accountsChanged", () => {
    signer = null;
    listeners.forEach((fn) => fn(null));
  });
  window.ethereum.on("chainChanged", () => {
    signer = null;
    listeners.forEach((fn) => fn(null));
  });
}

export function contract(kind, address, runner) {
  return new ethers.Contract(address, ABI[kind], runner || readProvider);
}

export function factory(runner, deployment = liveDeployment()) {
  return contract("factory", deployment.factory, runner);
}

export function registry(runner, deployment = liveDeployment()) {
  return contract("registry", deployment.registry, runner);
}

export function usdc(runner) {
  return contract("erc20", CONFIG.usdc, runner);
}

/** Sends a write through the connected wallet and waits for it to be mined. */
export async function write(kind, address, method, args = []) {
  if (!signer) await connect();
  const c = contract(kind, address, signer);
  const tx = await c[method](...args);
  const receipt = await tx.wait();
  forgetLiveReads();
  if (!receipt || receipt.status !== 1) throw new Error(`${method} reverted`);
  return receipt;
}

export async function approveIfNeeded(spender, amount) {
  if (!signer) await connect();
  const token = usdc(signer);
  const have = await token.allowance(signer.address, spender);
  if (have >= amount) return null;
  const tx = await token.approve(spender, amount);
  return tx.wait();
}

/** The block the contracts were deployed in, from the app's config. */
/**
 * Which deployment `address` belongs to: the one whose factory says isPool (or isChallenge) for it, live first; null
 * when none made it. One read per deployment, so a page of either can be shown and checked.
 */
export async function deploymentOf(address, kind = "pool", runner = undefined) {
  const ask = kind === "challenge" ? "isChallenge" : "isPool";
  const answers = await Promise.all(DEPLOYMENTS.map((d) => factory(runner, d)[ask](address)));
  return pickDeployment(DEPLOYMENTS, answers);
}

export function deployBlock(deployment = liveDeployment()) {
  return deployment.deployBlock || 0;
}

export function platformAssets(deployment = liveDeployment()) {
  return deployment.platformAssets || [];
}

export function randomSalt() {
  return ethers.hexlify(crypto.getRandomValues(new Uint8Array(32)));
}

export function same(a, b) {
  return Boolean(a && b && a.toLowerCase() === b.toLowerCase());
}

export function short(addr) {
  return addr ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : "—";
}

/** Fixed-point bigint or number to a decimal string. */
export function units(value, decimals, shown = 2) {
  const neg = BigInt(value) < 0n;
  const abs = neg ? -BigInt(value) : BigInt(value);
  const base = 10n ** BigInt(decimals);
  const whole = abs / base;
  const frac = (abs % base).toString().padStart(decimals, "0").slice(0, shown);
  return `${neg ? "-" : ""}${whole.toLocaleString("en-US")}${shown ? "." + frac : ""}`;
}

export const usd6 = (v, shown = 2) => units(v, 6, shown);
export const usd8 = (v, shown = 2) => units(v, 8, shown);

export function toUnits(text, decimals) {
  return ethers.parseUnits(String(text).trim(), decimals);
}
