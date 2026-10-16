// Which commit of namixai/colosseum-pools this copy of the site was packed from, for anyone who wants to
// check the site against the code. scripts/pack-site.sh writes it into build.json beside index.html when
// it packs a commit for upload. The repository's own app/ has no such file, so a copy served straight from
// a checkout shows nothing rather than a commit it may not be.
//
// No browser globals here, so node's test runner loads it (app/tests/build.test.mjs).

export const REPO = "https://github.com/namixai/colosseum-pools";
export const BUILD_FILE = "build.json";

/** The footer's link for what build.json holds, or null for anything that is not a full commit hash. */
export function buildLink(build) {
  const sha = typeof build?.commit === "string" ? build.commit : "";
  if (!/^[0-9a-f]{40}$/.test(sha)) return null;
  return { text: `Commit ${sha.slice(0, 8)}`, href: `${REPO}/commit/${sha}`, title: sha };
}
