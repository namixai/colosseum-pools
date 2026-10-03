// The deployments the site reads (app/config.js): which one is live, which is the archive, and which one an address
// belongs to. No chain reads here; chain.js asks the factories and hands the answers in.
import { CONFIG } from "../config.js";

export const DEPLOYMENTS = CONFIG.deployments;

/** The deployment the gateway and the keepers serve: where pools are bought, traded and opened. */
export function liveDeployment(list = DEPLOYMENTS) {
  return list.find((d) => d.role === "live") ?? null;
}

/** The first deployment, kept so its records can be read and checked. It sells nothing. */
export function isArchive(deployment) {
  return deployment?.role === "archive";
}

/** Given one answer per deployment in `list`'s order (isPool, isChallenge), the deployment that said yes. */
export function pickDeployment(list, answers) {
  const i = answers.findIndex(Boolean);
  return i < 0 ? null : list[i];
}

/** A deployment named by its factory's address or by its label, as the records page names it. */
export function deploymentNamed(name, list = DEPLOYMENTS) {
  const n = String(name ?? "").toLowerCase();
  return list.find((d) => d.factory.toLowerCase() === n || d.label.toLowerCase() === n) ?? null;
}

export const ARCHIVE = {
  heading: "First deployment, kept as an archive",
  note: "These pools belong to the first deployment. It is kept so its records can be read and checked; the gateway "
    + "and the keepers serve the live deployment, so these pools sell no challenges.",
  poolNote: "This pool belongs to the first deployment, kept as an archive: it sells no challenges, because the "
    + "gateway and the keepers serve the live deployment. Its records can still be read and checked, and its investor "
    + "can still withdraw.",
  challengeNote: "This challenge belongs to the first deployment, kept as an archive: the gateway serves the live "
    + "deployment, so orders for this account are not taken. Its records can still be read and checked.",
};
