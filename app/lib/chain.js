// HyperEVM access: a read-only provider, the connected wallet, and the contracts.
import { CONFIG } from "../config.js";
import { ensureChain } from "./wallet.js";
export { MAX_BATCH, readAll } from "./batch.js";

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
};

export const STATUS = ["None", "Created", "Active", "Breached", "Expired", "Forfeited", "Passed", "Aborted", "Settled"];
// A pool's stages, with the one added at the end (4, "Passed, waiting for a key"), live in stages.js.
export { STAGE } from "./stages.js";
export const BREACH = ["None", "Drawdown", "Daily loss", "Leverage", "Forbidden asset"];
export const KEY_STATE = ["Unknown", "Free", "Bound", "Retired"];

export const readProvider = new ethers.JsonRpcProvider(CONFIG.rpc, CONFIG.chainId, { staticNetwork: true });

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

export function deployed() {
  return Boolean(CONFIG.factory && CONFIG.registry);
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

export function factory(runner) {
  return contract("factory", CONFIG.factory, runner);
}

export function registry(runner) {
  return contract("registry", CONFIG.registry, runner);
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
export function deployBlock() {
  return CONFIG.deployBlock || 0;
}

export function platformAssets() {
  return CONFIG.platformAssets || [];
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
