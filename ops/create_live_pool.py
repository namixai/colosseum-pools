"""Creates the live-run pool on an existing deployment, and checks it afterwards.

    spike/.venv/bin/python ops/create_live_pool.py --deployment demo2            # checks only
    spike/.venv/bin/python ops/create_live_pool.py --deployment demo2 --send     # creates

Four steps in one place, because doing them by hand in four commands is how a pool ends up
half-made: the capital comes back from a finished stand, the pool is created, its spot is funded,
and its HyperCore account is prepared. Nothing is sent without `--send`, and every precondition is
read from the CHAIN first -- a deployment record says what was deployed, not what is true now.

The terms are arguments with the live run's defaults, so a different decision does not need a code
change. The factory refuses what it must (`PoolFactory._checkRules`, `_checkTerms`) and this script
refuses earlier and more loudly: a revert at step two leaves a withdrawal already done.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from hlspike import common as c  # noqa: E402

RULES = "(uint16,uint16,uint32,uint32[])"
TERMS = "(uint64,uint64,uint16,uint32,uint16,uint16,uint64)"
# `Pool.capitalNeeded`: both capitals in spot units, plus the fee for the challenge account.
SPOT_PER_PERP, NEW_ACCOUNT_FEE = 100, 10**8
POOL_CREATED = "PoolCreated(address,address)"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--deployment", required=True, help="label of a record in deployments/")
    p.add_argument("--send", action="store_true", help="actually create it")
    p.add_argument("--from-pool", default="0x237afA2D58B1612e19D47152FfB2E771c05Fe96D",
                   help="an Idle pool of ours whose spot comes back first; empty to skip")
    p.add_argument("--price", type=float, default=7.0, help="challenge price, USDC")
    p.add_argument("--capital", type=float, default=70.0, help="challenge capital, USDC")
    p.add_argument("--funded-capital", type=float, default=700.0, help="funded capital, USDC")
    p.add_argument("--target-bps", type=int, default=1000, help="profit target over capital, bps")
    p.add_argument("--days", type=int, default=90, help="how long a challenge may run")
    p.add_argument("--daily-loss-bps", type=int, default=300)
    p.add_argument("--drawdown-bps", type=int, default=600)
    p.add_argument("--leverage-x100", type=int, default=500)
    p.add_argument("--assets", default="3,4,0", help="perp indices: BTC 3, ETH 4, SOL 0")
    p.add_argument("--share-challenge-bps", type=int, default=0,
                   help="trader's share of challenge profit; 0 because the challenge is the audition")
    p.add_argument("--share-funded-bps", type=int, default=8000)
    return p


def preconditions(*, assets: list[int], listed: dict[int, bool], spot: int, need: int,
                  deployer: str, source: dict | None) -> list[str]:
    """Why this pool must not be created, from facts already read off the chain.

    Apart from the reads on purpose, so each refusal can be tested. A refusal that exists only
    inside a chain-reading `main` is one nobody has ever seen work -- and the cost of the first
    one failing here is a withdrawal already done and a pool not created.
    """
    out = []
    # `_checkRules` rejects an empty list and duplicates too. Leaving those to the factory means
    # finding out at step two, with the withdrawal already done.
    if not assets:
        out.append("список активов пуст")
    if len(assets) != len(set(assets)):
        out.append("в списке активов есть дубликаты")
    for a in assets:
        if not listed.get(a):
            out.append(f"актив {a} не в списке платформы этой фабрики")
    coming = 0
    if source is not None:
        coming = source["spot"]
        if source["stage"] != 0:
            out.append(f"{source['address']} не Idle (stage {source['stage']}) — withdrawOnCore откажет")
        if source["owner"].lower() != deployer.lower():
            out.append(f"{source['address']} принадлежит {source['owner']}, а не деплойщику")
    if spot + coming < need:
        out.append(f"не хватает {(need - spot - coming) / 1e8:.2f} USDC даже с возвратом")
    return out


def main() -> int:
    args = parser().parse_args()
    c.assert_testnet()
    rec = json.loads((ROOT / "deployments" / f"testnet-{args.deployment}.json").read_text())
    factory = rec["PoolFactory"]
    dep = c.account("deployer")
    assets = [int(a) for a in args.assets.split(",") if a.strip()]
    need = int((round(args.capital * 1e6) + round(args.funded_capital * 1e6)) * SPOT_PER_PERP) + NEW_ACCOUNT_FEE

    rules = (args.daily_loss_bps, args.drawdown_bps, args.leverage_x100, assets)
    terms = (round(args.price * 1e6), round(args.capital * 1e6), args.target_bps, args.days * 86400,
             args.share_challenge_bps, args.share_funded_bps, round(args.funded_capital * 1e6))

    print(f"развёртывание {args.deployment}, фабрика {factory}")
    print(f"правила: дневной {rules[0]} bps · просадка {rules[1]} bps · плечо {rules[2]/100}x · активы {assets}")
    print(f"условия: цена {args.price} · капитал {args.capital} · боевой {args.funded_capital} · "
          f"цель {args.target_bps} bps · срок {args.days} дней · доли {args.share_challenge_bps}/"
          f"{args.share_funded_bps} bps")
    print(f"пулу нужно на споте: {need/1e8:.2f} USDC\n")

    # ── preconditions, read from the chain ───────────────────────────────────────────────
    listed = {a: c.call_view(factory, "isPlatformAsset(uint32)", ["uint32"], [a], ["bool"])[0]
              for a in assets}
    spot = int(c.core_spot_balance(dep.address, 0)["total"])
    source = None
    if args.from_pool:
        source = {"address": args.from_pool,
                  "stage": c.call_view(args.from_pool, "stage()", [], [], ["uint8"])[0],
                  "owner": c.call_view(args.from_pool, "owner()", [], [], ["address"])[0],
                  "spot": int(c.core_spot_balance(args.from_pool, 0)["total"])}
        print(f"возврат из {source['address']}: {source['spot']/1e8:.4f} USDC · "
              f"stage {source['stage']} · владелец {source['owner']}")
    coming = source["spot"] if source else 0
    print(f"у деплойщика на споте {spot/1e8:.4f}, после возврата {(spot + coming)/1e8:.4f}")
    problems = preconditions(assets=assets, listed=listed, spot=spot, need=need,
                             deployer=dep.address, source=source)
    if problems:
        print("\n🔴 НЕ СОЗДАЮ:")
        for p in problems:
            print("   -", p)
        return 1
    print("\nпредусловия в порядке.")
    if not args.send:
        print("без --send ничего не отправлено.")
        return 0

    # ── 1. the capital comes back ────────────────────────────────────────────────────────
    if args.from_pool and coming:
        print(f"\n1. withdrawOnCore({coming}) на {args.from_pool}…")
        r = c.transact(dep, args.from_pool, "withdrawOnCore(uint64)", ["uint64"], [coming])
        print("   tx", r["transactionHash"])
        for _ in range(40):
            time.sleep(3)
            if int(c.core_spot_balance(dep.address, 0)["total"]) >= spot + coming:
                break
        # The loop gives up after two minutes either way, so the balance decides, not the loop.
        # What matters is whether `need` is covered -- not whether the exact expected total
        # arrived, which a fee or a rounding on the way could leave a unit short. Creating a pool
        # the next step cannot fund leaves a pool with no capital, which is worse than no pool.
        now = int(c.core_spot_balance(dep.address, 0)["total"])
        print(f"   спот деплойщика: {now/1e8:.4f}")
        if now < need:
            print(f"   🔴 вывод не дошёл: {now/1e8:.4f} против нужных {need/1e8:.2f}. "
                  f"Пул НЕ создан, деньги у деплойщика — можно запустить снова.")
            return 1

    # ── 2. the pool ──────────────────────────────────────────────────────────────────────
    print("\n2. createPool…")
    r = c.transact(dep, factory, f"createPool({RULES},{TERMS})", [RULES, TERMS], [rules, terms])
    topic = "0x" + c.keccak(text=POOL_CREATED).hex()
    pool = next(("0x" + lg["topics"][1][-40:] for lg in r["logs"]
                 if lg["topics"] and lg["topics"][0].lower() == topic), None)
    if pool is None:
        print("   🔴 в квитанции нет PoolCreated — адрес пула неизвестен, дальше руками")
        print("   tx", r["transactionHash"])
        return 1
    from eth_utils import to_checksum_address
    pool = to_checksum_address(pool)
    print(f"   пул {pool} · tx {r['transactionHash']}")

    # ── 3. spot, which also creates its HyperCore account ────────────────────────────────
    print(f"\n3. {need/1e8:.2f} USDC на спот пула…")
    print("  ", c.exchange(dep).spot_transfer(need / 1e8, pool, c.spot_token_wire("USDC")))
    for _ in range(40):
        time.sleep(3)
        if int(c.core_spot_balance(pool, 0)["total"]) >= need:
            break

    # ── 4. prepareAccount ────────────────────────────────────────────────────────────────
    print("\n4. prepareAccount…")
    r = c.transact(dep, pool, "prepareAccount()", [], [])
    print("   tx", r["transactionHash"])

    # ── what the chain says now, not what we asked for ───────────────────────────────────
    print("\nПРОВЕРКА ПО ЦЕПИ:")
    back_terms = c.call_view(pool, "terms()", [], [], [TERMS])[0]
    back_rules = c.call_view(pool, "rules()", [], [], [RULES])[0]
    ok = {
        "stage Idle": c.call_view(pool, "stage()", [], [], ["uint8"])[0] == 0,
        "accountReady": c.call_view(pool, "accountReady()", [], [], ["bool"])[0],
        "spot >= capitalNeeded": int(c.core_spot_balance(pool, 0)["total"]) >= int(
            c.call_view(pool, "capitalNeeded()", [], [], ["uint64"])[0]),
        "terms как просили": tuple(back_terms) == terms,
        "rules как просили": (back_rules[0], back_rules[1], back_rules[2], list(back_rules[3])) == (
            rules[0], rules[1], rules[2], assets),
    }
    for k, v in ok.items():
        print(f"  {'✓' if v else '🔴'} {k}")
    print(f"\nПУЛ: {pool}")
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
