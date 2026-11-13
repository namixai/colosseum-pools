#!/usr/bin/env python3
"""Size gate: every contract and library this repository deploys fits a block.

    python3 scripts/size-check.py                     # builds with forge and checks
    python3 scripts/size-check.py --sizes FILE --src DIR   # checks a saved `forge build --sizes --json`

A contract's runtime code may be at most 24 576 bytes (EIP-170) and its creation code at most 49 152
(EIP-3860). HyperEVM "uses the Cancun hardfork without blobs" (its documentation,
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm, read 13 November 2026): both rules are in
Cancun, and the documentation names no exception to them. Tests run without that limit, so a contract one byte over passes every test and is refused
only when it is deployed: this gate is what goes red first. It checks every `contract` and `library`
declared under `src/`, and nothing else: the simulator of the test library (`CoreExecution`, `HyperCore`)
is over the limit and is never deployed. A contract of `src/` missing from forge's table is a failure of
the gate, not a pass. It prints each one's margin, the smallest first.

Exit 0: all fit. 1: one or more over. 2: the gate could not run.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME_LIMIT = 24_576
INIT_LIMIT = 49_152
# A declaration may be indented; `abstract contract` and `interface` are never deployed and do not match.
DECLARED = re.compile(r"^[ \t]*(?:contract|library)\s+(\w+)", re.M)
COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def declared(src: pathlib.Path) -> list[str]:
    names: list[str] = []
    for path in sorted(src.rglob("*.sol")):
        # A contract in a comment is not compiled, and forge's table has no line for it (review of #298).
        names += DECLARED.findall(COMMENT.sub("", path.read_text(encoding="utf-8-sig")))
    return sorted(set(names))


def forge_sizes() -> dict:
    # forge exits 1 when any contract of the build is over, the test library's simulator included; the table
    # is on stdout either way, and only the contracts of src/ are judged here.
    run = subprocess.run(["forge", "build", "--sizes", "--json"], cwd=ROOT, capture_output=True, text=True)
    try:
        return json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(run.stderr[-2000:])
        raise RuntimeError("forge gave no table of sizes") from exc


def main(argv: list[str]) -> int:
    sizes_file = src = None
    args = list(argv)
    while args:
        flag = args.pop(0)
        if flag == "--sizes" and args:
            sizes_file = pathlib.Path(args.pop(0))
        elif flag == "--src" and args:
            src = pathlib.Path(args.pop(0))
        else:
            print(__doc__)
            return 2
    try:
        sizes = json.loads(sizes_file.read_text()) if sizes_file else forge_sizes()
        names = declared(src or ROOT / "src")
    except Exception as exc:  # whatever stops it, the gate did not run: 2, never 1 (review of #298)
        print(f"size-check: could not run: {exc}")
        return 2
    if not names:
        print("size-check: no contract declared under src/")
        return 2
    rows, missing, over = [], [], []
    for name in names:
        s = sizes.get(name)
        if s is None:
            missing.append(name)
            continue
        runtime, init = int(s["runtime_size"]), int(s["init_size"])
        rows.append((RUNTIME_LIMIT - runtime, name, runtime, init))
        if runtime > RUNTIME_LIMIT or init > INIT_LIMIT:
            over.append(name)
    for margin, name, runtime, init in sorted(rows):
        flag = "  OVER" if name in over else ""
        print(f"  {name:28} runtime {runtime:>6}  margin {margin:>6}  init {init:>6}{flag}")
    if missing:
        print(f"size-check: not in forge's table: {', '.join(missing)}")
        return 2
    if over:
        print(f"size-check: over the limit (runtime {RUNTIME_LIMIT}, init {INIT_LIMIT}): {', '.join(over)}")
        return 1
    print(f"size-check: clean ({len(rows)} contracts and libraries of src/, the smallest margin {min(rows)[0]} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
