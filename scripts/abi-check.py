#!/usr/bin/env python3
"""The structs the contracts define, against the copies of them the app and the agents carry.

    ./scripts/abi-check.py            # exit 0 clean, 1 a mismatch, 2 the check could not run

`Rules` and `Terms` cross three languages. Solidity defines them; `app/lib/chain.js` repeats them
as an ethers signature; `agents/desk.py` repeats their types for the call encoder. Change the
struct and forget one copy and nothing fails loudly: the app decodes a pool's terms into the wrong
fields, or refuses to decode them at all, and the first place anyone notices is a pool that will
not open. That is a silent failure with a long fuse, and it fires on the day someone is watching.

So: read the compiled ABI, read the two copies, compare. Types always; names too where the copy
carries them, because a swap of two fields of the same width is exactly the mistake that is
invisible otherwise.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def compiled(contract: str, function: str) -> dict[str, list[tuple[str, str]]]:
    """{argument name: [(type, name), ...]} for every struct argument of `function`."""
    try:
        out = subprocess.run(["forge", "inspect", contract, "abi", "--json"],
                             cwd=ROOT, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        print("abi-check: forge is not on PATH; this check needs the contracts built")
        raise SystemExit(2)
    if out.returncode != 0:
        print(f"abi-check: forge inspect failed\n{out.stderr.strip()[:800]}")
        raise SystemExit(2)
    abi = json.loads(out.stdout)
    fns = [f for f in abi if f.get("name") == function]
    if len(fns) != 1:
        print(f"abi-check: expected one {function} in {contract}'s ABI, found {len(fns)}")
        raise SystemExit(2)
    return {i["name"]: [(c["type"], c["name"]) for c in i.get("components", [])]
            for i in fns[0]["inputs"] if i.get("components")}


def js_struct(source: str, name: str) -> list[tuple[str, str]]:
    """`const NAME = "(type name, ...)";`, including the form split over lines with `+`."""
    m = re.search(rf'const {name} =\s*((?:"[^"]*"\s*\+?\s*)+);', source)
    if not m:
        raise SystemExit(f"abi-check: no {name} in app/lib/chain.js — did it move? (exit 2)")
    text = "".join(re.findall(r'"([^"]*)"', m.group(1))).strip()
    return [tuple(part.split()) for part in inner(text, name).split(",")]  # type: ignore[misc]


def py_struct(source: str, name: str) -> list[tuple[str, str]]:
    """`NAME = "(type,type,...)"` — types only, so the names are not compared for this copy."""
    m = re.search(rf'^{name} = "([^"]*)"', source, re.M)
    if not m:
        raise SystemExit(f"abi-check: no {name} in agents/desk.py — did it move? (exit 2)")
    return [(part.strip(), "") for part in inner(m.group(1), name).split(",")]


def inner(text: str, name: str) -> str:
    text = text.strip()
    if not (text.startswith("(") and text.endswith(")")):
        raise SystemExit(f"abi-check: {name} is not a tuple: {text[:60]!r} (exit 2)")
    return text[1:-1]


def compare(label: str, solidity: list[tuple[str, str]], copy: list[tuple[str, str]]) -> list[str]:
    """Names are compared only where the copy carries one: desk.py keeps types alone on purpose."""
    problems = []
    if len(solidity) != len(copy):
        problems.append(f"{label}: {len(copy)} fields, the contract has {len(solidity)}")
    for i, (want, got) in enumerate(zip(solidity, copy)):
        if want[0] != got[0]:
            problems.append(f"{label} field {i}: type {got[0]}, the contract says {want[0]}")
        elif got[1] and want[1] != got[1]:
            problems.append(f"{label} field {i}: named {got[1]}, the contract calls it {want[1]}")
    return problems


def main() -> int:
    structs = compiled("PoolFactory", "createPool")
    js = (ROOT / "app/lib/chain.js").read_text()
    py = (ROOT / "agents/desk.py").read_text()
    problems: list[str] = []
    for arg, name in (("rules", "RULES"), ("terms", "TERMS")):
        if arg not in structs:
            print(f"abi-check: createPool has no struct argument '{arg}'")
            return 2
        problems += compare(f"app/lib/chain.js {name}", structs[arg], js_struct(js, name))
        problems += compare(f"agents/desk.py {name}", structs[arg], py_struct(py, name))
    if problems:
        print("abi-check: the contracts and their copies disagree.")
        for p in problems:
            print(f"  - {p}")
        print("Fix the copy, or the app decodes a pool's terms into the wrong fields.")
        return 1
    fields = sum(len(v) for v in structs.values())
    print(f"abi-check: clean ({fields} fields x 2 copies).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
