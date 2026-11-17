#!/usr/bin/env python3
"""The structs the contracts define, against the copies of them the app and the agents carry.

    ./scripts/abi-check.py            # exit 0 clean, 1 a mismatch, 2 the check could not run

`Rules` and `Terms` cross three languages. Solidity defines them; `app/lib/chain.js` repeats them
as an ethers signature; `agents/desk.py` repeats their types for the call encoder. Change the
struct and forget one copy and nothing fails loudly: the app decodes a pool's terms into the wrong
fields, or refuses to decode them at all, and the first place anyone notices is a pool that will
not open. That is a silent failure with a long fuse, and it fires on the day someone is watching.

The app's libraries of the simulated pool and the paper exam (`app/lib/simpool.js`, `exam.js`,
`operator.js`) write their ABIs and call signatures out by hand; each function, event and call is held
to the compiled ABI of its contract too (LIB_ABIS, LIB_CALLS).

So: read the compiled ABI, read the two copies, compare. Types always; names too where the copy
carries them, because a swap of two fields of the same width is exactly the mistake that is
invisible otherwise.
"""
from __future__ import annotations

from typing import NoReturn

import json
import os
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


def js_struct(source: str, name: str, where: str = "app/lib/chain.js") -> list[tuple[str, str]]:
    """`const NAME = "(type name, ...)";`, including the form split over lines with `+`."""
    m = re.search(rf'''const {name}\s*=\s*((?:(?:"[^"]*"|'[^']*')\s*\+?\s*)+);''', source)
    if not m:
        cannot_run(f"no {name} in {where} -- did it move?")
    text = "".join(a + b for a, b in re.findall(r"\"([^\"]*)\"|'([^']*)'", m.group(1))).strip()
    fields = []
    for i, part in enumerate(inner(text, name, where).split(",")):
        words = part.split()
        # Exactly a type and a name. `uint64` alone would compare as a one-item tuple and fall
        # over on the name with a traceback instead of saying which copy is malformed.
        if len(words) != 2:
            cannot_run(f"{where} {name} field {i} is {part.strip()!r}, not 'type name'")
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


# Every copy of Rules outside the contracts, wherever it is (the weekly architecture audit, 26 October
# 2026: five files held one, and this check read two; v3's fifth field was put into each by hand).
# Each copy is either the contracts' Rules or the other side of v3 from them -- the contracts' with
# assetLeverageX100 appended, or the contracts' without it -- and nothing else, so a reader of either
# deployment keeps working and a drift anywhere fails CI.
SCANNED = ("app", "agents", "gateway", "ops", "spike")
SKIPPED_DIRS = {"tests", ".venv", "node_modules", "results", "__pycache__", "data"}
SOURCE_SUFFIXES = (".py", ".js", ".mjs")
V3_FIELD = ("uint32[]", "assetLeverageX100")
# Either quote, any spacing: a copy spelled another way must not slip past (Gemini on cp#173).
TYPE_TUPLE = re.compile(r'''(['"])(\(\s*(?:[a-z0-9\[\]]+\s*,\s*)*[a-z0-9\[\]]+\s*\))\1''')
RULES_CONST = re.compile(r'''^\s*(\w*RULES\w*)\s*=\s*['"]\(''')
RULES_READ = re.compile(r'''(['"])rules\(\)\1''')
# The contract each signature that carries Rules goes to: a signature naming another function is
# reported, so a new one is added here rather than read past.
SIGNATURE_CONTRACTS = {"createPool": "PoolFactory", "addSeat": "SharedPool"}


