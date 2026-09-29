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
# The findings the table has carried. It may grow; it may not shrink without somebody saying so.
MIN_FINDINGS = 12


def test_names() -> set[str]:
    """Every test that would actually run.

    Comment lines are thrown away first. A commented-out test still reads as a declaration to a
    regular expression, so without this a row could go on naming a test somebody had disabled --
    which is the one failure this whole file exists to make impossible.
    """
    names: set[str] = set()
    for d in TEST_DIRS:
        for f in (ROOT / d).rglob("*"):
            if f.suffix not in (".sol", ".py") or not f.is_file():
                continue
            live = "\n".join(l for l in f.read_text().split("\n")
                              if not l.lstrip().startswith(("//", "#")))
            names |= set(re.findall(r"function (test_\w+)", live))
            names |= set(re.findall(r"def (test_\w+)", live))
    return names


def main() -> int:
    if not MAP.exists():
        print(f"findings-check: {MAP} is missing", file=sys.stderr)
        return 1
    known = test_names()
    rows = [line for line in MAP.read_text().split("\n") if line.startswith("| A-")]
    if not rows:
        print("findings-check: the table has no findings in it", file=sys.stderr)
        return 1

    # A gate that only checks the rows it finds cannot tell a finding that was closed from one
    # that was deleted: both leave a shorter table and a smaller number in the summary. So the
    # numbering has to run from A-01 with no gaps, and it can only grow.
    seen = sorted(int(m.group(1)) for m in (re.match(r"\| A-(\d+)", r) for r in rows) if m)
    missing = [n for n in range(1, max(seen, default=0) + 1) if n not in seen]
    if missing:
        print(f"findings-check: A-{missing[0]:02d} is not in the table; a finding cannot be "
              f"dropped out of it", file=sys.stderr)
        return 1
    if len(seen) < MIN_FINDINGS:
        print(f"findings-check: the table has {len(seen)} findings and has had {MIN_FINDINGS}; "
              f"raise MIN_FINDINGS deliberately if that is really right", file=sys.stderr)
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
            low = pins.lower()
            marker = next((w for w in REASON_WORDS if w in low), None)
            if marker is None:
                problems.append(f"{finding}: names no test and gives no reason")
            elif len(low.replace(marker, "").strip(" .,*_`—-")) < 20:
                # "No test" on its own is a shrug, not a reason. The point of the cell is the
                # sentence after it.
                problems.append(f"{finding}: says it has no test but does not say why")
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
