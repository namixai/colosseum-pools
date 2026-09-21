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

from typing import NoReturn

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def cannot_run(message: str) -> "NoReturn":
    """Exit 2: the check could not do its job. Distinct from 1, a real mismatch -- a copy that has
    moved or no longer parses must not read as "the copies disagree", nor the other way round.
    (`raise SystemExit("...")` would exit 1 whatever the message says.)"""
    print(f"abi-check: {message}")
    raise SystemExit(2)


def compiled(contract: str, function: str) -> dict[str, list[tuple[str, str]]]:
    """{argument name: [(type, name), ...]} for every struct argument of `function`."""
    try:
        out = subprocess.run(["forge", "inspect", contract, "abi", "--json"],
                             cwd=ROOT, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        cannot_run("forge is not on PATH; this check needs the contracts built")
    except subprocess.TimeoutExpired:
        cannot_run("forge inspect did not finish in 600 seconds")
    except OSError as error:  # after FileNotFoundError, which is one of these
        cannot_run(f"could not run forge inspect: {error}")
    if out.returncode != 0:
        cannot_run(f"forge inspect failed\n{out.stderr.strip()[:800]}")
    try:
        abi = json.loads(out.stdout)
    except json.JSONDecodeError:
        cannot_run(f"forge inspect did not print an ABI: {out.stdout.strip()[:120]!r}")
    fns = [f for f in abi if f.get("name") == function]
    if len(fns) != 1:
        cannot_run(f"expected one {function} in {contract}'s ABI, found {len(fns)}")
    return {i["name"]: [(c["type"], c["name"]) for c in i.get("components", [])]
            for i in fns[0]["inputs"] if i.get("components")}


def js_struct(source: str, name: str) -> list[tuple[str, str]]:
    """`const NAME = "(type name, ...)";`, including the form split over lines with `+`."""
    m = re.search(rf'const {name} =\s*((?:"[^"]*"\s*\+?\s*)+);', source)
    if not m:
        cannot_run(f"no {name} in app/lib/chain.js -- did it move?")
    text = "".join(re.findall(r'"([^"]*)"', m.group(1))).strip()
    fields = []
    for i, part in enumerate(inner(text, name, "app/lib/chain.js").split(",")):
        words = part.split()
        # Exactly a type and a name. `uint64` alone would compare as a one-item tuple and fall
        # over on the name with a traceback instead of saying which copy is malformed.
        if len(words) != 2:
            cannot_run(f"app/lib/chain.js {name} field {i} is {part.strip()!r}, not 'type name'")
        fields.append((words[0], words[1]))
    return fields


def py_struct(source: str, name: str) -> list[tuple[str, str]]:
    """`NAME = "(type,type,...)"` — types only, so the names are not compared for this copy."""
    m = re.search(rf'^{name} = "([^"]*)"', source, re.M)
    if not m:
        cannot_run(f"no {name} in agents/desk.py -- did it move?")
    fields = []
    for i, part in enumerate(inner(m.group(1), name, "agents/desk.py").split(",")):
        if len(part.split()) != 1:
            cannot_run(f"agents/desk.py {name} field {i} is {part.strip()!r}, not a bare type")
        fields.append((part.strip(), ""))
    return fields


def inner(text: str, name: str, where: str) -> str:
    text = text.strip()
    if not (text.startswith("(") and text.endswith(")")):
        cannot_run(f"{where} {name} is not a tuple: {text[:60]!r}")
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
            cannot_run(f"createPool has no struct argument '{arg}'")
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