def sources() -> list[tuple[str, str]]:
    """(path, text) of every source file under SCANNED: tests, environments and data left out."""
    out = []
    for top in SCANNED:
        for here, dirs, files in os.walk(ROOT / top, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
            for f in sorted(files):
                if f.endswith(SOURCE_SUFFIXES):
                    path = pathlib.Path(here) / f
                    out.append((path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")))
    return out


def rules_shapes(contract_rules: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The contracts' Rules and the other side of v3: (before v3, v3)."""
    if contract_rules and contract_rules[-1] == V3_FIELD:
        return contract_rules[:-1], contract_rules
    return contract_rules, contract_rules + [V3_FIELD]


def unnamed_copies(rel: str, text: str) -> list[tuple[int, str]]:
    """(line, type string) of each bare-type copy of Rules in a file: a constant named for it, the
    type a `rules()` read decodes with, or a tuple that begins as Rules does."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in TYPE_TUPLE.finditer(line):
            t = re.sub(r"\s+", "", m.group(2))
            if RULES_CONST.match(line) or RULES_READ.search(line) or t.startswith("(uint16,uint16,uint32,"):
                found.append((n, t))
    return found


def check_copies(contract_rules: list[tuple[str, str]]) -> tuple[list[str], int]:
    """Every copy of Rules under SCANNED, held to the two shapes. Returns the problems and how many
    copies there were."""
    before, v3 = rules_shapes(contract_rules)
    shapes = ([t for t, _ in before], [t for t, _ in v3])
    problems, count = [], 0
    for rel, text in sources():
        for n, t in unnamed_copies(rel, text):
            count += 1
            if inner(t, "Rules", f"{rel}:{n}").split(",") not in shapes:
                problems.append(f"{rel}:{n} {t} is neither the contracts' Rules nor v3's")
        if rel.endswith((".js", ".mjs")):
            for name in sorted(set(re.findall(r"^\s*(?:export\s+)?const (\w*RULES\w*)\s*=", text, re.M))):
                count += 1
                fields = js_struct(text, name, rel)
                if fields not in (before, v3):
                    problems.append(f"{rel} {name} is neither the contracts' Rules nor v3's, field for field")
    return problems, count


def canonical(entry: dict) -> str:
    """An ABI input's type as a signature spells it: a tuple by its components."""
    t = entry["type"]
    if t.startswith("tuple"):
        return "(" + ",".join(canonical(c) for c in entry["components"]) + ")" + t[len("tuple"):]
    return t


def check_signatures(contract_rules: list[tuple[str, str]]) -> tuple[list[str], int]:
    """Each Python signature built from constants that carries the contracts' Rules is one its
    contract declares. One that carries the other side of v3 is for a deployment of that other
    side, and its shape is already held by check_copies."""
    current = "(" + ",".join(t for t, _ in contract_rules) + ")"
    declared: dict[str, set[str]] = {}
    problems, count = [], 0
    for rel, text in sources():
        if not rel.endswith(".py"):
            continue
        consts = {m.group(1): m.group(3) for m in re.finditer(r'''^(\w+)\s*=\s*(['"])(.*?)\2\s*$''', text, re.M)}
        for n, line in enumerate(text.splitlines(), 1):
            # f-strings built from the constants, and signatures written out whole.
            for _, fn, args in re.findall(r'''f?(['"])(\w+)\(([^'"]*)\)\1''', line):
                resolved = re.sub(r"\{(\w+)\}", lambda m: consts.get(m.group(1), m.group(0)), args)
                # A signature has no spaces, whatever the source's spacing.
                resolved = re.sub(r"\s+", "", resolved)
                if "{" in resolved or current not in resolved:
                    continue
                count += 1
                contract = SIGNATURE_CONTRACTS.get(fn)
                if contract is None:
                    problems.append(f"{rel}:{n} {fn}({resolved}) carries Rules to a function this check does not know")
                    continue
                if contract not in declared:
                    declared[contract] = {f"{f['name']}({','.join(canonical(i) for i in f['inputs'])})"
                                          for f in abi_of(contract) if f.get("type") == "function"}
                if f"{fn}({resolved})" not in declared[contract]:
                    problems.append(f"{rel}:{n} {fn}({resolved}) is not a function {contract} declares")
    return problems, count


# ── the app's libraries of the simulated pool and the paper exam ─────────────────────────────────────────────────

# Each ABI an app library writes out by hand, and the contracts whose compiled ABI it is held to (the weekly
# architecture audit, 16 November 2026, finding A): change an argument of `SimPool.buy` and the contract's tests
# and the site's stay green while every purchase fails. A constant named *_ABI in one of these files that this
# table does not name is reported, so a new one is added here rather than read past.
LIB_ABIS = {
    "app/lib/simpool.js": {"SIM_POOL_ABI": ("SimPool",), "SIM_POOL_STOP_RESERVE_ABI": ("SimPool",),
                           "SIM_POOL_APPROVAL_ABI": ("SimPool",), "SIM_FACTORY_ABI": ("SimPoolFactory",),
                           "PAPER_EXAM_EVENTS_ABI": ("PaperExam",)},
    "app/lib/exam.js": {"PAPER_EXAM_ABI": ("PaperExam",)},
    "app/lib/operator.js": {"OPERATOR_ABI": ("SimPool",)},
    "app/lib/simlines.js": {"SIM_LINES_ABI": ("SimPool",), "SIM_OWNER_ABI": ("SimPool",), "TERMS_BOOK_ABI": ("TermsBook",)},
}
# The contract each library's `call(target, "name(types)", ...)` sends to.
LIB_CALLS = {"app/lib/exam.js": "PaperExam", "app/lib/operator.js": "SimPool"}
HUMAN_ITEM = re.compile(r"^(function|event|error)\s+(\w+)\s*\((.*)$")


def split_top(text: str, where: str) -> list[str]:
    """`text` split at the commas outside parentheses."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        depth += (ch == "(") - (ch == ")")
        if depth < 0:
            cannot_run(f"{where}: unbalanced parentheses in {text[:80]!r}")
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if depth:
        cannot_run(f"{where}: unbalanced parentheses in {text[:80]!r}")
    return [x.strip() for x in parts + [cur] if x.strip()]


def closing(text: str, where: str) -> int:
    """The index of the parenthesis that closes the one `text` opens with."""
    depth = 0
    for i, ch in enumerate(text):
        depth += (ch == "(") - (ch == ")")
        if depth == 0:
            return i
    cannot_run(f"{where}: unbalanced parentheses in {text[:80]!r}")


def human_param(text: str, where: str) -> dict:
    """One parameter of a human-readable ABI line, as the compiled ABI writes it: type, components, name, indexed."""
    text = text.strip()
    if text.startswith("("):
        end = closing(text, where)
        m = re.match(r"((?:\[\d*\])*)\s*(.*)$", text[end + 1:])
        out = {"type": "tuple" + m.group(1), "components": [human_param(c, where) for c in split_top(text[1:end], where)]}
        words = m.group(2).split()
    else:
        words = text.split()
        if not words:
            cannot_run(f"{where}: an empty parameter")
        # `uint` and `int` are `uint256` and `int256`, bare or as the element of an array: `uint[]`, `int[3]`.
        out = {"type": re.sub(r"^(u?int)(?!\d)", r"\g<1>256", words.pop(0))}
    out["indexed"] = "indexed" in words
    words = [w for w in words if w != "indexed"]
    if len(words) > 1:
        cannot_run(f"{where}: {text!r} is not 'type [indexed] [name]'")
    out["name"] = words[0] if words else ""
    return out


def human_item(line: str, where: str) -> dict:
    """A line of an ethers human-readable ABI as a compiled ABI entry: type, name, inputs, outputs, mutability."""
    m = HUMAN_ITEM.match(line.strip())
    if not m:
        cannot_run(f"{where}: {line[:80]!r} is not a function, event or error")
    kind, name, rest = m.groups()
    end = closing("(" + rest, where)
    inputs = [human_param(p, where) for p in split_top(rest[:end - 1], where)]
    tail = rest[end:].strip()
    entry = {"type": kind, "name": name, "inputs": inputs, "outputs": [], "mutability": "nonpayable"}
    if kind != "function":
        if tail:
            cannot_run(f"{where}: {line[:80]!r} has words after its parameters")
        return entry
    r = re.match(r"^((?:view|pure|payable|nonpayable)?)\s*(?:returns\s*(\(.*\)))?$", tail)
    if not r:
        cannot_run(f"{where}: {line[:80]!r} ends in something other than a mutability and returns (...)")
    entry["mutability"] = r.group(1) or "nonpayable"
    if r.group(2):
        entry["outputs"] = [human_param(p, where) for p in split_top(r.group(2)[1:-1], where)]
    return entry


def js_abi(source: str, name: str, where: str) -> list[str]:
    """`export const NAME = [ "…", "…" ];`: the strings, one a line, each in double quotes."""
    m = re.search(rf"^(?:export\s+)?const {name}\s*=\s*\[\n(.*?)^\];", source, re.M | re.S)
    if not m:
        cannot_run(f"no {name} in {where} -- did it move?")
    lines = []
    for ln in m.group(1).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("//"):
            continue
        if not re.fullmatch(r'"[^"]+",?', ln):
            cannot_run(f"{where} {name} has a line that is not one string: {ln[:60]!r}")
        lines.append(ln.rstrip(",")[1:-1])
    return lines


def is_static(param: dict) -> bool:
    """A dynamic type is encoded behind an offset word: a dynamic array, `string`, `bytes`, and a tuple holding one.
    A fixed array is static when its element is."""
    t = param["type"]
    if t.endswith("[]"):
        return False
    if t.endswith("]"):
        return is_static({**param, "type": t[:t.rindex("[")]})
    if t in ("string", "bytes"):
        return False
    if t == "tuple":
        return all(is_static(c) for c in param["components"])
    return True


def same_fields(label: str, copy: list[dict], compiled_: list[dict], prefix: bool) -> list[str]:
    """Types always, names where both sides carry one; indexed for events. With `prefix`, the copy may leave off
    fields at the end: a static tuple or a list of outputs decoded short reads its first fields alike."""
    problems = []
    if len(copy) > len(compiled_) or (not prefix and len(copy) != len(compiled_)):
        return [f"{label}: {len(copy)} fields, the contract has {len(compiled_)}"]
    for i, (got, want) in enumerate(zip(copy, compiled_)):
        here = f"{label} field {i}"
        if got["type"] != want["type"]:
            problems.append(f"{here}: type {canonical(got)}, the contract says {canonical(want)}")
            continue
        if got["name"] and want["name"] and got["name"] != want["name"]:
            problems.append(f"{here}: named {got['name']}, the contract calls it {want['name']}")
        if got.get("indexed", False) != want.get("indexed", False):
            problems.append(f"{here}: indexed {got.get('indexed', False)}, the contract says {want.get('indexed', False)}")
        if got["type"].startswith("tuple"):
            # Only a lone static tuple at the end may be decoded short: inside an array each element's size counts,
            # and a dynamic tuple sits behind an offset word, so its first fields are not at the start of the data.
            last = prefix and i == len(copy) - 1 and got["type"] == "tuple" and is_static(want)
            problems += same_fields(here, got["components"], want["components"], last)
    return problems


def mutability(entry: dict) -> str:
    return "view" if entry.get("stateMutability") in ("view", "pure") else entry.get("stateMutability", "nonpayable")


def check_libraries() -> tuple[list[str], int]:
    """Every function, event and error the three app libraries write out, and every call they build from a
    signature, against the compiled ABI of the contract it goes to: the selector or topic, each field's type,
    names where both name it, indexed, and whether a function only reads. Outputs may be read short at the end,
    for a pool from before a field was added (a payout's `stoppedAt`, `approved`)."""
    problems, count, compiled_abis = [], 0, {}

    def abi(contract: str) -> list[dict]:
        if contract not in compiled_abis:
            compiled_abis[contract] = abi_of(contract)
        return compiled_abis[contract]

    for rel, consts in LIB_ABIS.items():
        text = read(rel)
        # Found by name alone, whatever the initializer is (`[...]`, `Object.freeze([...])`): js_abi then refuses
        # the ones it cannot read, and the ones this table does not know are named here.
        found = set(re.findall(r"^(?:export\s+)?(?:const|let|var)\s+(\w+_ABI)\b", text, re.M))
        for name in sorted(found - set(consts)):
            problems.append(f"{rel} {name} is an ABI this check does not know: add it to LIB_ABIS")
        for name, contracts in consts.items():
            for line in js_abi(text, name, rel):
                count += 1
                item = human_item(line, f"{rel} {name}")
                sig = f"{item['name']}({','.join(canonical(i) for i in item['inputs'])})"
                label = f"{rel} {name} {item['type']} {sig}"
                matches = [e for c in contracts for e in abi(c) if e.get("type") == item["type"]
                           and f"{e['name']}({','.join(canonical(i) for i in e['inputs'])})" == sig]
                if not matches:
                    problems.append(f"{label}: {' or '.join(contracts)} declares no such {item['type']}")
                    continue
                want = matches[0]
                problems += same_fields(f"{label} inputs", item["inputs"], want["inputs"], False)
                if item["type"] == "function":
                    problems += same_fields(f"{label} outputs", item["outputs"], want["outputs"], True)
                    if item["mutability"] in ("view", "pure"):
                        item["mutability"] = "view"
                    if item["mutability"] != mutability(want):
                        problems.append(f"{label}: {item['mutability']}, the contract says {mutability(want)}")
    for rel, contract in LIB_CALLS.items():
        declared = {f"{e['name']}({','.join(canonical(i) for i in e['inputs'])})" for e in abi(contract)
                    if e.get("type") == "function" and mutability(e) != "view"}
        # The whole file, not a line at a time: a call whose signature sits on the line after `call(pool,` counts too.
        source = read(rel)
        for m in re.finditer(r"""\bcall\(\s*\w+\s*,\s*["'`](\w+\([^"'`]*\))["'`]""", source):
            count += 1
            sig = m.group(1)
            if re.sub(r"\s+", "", sig) not in declared:
                n = source.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{n} call {sig}: {contract} declares no such function that sends")
    return problems, count


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


# Fields a struct gained at its end, which a copy that only reads it may leave off: a decoder of the
# fields before them reads an older deployment's struct and a newer one's alike. Rules'
# assetLeverageX100 came with v3 (the pools team, 21 October 2026), while the app, the gateway and the
# agents still read deployments before it. What the app SENDS is held below with no field left off.
APPENDED = {"RULES": ("assetLeverageX100",)}


def compare(label: str, solidity: list[tuple[str, str]], copy: list[tuple[str, str]],
            appended: tuple[str, ...] = ()) -> list[str]:
    """Names are compared only where the copy carries one: desk.py keeps types alone on purpose."""
    problems = []
    left_off = [name for _, name in solidity[len(copy):]]
    if len(solidity) != len(copy) and not (left_off and all(name in appended for name in left_off)):
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
        tail = APPENDED.get(name, ())
        problems += compare(f"app/lib/chain.js {name}", structs[arg], js_struct(js, name), tail)
        problems += compare(f"agents/desk.py {name}", structs[arg], py_struct(py, name), tail)
    # The rules the app SENDS (createPool), held to the contracts with no field left off: a copy short
    # of one encodes another function and the factory reverts (AUDIT В6-1, 21 Oct 2026). The app keeps
    # one for a factory before v3 and one for v3, which appends each asset's own cap. The one with the
    # contracts' number of fields is compared field for field; the two differ by that field alone; and
    # each is the one a createPool of the app sends.
    write = {name: js_struct(js, name) for name in ("RULES_WRITE", "RULES_WRITE_V3")}
    if write["RULES_WRITE_V3"] != write["RULES_WRITE"] + [("uint32[]", "assetLeverageX100")]:
        problems.append("app/lib/chain.js RULES_WRITE_V3 is not RULES_WRITE with uint32[] assetLeverageX100 appended")
    sent = [name for name, fields in write.items() if len(fields) == len(structs["rules"])]
    if not sent:
        problems.append(f"app/lib/chain.js: no RULES_WRITE copy has the contracts' {len(structs['rules'])} fields of Rules")
    for name in sent:
        problems += compare(f"app/lib/chain.js {name}", structs["rules"], write[name])
    for name in write:
        if f"function createPool(${{{name}}} rules," not in js:
            problems.append(f"app/lib/chain.js: no createPool sends {name}")
    # Every other copy of Rules, and the signatures built from one.
    found, copies = check_copies(structs["rules"])
    problems += found
    found, signatures = check_signatures(structs["rules"])
    problems += found
    # The simulated pool's and the paper exam's libraries in the app, against their contracts.
    found, library_items = check_libraries()
    problems += found
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
    # AccountOps is the library both link to: its errors come back from the account by
    # DELEGATECALL, as the account's own (HIP-3 moved the closing orders there, 21 October 2026).
    errors: set[str] = set()
    for contract in ("ChallengeAccount", "Pool", "AccountOps"):
        errors |= error_signatures(contract)
    carried = py_strings(py, "CONTRACT_ERRORS")
    for sig in sorted(errors - set(carried)):
        problems.append(f"agents/desk.py CONTRACT_ERRORS lacks {sig}, which a contract can answer")
    for sig in sorted(set(carried) - errors):
        problems.append(f"agents/desk.py CONTRACT_ERRORS carries {sig}, which none of them has")
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
    print(f"abi-check: clean ({fields} fields and {len(stages)} stages x 2 copies, {len(errors)} errors x 1, "
          f"{copies} copies of Rules, {signatures} signatures that carry it, {library_items} functions, events "
          "and calls of the app's simulated pool libraries).")
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
