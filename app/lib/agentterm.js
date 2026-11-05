// A v3 account's trading key and how long its approval lasts. Hyperliquid approves an agent key for a term the
// account names, and takes no order signed with it after that; a v3 account approves its key for AGENT_TERM from
// `agentApprovedAt` and says when that ends in `agentValidUntil`, in HyperCore's milliseconds (src/RuledAccount.sol at
// the deployed commit). No chain reads here: chain.v3Facts reads the two numbers and hands them in.
import { when } from "./ui.js";

const DAY = 86400;

/**
 * Where `account`'s key approval stands at `now` (unix seconds): "none" when no key was ever approved on it
 * (approvedAt 0, and validUntil then means nothing), "ended" from the second the approval ends, else "valid", with
 * the whole days left.
 */
export function keyTerm({ approvedAt = 0, validUntilMs = 0 } = {}, now) {
  if (!Number(approvedAt)) return { state: "none" };
  const until = Math.floor(Number(validUntilMs) / 1000);
  if (Number(now) >= until) return { state: "ended", until };
  return { state: "valid", until, days: Math.floor((until - Number(now)) / DAY) };
}

/** keyTerm in words, for an account's page. */
export function keyTermWords(term) {
  if (term.state === "none") return "No trading key is approved on this account.";
  if (term.state === "ended") {
    return `Its trading key's approval ended at ${when(term.until)}; Hyperliquid takes no order signed with it.`;
  }
  return `Its trading key is approved until ${when(term.until)}, ${term.days} day${term.days === 1 ? "" : "s"} from now.`;
}
