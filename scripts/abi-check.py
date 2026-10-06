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


def abi_of(contract: str) -> list[dict]:
    """The compiled ABI of `contract`, as forge prints it."""
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
    if not isinstance(abi, list) or not all(isinstance(f, dict) for f in abi):
        cannot_run(f"forge inspect printed JSON that is not an ABI: {out.stdout.strip()[:120]!r}")
    return abi


def compiled(contract: str, function: str) -> dict[str, list[tuple[str, str]]]:
    """{argument name: [(type, name), ...]} for every struct argument of `function`."""
    abi = abi_of(contract)
    fns = [f for f in abi if f.get("name") == function]
    if len(fns) != 1:
        cannot_run(f"expected one {function} in {contract}'s ABI, found {len(fns)}")
    return {i["name"]: [(c["type"], c["name"]) for c in i.get("components", [])]
            for i in fns[0]["inputs"] if i.get("components")}


def sol_enum(path: str, name: str) -> list[str]:
    """The members of `enum NAME { ... }`, with comments thrown away.

    Two test suites grew their own copy of this parser and both of them kept the comments, so a
    member with a doc comment above it came back glued to its own explanation. One copy was fixed
    and the other was not, which is how a branch stayed red for five CI runs. It lives here now,
    once, next to the other thing that compares the contracts with their copies.
    """
    src = (ROOT / path).read_text()
    m = re.search(rf"enum {name} \{{(.*?)\}}", src, re.S)
    if not m:
        cannot_run(f"no enum {name} in {path} -- did it move?")
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return [x.strip() for x in body.split(",") if x.strip()]


def listed_names(source: str, where: str) -> list[str]:
    """`STAGE = ("a", "b")` in Python or `STAGE = ["a", "b"]` in JavaScript."""
    m = re.search(r"STAGE\s*=\s*[\[(](.*?)[\])]", source, re.S)
    if not m:
        cannot_run(f"no STAGE in {where} -- did it move?")
    return re.findall(r'"([^"]*)"', m.group(1))


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


def error_signatures(contract: str) -> set[str]:
    """`Name(type,...)` for every custom error in the contract's ABI."""
    return {f"{e['name']}({','.join(i['type'] for i in e['inputs'])})"
            for e in abi_of(contract) if e.get("type") == "error"}


def py_strings(source: str, name: str) -> list[str]:
    """`NAME = (\n    "a",\n    "b",\n)` in agents/desk.py: a tuple of strings, one to a line."""
    m = re.search(rf"^{name} = \(\n(.*?)^\)", source, re.M | re.S)
    if not m:
        cannot_run(f"no {name} in agents/desk.py -- did it move?")
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    for ln in lines:
        if not re.fullmatch(r'"[^"]+",', ln):
            cannot_run(f"agents/desk.py {name} has a line that is not one string: {ln[:60]!r}")
    return [ln[1:-2] for ln in lines]


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


def read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text()
    except (OSError, UnicodeError) as error:
        cannot_run(f"could not read {rel}: {error}")


def main() -> int:
    structs = compiled("PoolFactory", "createPool")
    js = read("app/lib/chain.js")
    py = read("agents/desk.py")
    problems: list[str] = []
    for arg, name in (("rules", "RULES"), ("terms", "TERMS")):
        if arg not in structs:
            cannot_run(f"createPool has no struct argument '{arg}'")
        problems += compare(f"app/lib/chain.js {name}", structs[arg], js_struct(js, name))
        problems += compare(f"agents/desk.py {name}", structs[arg], py_struct(py, name))
    # The stages, in all three places that name them. A stage added to the contract and not to a
    # copy is not a compile error anywhere: the app renders `undefined` and the agent walks off the
    # end of its tuple, both at the moment somebody's pool is in that stage.
    stages = sol_enum("src/Pool.sol", "Stage")
    for names, where in ((listed_names(js, "app/lib/chain.js"), "app/lib/chain.js STAGE"),
                         (listed_names(py, "agents/desk.py"), "agents/desk.py STAGE")):
        if names != stages:
            problems.append(f"{where} is {names}, the contract says {stages}")

    # The custom errors. The agents' client names a contract's refusal from its own table of them,
    # because a trader's clone may never have run `forge build`. An error the table lacks comes
    # back to the trader as raw bytes; one it carries and the contracts do not is a name for
    # nothing.
    errors: set[str] = set()
    for contract in ("ChallengeAccount", "Pool"):
        errors |= error_signatures(contract)
    carried = py_strings(py, "CONTRACT_ERRORS")
    for sig in sorted(errors - set(carried)):
        problems.append(f"agents/desk.py CONTRACT_ERRORS lacks {sig}, which a contract can answer")
    for sig in sorted(set(carried) - errors):
        problems.append(f"agents/desk.py CONTRACT_ERRORS carries {sig}, which neither contract has")
    if len(carried) != len(set(carried)):
        problems.append("agents/desk.py CONTRACT_ERRORS names an error twice")

    if problems:
        print("abi-check: the contracts and their copies disagree.")
        for p in problems:
            print(f"  - {p}")
        print("Fix the copy. A wrong struct decodes a pool's terms into the wrong fields; a")
        print("missing stage makes the app render `undefined` and the agent read past its tuple,")
        print("both at the moment somebody's pool is actually in that stage.")
        return 1
    fields = sum(len(v) for v in structs.values())
    print(f"abi-check: clean ({fields} fields and {len(stages)} stages x 2 copies, {len(errors)} errors x 1).")
    return 0


if __name__ == "__main__":
    # Exit 1 happens only through the explicit mismatch path in main(). Anything unforeseen -- an
    # ABI entry of an odd shape, a missing key -- means the comparison never completed, which is
    # "could not run", with the error printed rather than a traceback that CI reads as a mismatch.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001
        cannot_run(f"stopped on {type(error).__name__}: {error}")
