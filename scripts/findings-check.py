#!/usr/bin/env python3
"""Holds docs/AUDIT-FINDINGS.md to the repository.

The document claims that each audit finding is pinned by a named test. A claim like that is worth
exactly as much as the thing that checks it, so this does: every test named in the table has to
exist in the test sources, and every row with no test has to say why in words rather than leaving
the cell blank.

    scripts/findings-check.py            # and it prints what it checked

It does not run the tests. Forge and unittest do that; this only makes sure the map points at
something real, which is the failure mode a passing suite cannot catch -- a row naming a test that
was renamed out from under it reads exactly like a row naming a test that works.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAP = ROOT / "docs/AUDIT-FINDINGS.md"
TEST_DIRS = ["test", "ops/tests", "gateway/tests"]
# A row that names no test has to explain itself; these are the words that count as an explanation
# rather than a shrug.
REASON_WORDS = ("no test", "no behaviour test", "no contract test")


def test_names() -> set[str]:
    names: set[str] = set()
    for d in TEST_DIRS:
        for f in (ROOT / d).rglob("*"):
            if f.suffix in (".sol", ".py") and f.is_file():
                names |= set(re.findall(r"function (test_\w+)", f.read_text()))
                names |= set(re.findall(r"def (test_\w+)", f.read_text()))
    return names


def main() -> int:
    if not MAP.exists():
        print(f"findings-check: {MAP} is missing", file=sys.stderr)
        return 1
    known = test_names()
    rows = [l for l in MAP.read_text().split("\n") if l.startswith("| A-")]
    if not rows:
        print("findings-check: the table has no findings in it", file=sys.stderr)
        return 1

    problems, pinned, unpinned = [], 0, 0
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 5:
            problems.append(f"{cells[0]}: the row is malformed")
            continue
        finding, status, pins = cells[0], cells[3], cells[4]
        named = re.findall(r"`(test_\w+)`", pins)
        if named:
            pinned += 1
            for n in named:
                if n not in known:
                    problems.append(f"{finding}: names {n}, which no test file defines")
        else:
            unpinned += 1
            if not any(w in pins.lower() for w in REASON_WORDS):
                problems.append(f"{finding}: names no test and gives no reason")
        if not status:
            problems.append(f"{finding}: has no status")

    for p in problems:
        print(f"findings-check: {p}", file=sys.stderr)
    if problems:
        print(f"findings-check: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print(f"findings-check: {len(rows)} findings, {pinned} pinned by tests that exist, "
          f"{unpinned} with a stated reason for having none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
