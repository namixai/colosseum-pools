"""Deploy a pool of many traders on testnet (chain 998), with everything it stands on: a key
registry, a factory on this commit's Pool, the paper exam's implementation, and one MultiPool on
a plan read from a file.

    spike/.venv/bin/python ops/deploy_multi.py --label multi-1 --plan ops/plans/multi-testnet-run.json \\
        --keys-file <addresses.txt from ops/make_demo_keys.py> [--owner 0x...] [--dry-run]
    spike/.venv/bin/python ops/deploy_multi.py --big-blocks-off [--signer multi-operator]

The second form does one thing: it switches the signer back to small blocks. A run that died
between switching big blocks on and off leaves the wallet there, and every transaction of it
then waits for a big block, about one a minute.

Nothing of an existing deployment is used or changed. The seats of a MultiPool are pools its
owner runs (`Pool.runByOwner`), which the pools deployed before 13 October 2026 cannot do, so
this needs a factory of its own on a Pool built from this commit, and a registry of its own for
the keys its funded accounts will take.

--dry-run makes every check a real run makes -- the label is free, the contracts build, the plan
is one the contracts will take, the keys are new to every registry on record, the signer is on
HyperCore and holds gas -- and sends nothing.

The plan file is read and checked here against the same bounds the contracts hold, so a mistake
costs a message and not a deployment. Amounts are in USDC, shares in bps, times in the unit their
key names.

The shortest and the longest term, and the shortest last stretch in which nothing is sold, are
not the plan's: whoever deploys sets them, and the pool holds its plan and every extension to
them. Left alone they are the product's, 180 days, 730 days and 14 days. --min-term-days,
--max-term-days and --notice-days make them shorter for a pool whose whole term has to be
watched in an afternoon; a run with any of them says so in its first lines and in its record.

Signs with the key named by --signer, `multi-operator` unless told otherwise: a wallet of its own,
and not the one that runs the shared pool people can see, which big blocks would slow to a
transaction a minute. The pool's owner is --owner, in checksummed form, or the signer; it can
never be changed, and it is the one address that takes money out. The owner puts capital in afterwards, by sending USDC to the
pool's address on HyperCore; this script moves no USDC.

Six contracts go through big blocks: the MultiPool alone is close to six million gas, twice what
a small block holds. The switch is a HyperCore action of the signer's, turned off again whatever
happens. Writes deployments/testnet-<label>.json after every transaction, status field included,
the way ops/deploy_testnet.py does.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
sys.path.insert(0, str(ROOT))

from eth_utils import to_checksum_address  # noqa: E402

from hlspike import common as c  # noqa: E402
from ops.deploy_testnet import (  # noqa: E402
    CHALLENGE_FEE,
    PLATFORM_ASSETS,
    big_blocks,
    check_assets,
    check_keys,
    deploy_contract,
    git_head,
    mark_incomplete,
    save,
)

# The Plan struct of src/multi/MultiPool.sol as the call encoder takes it. A test holds this
# against the Solidity source, field by field.
RULES_T = "(uint16,uint16,uint32,uint32[])"
EXAM_T = "(uint64,uint16[],uint32,uint32,uint16,uint32,uint16,uint16)"
PLAN_T = (f"((uint64,uint64)[],{RULES_T},{EXAM_T},{RULES_T},"
          "uint16,uint16,uint8,uint16,uint16,uint32,uint32,uint16,uint32,uint16,address,address)")
AFTER_TARGET = {"trader_stops": 0, "closes": 1, "stays": 2}
DEFAULT_SIGNER = "multi-operator"
# MultiPool's constructor: owner, factory, exam, plan, and the deployment's three bounds.
CTOR_T = ["address", "address", "address", PLAN_T, "uint32", "uint32", "uint32"]

USDC_1E6 = 1_000_000
ZERO_ADDRESS = "0x" + "00" * 20
DAY = 86_400
BPS = 10_000
# The contracts' own bounds, repeated so that a plan is refused here before any gas is spent.
MAX_MENU, MAX_SEATS, MAX_STEPS = 8, 16, 4
MAX_COST_PPM, MAX_FILL_BLOCKS, MAX_LEVERAGE_X100 = 100_000, 600, 1000
# The product's bounds on a term and on its last stretch, in days; a deployment may name others.
MIN_TERM_DAYS, MAX_TERM_DAYS, NOTICE_DAYS = 180, 730, 14
# Whatever a deployment names, the contract holds the notice to fourteen days and to a twelfth of
# the shortest term, and a funded account's idle limit to one day to ninety.
MAX_NOTICE_DAYS, NOTICE_SHARE = 14, 12
MIN_FUNDED_IDLE_DAYS, MAX_FUNDED_IDLE_DAYS = 1, 90
MIN_CLAIM_S, MAX_CLAIM_S = 3_600, 30 * DAY
# Six contracts through big blocks at 0.1 gwei come to a few thousandths of a HYPE; this is a
# refusal to start on an empty wallet, not an estimate.
GAS_FLOOR_WEI = 3 * 10**16


class BadPlan(SystemExit):
    """The plan file asks for something the contracts would refuse."""


def _need(ok: bool, message: str) -> None:
    if not ok:
        raise BadPlan(f"plan: {message}")


def _rules(raw: dict, what: str) -> tuple:
    for name in raw["assets"]:
        _need(name in PLATFORM_ASSETS, f"{what}: {name} is not an asset this deployment lists")
    assets = [PLATFORM_ASSETS[name] for name in raw["assets"]]
    _need(0 < len(assets) <= 16 and len(set(assets)) == len(assets), f"{what}: 1 to 16 assets, none twice")
    _need(0 < raw["daily_loss_bps"] < BPS, f"{what}: daily loss is 1 to 9999 bps")
    _need(0 < raw["max_drawdown_bps"] < BPS, f"{what}: drawdown is 1 to 9999 bps")
    _need(100 <= raw["max_leverage_x100"] <= MAX_LEVERAGE_X100, f"{what}: leverage is 100 to 1000 (1x to 10x)")
    return (raw["daily_loss_bps"], raw["max_drawdown_bps"], raw["max_leverage_x100"], assets)


def bounds_of(min_term_days: float, max_term_days: float, notice_days: float) -> tuple[int, int, int]:
    """The deployment's bounds in seconds, as MultiPool's constructor takes them."""
    lo, hi, notice = round(min_term_days * DAY), round(max_term_days * DAY), round(notice_days * DAY)
    _need(0 < notice <= MAX_NOTICE_DAYS * DAY, "the notice is above zero and fourteen days at most")
    # This keeps the shortest term above zero too.
    _need(notice * NOTICE_SHARE <= lo, "the notice is a twelfth of the shortest term at most")
    _need(lo <= hi, "the shortest term is not above the longest")
    return lo, hi, notice


def plan_of(raw: dict, bounds: tuple[int, int, int] | None = None) -> tuple:
    """The plan file as the tuple MultiPool's constructor takes, or BadPlan saying what is wrong.
    `bounds` are the deployment's; the product's if not given."""
    min_term_s, max_term_s, notice_s = bounds or bounds_of(MIN_TERM_DAYS, MAX_TERM_DAYS, NOTICE_DAYS)
    menu = [(round(line["size_usdc"] * USDC_1E6), round(line["price_usdc"] * USDC_1E6)) for line in raw["menu"]]
    _need(0 < len(menu) <= MAX_MENU, f"the menu has 1 to {MAX_MENU} lines")
    _need(all(size > 0 and price > 0 for size, price in menu), "every size and every price is above zero")
    _need(all(a[0] < b[0] for a, b in zip(menu, menu[1:])), "each size is larger than the one before")

    e = raw["exam"]
    targets = list(e["targets_bps"])
    _need(0 < len(targets) <= MAX_STEPS and all(0 < t <= BPS for t in targets),
          f"an exam has 1 to {MAX_STEPS} steps, each of 1 to 10000 bps")
    step_s = round(e["step_days"] * DAY)
    _need(step_s > 0, "a step needs a deadline: an exam with none could be cut short by the pool's end")
    idle_s = round(e["max_idle_days"] * DAY)
    _need(idle_s >= 0, "the idle limit is zero (off) or a time")
    _need(0 <= e["min_trading_days"] <= step_s // DAY + 1, "the trading days a step needs have to fit the step")
    _need(0 <= e["cost_ppm"] <= MAX_COST_PPM, "a fill costs 0 to 100000 millionths")
    _need(1 <= e["fill_delay_blocks"] <= MAX_FILL_BLOCKS, "the fill delay is 1 to 600 blocks")
    _need(1 <= e["fill_window_blocks"] <= MAX_FILL_BLOCKS, "the fill window is 1 to 600 blocks")
    exam = (0, targets, step_s, idle_s, e["min_trading_days"], e["cost_ppm"],
            e["fill_delay_blocks"], e["fill_window_blocks"])

    f = raw["funded"]
    _need(f["after_target"] in AFTER_TARGET, f"after_target is one of {sorted(AFTER_TARGET)}")
    mode = AFTER_TARGET[f["after_target"]]
    if mode == 0:
        _need(f["target_bps"] == 0, "where the trader stops the account themselves there is no target: 0")
    else:
        _need(0 < f["target_bps"] <= BPS, "the funded target is 1 to 10000 bps")
    _need(0 <= f["trader_share_bps"] <= BPS, "the trader's share is 0 to 10000 bps")
    _need(f["max_retakes"] == 0 or mode == 2, "retakes belong to after_target 'stays' only")
    idle_s = round(f["idle_days"] * DAY)
    _need(MIN_FUNDED_IDLE_DAYS * DAY <= idle_s <= MAX_FUNDED_IDLE_DAYS * DAY,
          "the funded account's idle limit is one day to ninety; it cannot be turned off")

    p = raw["pool"]
    _need(0 < p["in_work_bps"] <= BPS, "the share in work is 1 to 10000 bps")
    _need(0 < p["max_seats"] <= MAX_SEATS, f"1 to {MAX_SEATS} seats")
    claim_s = round(p["claim_window_days"] * DAY)
    _need(MIN_CLAIM_S <= claim_s <= MAX_CLAIM_S, "the claim window is 1 hour to 30 days")
    term_s = round(p["term_days"] * DAY)
    _need(min_term_s <= term_s <= max_term_s,
          f"the term is {min_term_s / DAY:g} to {max_term_s / DAY:g} days in this deployment")
    # The pool's sale lead is the longer of this and the notice; the notice is a twelfth of the
    # term at most, so whether the plan fits rests on the exam alone.
    lead_s = step_s * len(targets) + claim_s
    _need(lead_s < term_s, "an exam and the time to take its account have to fit the term with time to spare")

    # Free exams: absent, or a number a month and who signs the invitations. How many is the
    # creator's; what they cost is worked out elsewhere, not here.
    free = raw.get("free", {"per_month": 0, "inviter": None})
    per_month, inviter = free["per_month"], free.get("inviter")
    _need(isinstance(per_month, int) and not isinstance(per_month, bool) and 0 <= per_month < 2**16,
          "free exams a month are a whole number, 0 for none")
    _need(inviter is not None or not per_month, "free exams need the inviter's address")
    _need(inviter is None or (isinstance(inviter, str) and _checksummed(inviter)),
          "the inviter's address is given in checksummed form, so that a typo cannot pass")
    inviter = inviter or ZERO_ADDRESS
    # Who may record a trade the chain cannot see: the keeper that reads the funded accounts'
    # fills. Absent is nobody, and then a trader flat at every look is closed as idle.
    noter = f.get("noter")
    _need(noter is None or (isinstance(noter, str) and _checksummed(noter)),
          "the noter's address is given in checksummed form, so that a typo cannot pass")
    noter = noter or ZERO_ADDRESS

    return (menu, _rules(raw["exam_rules"], "exam_rules"), exam, _rules(raw["funded_rules"], "funded_rules"),
            f["target_bps"], f["trader_share_bps"], mode, p["in_work_bps"], p["max_seats"], claim_s, term_s,
            f["max_retakes"], idle_s, per_month, inviter, noter)


def _checksummed(address: str) -> bool:
    try:
        return to_checksum_address(address) == address
    except ValueError:
        return False


def checked_owner(owner: str | None) -> str | None:
    """The owner as given, refused unless it is in checksummed form. An address typed in lower case
    with one wrong character is still an address, and the pool would belong to it for good."""
    if owner is None:
        return None
    try:
        good = to_checksum_address(owner)
    except ValueError as exc:
        raise SystemExit(f"--owner {owner} is not an address: {exc}")
    if owner != good:
        raise SystemExit(f"--owner has to be given in checksummed form, {good}, so that a typo cannot pass")
    return owner


def node_host() -> str:
    """Where signed transactions go: the host only, so that a token in the path is not printed."""
    return urlsplit(c.RPC_URL).netloc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label")
    p.add_argument("--plan", help="the plan file, JSON")
    p.add_argument("--keys-file", help="agent key addresses for this registry, one per line")
    p.add_argument("--signer", default=DEFAULT_SIGNER, help=f"the named key that deploys and operates ({DEFAULT_SIGNER})")
    p.add_argument("--big-blocks-off", action="store_true", help="only switch the signer back to small blocks")
    p.add_argument("--owner", help="the pool's owner; the signer if not given")
    p.add_argument("--min-term-days", type=float, default=MIN_TERM_DAYS, help="the shortest term (default 180)")
    p.add_argument("--max-term-days", type=float, default=MAX_TERM_DAYS, help="the longest term (default 730)")
    p.add_argument("--notice-days", type=float, default=NOTICE_DAYS,
                   help="the shortest last stretch with nothing sold (default 14)")
    p.add_argument("--dry-run", action="store_true",
                   help="run every check and send nothing; says what a real run would do")
    args = p.parse_args()

    c.assert_testnet()
    if args.big_blocks_off:
        big_blocks(c.account(args.signer), False)
        print(f"{args.signer} is on small blocks again")
        return 0
    if not (args.label and args.plan and args.keys_file):
        p.error("--label, --plan and --keys-file are needed to deploy")
    checked_owner(args.owner)
    out_path = ROOT / "deployments" / f"testnet-{args.label}.json"
    if out_path.exists():
        raise SystemExit(f"{out_path} exists; pick another label")
    commit = git_head()
    raw = json.loads(pathlib.Path(args.plan).read_text())
    bounds = bounds_of(args.min_term_days, args.max_term_days, args.notice_days)
    products = bounds == bounds_of(MIN_TERM_DAYS, MAX_TERM_DAYS, NOTICE_DAYS)
    plan = plan_of(raw, bounds)
    subprocess.run(["forge", "build"], cwd=ROOT, check=True, capture_output=True)
    assets = check_assets()
    keys = check_keys(pathlib.Path(args.keys_file).read_text().splitlines())
    if not keys:
        raise SystemExit("no agent keys: every funded account takes one")

    op = c.account(args.signer)
    owner = args.owner or op.address
    if not c.core_user_exists(op.address):
        raise SystemExit(f"{args.signer} has no HyperCore account yet; fund it first")
    gas_wei = int(c.rpc("eth_getBalance", [op.address, "latest"]), 16)
    if gas_wei < GAS_FLOOR_WEI:
        raise SystemExit(f"{args.signer} holds {gas_wei / 1e18:.4f} HYPE, which is below the "
                         f"{GAS_FLOOR_WEI / 1e18:.2f} this deployment needs; fund it first")

    if args.dry_run:
        print("deploy_multi --dry-run: nothing was sent.")
        print(f"  label            {args.label}  (deployments/testnet-{args.label}.json is free)")
        print(f"  commit           {commit}")
        print(f"  signer           {op.address}, {gas_wei / 1e18:.4f} HYPE, on HyperCore; it operates the registry and the factory")
        print(f"  owner            {owner}  -- for good: the one address that takes money out")
        print(f"  node             {node_host()}  -- where the signed transactions go")
        print(f"  bounds           term {bounds[0] / DAY:g} to {bounds[1] / DAY:g} days, notice {bounds[2] / DAY:g} days"
              + ("" if products else "  -- NOT the product's 180 / 730 / 14: a pool for watching a whole term"))
        print(f"  plan             {len(plan[0])} sizes, {len(plan[2][1])} exam step(s), "
              f"{plan[7] / 100:g}% in work, {plan[8]} seats, term {raw['pool']['term_days']} days, "
              f"after the target: {raw['funded']['after_target']}")
        print("  would deploy     KeyRegistry, Pool, ChallengeAccount, PoolFactory, PaperExam  (big blocks on, then off)")
        print(f"  would configure  setAccountSource, setPlatformAssets {assets}, setChallengeFee {CHALLENGE_FEE}, "
              f"publish {len(keys)} keys")
        print("  would deploy     MultiPool(owner, factory, exam, plan, bounds)  (big blocks on, then off)")
        print(f"  would write      deployments/testnet-{args.label}.json")
        print("  would NOT touch  any existing deployment record or contract, and moves no USDC")
        return 0

    record: dict = {"chain_id": c.CHAIN_ID, "label": args.label, "commit": commit, "status": "deploying",
                    "deployer": op.address, "operator": op.address, "owner": owner,
                    "platform_assets": PLATFORM_ASSETS, "challenge_fee": CHALLENGE_FEE, "plan": raw,
                    "bounds_s": {"min_term": bounds[0], "max_term": bounds[1], "notice": bounds[2]},
                    "bounds_are_the_products": products, "tx": {}}
    big_blocks(op, True)
    try:
        try:
            registry, _ = deploy_contract(op, out_path, record, "KeyRegistry", "KeyRegistry", ["address"], [op.address])
            pool_impl, _ = deploy_contract(op, out_path, record, "PoolImpl", "Pool", [], [])
            challenge_impl, _ = deploy_contract(op, out_path, record, "ChallengeAccountImpl", "ChallengeAccount", [], [])
            factory, _ = deploy_contract(op, out_path, record, "PoolFactory", "PoolFactory",
                                         ["address", "address", "address", "address"],
                                         [registry, pool_impl, challenge_impl, op.address])
            exam_impl, _ = deploy_contract(op, out_path, record, "PaperExamImpl", "PaperExam", [], [])
        finally:
            big_blocks(op, False)
    except BaseException as exc:  # a deployment or the switch back to small blocks failed
        mark_incomplete(out_path, record, exc)
        raise

    record["status"] = "configuring"
    save(out_path, record)
    # Before the pool, not after: its constructor asks the factory for a seat of every size, and
    # the factory refuses rules on assets it has not been told to list.
    steps = [
        ("setAccountSource", registry, "setAccountSource(address)", ["address"], [factory]),
        ("setPlatformAssets", factory, "setPlatformAssets(uint32[],bool)", ["uint32[]", "bool"], [assets, True]),
        ("setChallengeFee", factory, "setChallengeFee(uint256,address)", ["uint256", "address"],
         [CHALLENGE_FEE, op.address]),
        ("publish", registry, "publish(address[])", ["address[]"], [keys]),
    ]
    try:
        for name, to, sig, types, values in steps:
            record["tx"][name] = c.transact(op, to, sig, types, values)["transactionHash"]
            if name == "publish":
                record["published_keys"] = keys
            save(out_path, record)
        big_blocks(op, True)
        try:
            _, rcpt = deploy_contract(op, out_path, record, "MultiPool", "MultiPool",
                                      CTOR_T, [owner, factory, exam_impl, plan, *bounds])
        finally:
            big_blocks(op, False)
    except BaseException as exc:
        mark_incomplete(out_path, record, exc)
        raise

    record.update({"block": int(rcpt["blockNumber"], 16), "gas_used_MultiPool": int(rcpt["gasUsed"], 16),
                   "status": "complete"})
    save(out_path, record)
    c.record("deployed_multi", **{k: v for k, v in record.items() if k not in ("tx", "plan")})
    print(f"wrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
