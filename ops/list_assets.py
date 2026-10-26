"""List or delist markets on a v3 factory, every check made before anything is sent (the CTO's 1/7/9,
21 October 2026). Without --send it is a dry run: it reads the markets, checks them, asks the factory
and sends nothing.

    spike/.venv/bin/python ops/list_assets.py --deployment v3-demo3 --list platform --assets BTC,ETH,xyz:XYZ100
    spike/.venv/bin/python ops/list_assets.py --deployment v3-demo3 --list funded --assets xyz:XYZ100 --send
    spike/.venv/bin/python ops/list_assets.py --deployment v3-demo3 --list platform --assets SOL --delist --send

A market is named as Hyperliquid's meta names it: "BTC" on the main dex, "xyz:XYZ100" on a builder
dex. Its id is the order id the contracts hold: the index on the main dex, 100000 + dex * 10000 +
index on a builder dex. Refused before anything is sent:
- a market the dex's meta does not have, or has delisted (when listing);
- a builder dex the run does not trust (H-3). Its owner sets the oracle, may halt trading and
  settle the market, and a halt takes the exchange's stops away. xyz and mkts are trusted unless
  --trust names others;
- a builder dex whose collateral is not USDC (H-2). An account's capital goes there as USDC, and no
  precompile shows a dex's collateral, so the contracts cannot check it themselves;
- what the factory would refuse (NotThatMarket, NotAnOrderId, not its operator). It is asked first
  by eth_call from the signer.
A market already in the list (or not in it, when delisting) is left as it is.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from eth_utils import keccak  # noqa: E402

from hlspike import common as c  # noqa: E402

TRUSTED_DEXES = ("xyz", "mkts")
USDC_TOKEN = 0
HIP3_BASE, HIP3_DEX = 100_000, 10_000
LISTS = {"platform": ("setPlatformAssets", "isPlatformAsset"), "funded": ("setFundedAssets", "isFundedAsset")}
SET_TYPES = ["uint32[]", "string[]", "bool"]
# The factory's refusals a listing can meet, by selector, so a refusal reads as its name.
FACTORY_ERRORS = ("NotThatMarket(uint32,string)", "NotAnOrderId(uint32)", "NotOperator()")
BY_SELECTOR = {"0x" + keccak(text=e)[:4].hex(): e.split("(")[0] for e in FACTORY_ERRORS}


def dex_of(name: str) -> str:
    """"xyz" for "xyz:XYZ100", "" (the main dex) for "BTC"."""
    return name.split(":")[0] if ":" in name else ""


def resolve(names: list[str], trusted: tuple[str, ...], listing: bool) -> tuple[list[dict], list[str]]:
    """Each market's id and its name as the meta has it, and every reason to refuse the run."""
    problems: list[str] = []
    dexes = [d.get("name") if d else "" for d in c.info_post({"type": "perpDexs"})]
    metas: dict[str, dict] = {}
    out = []
    for name in names:
        dex = dex_of(name)
        if dex and dex not in trusted:
            problems.append(f"{name}: dex {dex} is not one this run trusts ({', '.join(trusted)}); --trust names others")
            continue
        if dex not in dexes:
            problems.append(f"{name}: Hyperliquid lists no perp dex {dex}")
            continue
        if dex not in metas:
            metas[dex] = c.info_post({"type": "meta", "dex": dex} if dex else {"type": "meta"})
        meta = metas[dex]
        if dex and meta.get("collateralToken") != USDC_TOKEN:
            problems.append(f"{name}: dex {dex}'s collateral is token {meta.get('collateralToken')}, not USDC")
            continue
        found = [(i, a) for i, a in enumerate(meta["universe"]) if a["name"] == name]
        if not found:
            problems.append(f"{name}: not in dex {dex or 'main'}'s meta")
            continue
        i, a = found[0]
        if listing and a.get("isDelisted"):
            problems.append(f"{name}: delisted on Hyperliquid")
            continue
        d = dexes.index(dex)
        out.append({"name": a["name"], "id": HIP3_BASE + d * HIP3_DEX + i if dex else i})
    return out, problems


def refusal(exc: Exception) -> str:
    text = str(exc)
    for sel, name in BY_SELECTOR.items():
        if sel[2:] in text.lower():
            return name
    return " ".join(text.split())[:200]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="the label of a record in deployments/ (testnet-<label>.json)")
    p.add_argument("--list", required=True, choices=sorted(LISTS), help="the exams' and pools' list, or the funded accounts'")
    p.add_argument("--assets", required=True, help="markets as the meta names them: BTC,ETH,xyz:XYZ100")
    p.add_argument("--delist", action="store_true", help="take them off the list instead")
    p.add_argument("--trust", default=",".join(TRUSTED_DEXES), help="the builder dexes this run may list from")
    p.add_argument("--signer", default="deployer", help="the factory's operator, by its key's name")
    p.add_argument("--send", action="store_true", help="send it; without this nothing is sent")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    c.assert_testnet()
    record = json.loads((ROOT / "deployments" / f"testnet-{args.deployment}.json").read_text())
    factory = record["PoolFactory"]
    setter, reader = LISTS[args.list]
    listing = not args.delist
    names = [n.strip() for n in args.assets.split(",") if n.strip()]
    trusted = tuple(t.strip() for t in args.trust.split(",") if t.strip())
    markets, problems = resolve(names, trusted, listing)
    for problem in problems:
        print(f"🔴 {problem}")
    if problems:
        print("refused: nothing was sent")
        return 1

    todo = []
    for m in markets:
        if c.call_view(factory, f"{reader}(uint32)", ["uint32"], [m["id"]], ["bool"])[0] == listing:
            print(f"  {m['name']} ({m['id']}): already {'listed' if listing else 'off the list'}")
        else:
            todo.append(m)
    if not todo:
        print("nothing to do: nothing was sent")
        return 0
    ids, labels = [m["id"] for m in todo], [m["name"] for m in todo]
    sig = f"{setter}({','.join(SET_TYPES)})"
    signer = c.address_of(args.signer)
    data = c.encode_call(sig, SET_TYPES, [ids, labels, listing])
    try:
        c.rpc("eth_call", [{"from": signer, "to": factory, "data": "0x" + data.hex()}, "latest"])
    except Exception as exc:
        print(f"🔴 the factory refuses: {refusal(exc)}")
        print("refused: nothing was sent")
        return 1
    print(f"{'would send' if not args.send else 'sending'}  {setter}({ids}, {labels}, {listing}) "
          f"to {factory} from {signer}")
    if not args.send:
        print("dry run: nothing was sent")
        return 0
    rcpt = c.transact(c.account(args.signer), factory, sig, SET_TYPES, [ids, labels, listing])
    print("   tx", rcpt["transactionHash"])
    ok = True
    for m in todo:
        now = c.call_view(factory, f"{reader}(uint32)", ["uint32"], [m["id"]], ["bool"])[0]
        ok &= now == listing
        print(f"  {'✓' if now == listing else '🔴'} {m['name']} ({m['id']}): {reader} = {now}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
