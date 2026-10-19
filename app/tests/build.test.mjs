// node --test app/tests/build.test.mjs
//
// The commit a copy of the site was packed from, shown in its footer (app/lib/build.js, app/app.js,
// scripts/pack-site.sh). Asked for on 14 October 2026: a stable testnet demo with the commit it runs
// visible, so that anyone can hold the site they see to the code it came from.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { REPO, BUILD_FILE, buildLink } from "../lib/build.js";

const text = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const SHA = "a0f2f74c9f43e063e0c3cea72a0b9141ed596d1d";

test("the footer links the commit a packed copy names, and nothing else", () => {
  assert.equal(REPO, "https://github.com/namixai/colosseum-pools");
  assert.equal(BUILD_FILE, "build.json");
  assert.deepEqual(buildLink({ commit: SHA }), { text: "Commit a0f2f74c", href: `${REPO}/commit/${SHA}`, title: SHA });
  // Only a full commit hash, as git writes it: anything else is not shown as one.
  for (const build of [null, undefined, {}, { commit: "" }, { commit: SHA.slice(0, 8) }, { commit: `${SHA}0` }, { commit: SHA.toUpperCase() },
    { commit: `${SHA.slice(0, 39)}g` }, { commit: 12345 }, "a0f2f74c", { commit: [SHA] }]) {
    assert.equal(buildLink(build), null, JSON.stringify(build));
  }
});

test("the page shows the link only when its copy carries a commit, and the repository's own copy carries none", () => {
  const page = text("../index.html");
  assert.match(page, /<footer>[\s\S]*<a id="build" hidden target="_blank" rel="noopener"><\/a>[\s\S]*<\/footer>/);
  const app = text("../app.js");
  assert.match(app, /fetch\(BUILD_FILE, \{ cache: "no-store" \}\)/);
  assert.match(app, /const shown = buildLink\(build\);\s+if \(!shown\) return;\s+link\.textContent = shown\.text;\s+link\.href = shown\.href;\s+link\.title = shown\.title;\s+link\.hidden = false;/);
  assert.match(app, /^showBuild\(\);$/m);
  // The file is the packer's to write: a commit that held one would name a commit other than its own.
  assert.equal(existsSync(new URL("../build.json", import.meta.url)), false);
  const packer = text("../../scripts/pack-site.sh");
  assert.match(packer, /SHA="\$\(git rev-parse --verify "\$\{1:\?a commit\}\^\{commit\}"\)"/);
  assert.match(packer, /printf '\{"commit":"%s"\}\\n' "\$SHA" > "\$OUT\/build\.json"/);
  assert.match(packer, /\[ -e "\$OUT\/build\.json" \] && \{ echo "app\/build\.json is in the commit; it is the packer's to write"; exit 1; \}/);
});
