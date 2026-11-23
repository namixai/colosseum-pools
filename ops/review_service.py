"""The review of every payout still waiting, on a timer, and an alarm for each one a person has to act on.

    spike/.venv/bin/python -m ops.review_service --deployment sim1 [--prices journal.json] [--out ops/state/reviews]

One pass reads the chain once (`ops/payout_review.py`, `read_chain`), reviews each payout still waiting and writes its
document, `<pool>-<id>.json` (schema payout-review/4), into --out: what `deploy_sim approve` reads before it approves
(each payout only on a fresh review that says approve, `payout_review.card_problem`), and what the operator's page
shows. A document is written whole or not at all (a file beside it, then renamed).

An alarm (the journal marked ALARM, the flag file, COLOSSEUM_ALERT_CMD when the host has a channel) for each payout
that is suspicious and that nothing on chain will settle by itself: a stop of reason 1 or 2 the checker sends itself
(action "stop") is no alarm; a payout held ("hold") waits for a person (a stop of reason 3, or its ceiling, whose date
the alarm says). A payout the pool will pay by itself without anyone's approval (a pool without approvals, a payout
approved already, or one whose approvals were not read) is an alarm at once with its deadline: the last moment a
person can still stop it for reason 3 (`stop_deadline`). Each is said again every 6 hours while it stays so, and once
more 24 and 6 hours before its deadline; one whose verdict, action or reasons change is said again at once; and an
alarm is remembered only once the channel took it, so one that did not go out is said at the next pass. A pass that
fails is an alarm too (once an hour), and so is a unit that failed (`--unit-failed`, its OnFailure=).

Every block of each stop's time is judged by the journals of prices (--prices, the keeper's; --prices2, the second
writer's from another machine), the venue's minute candles kept in --out (`ops/venue_candles.py`, fetched each pass),
and, when there is one, a node that answers for old blocks (--archive-rpc). The logs read are kept in --out/scan.json:
each pass reads only the new blocks. The end of each pass goes to --out/status.json, whose age the stops' loop
watches.

It sends nothing and holds no key. On the host: colosseum-payout-review@<name>.service run by its .timer every 10
minutes (ops/host). Testnet unless both COLOSSEUM_NETWORK=mainnet and --network mainnet name mainnet.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

from ops import deployments
from ops import keeper as core
from ops import payout_review as pr
from ops import sim_check as chk
from ops import sim_network as sn
from ops import stop_filler as sf
from spike.hlspike import common as c


def stop_deadline(r: dict) -> int | None:
    """The last moment a stop of `r`'s payout is still taken, by the pool's own rule (`sim_check.stop_due`): a pool
    with approvals stops until the payout is paid, which for one approved is from its due time; a pool without, until
    MARGIN_S before its due time. Approvals not read: the earlier of the two."""
    if r["approvals"] is True:
        return r["dueAt"] if r["approved"] else r["hardAt"]
    return r["dueAt"] - chk.MARGIN_S


# A payout held, or one that pays by itself unless a person stops it, is said again every REPEAT_S while it stays
# so, and once more as its deadline comes within each of BEFORE_S: a person who missed one alarm gets the next.
REPEAT_S = 6 * 3_600
BEFORE_S = (24 * 3_600, 6 * 3_600)
# A pass that fails is said at most this often (each run of the timer fails alike; the unit's OnFailure says it too).
FAILED_EVERY_S = 3_600
# Seconds a pass may read the chain before it saves what it read and ends (the unit's TimeoutStartSec is 540).
PASS_BUDGET_S = 420


def alarm_of(r: dict) -> dict | None:
    """The alarm a review calls for, or None: suspicious and not settled by a stop the checker sends itself."""
    if r["verdict"] != "suspicious" or r["action"] == "stop":
        return None
    by_itself = r["action"] != "hold" and (r["approvals"] is not True or r["approved"])
    pays = r["dueAt"] if by_itself else r["hardAt"]
    return {"pool": r["pool"], "id": r["id"], "trader": r["trader"], "action": r["action"], "kinds": r["kinds"],
            "reasons": r["reasons"], "deadline": stop_deadline(r), "paysAt": pays,
            "do": ("it pays by itself: a person stops it for reason 3 (the draft is in the review) before the deadline"
                   if by_itself else
                   f"held: it is not approved, and it goes out by itself at its ceiling ({utc(pays)}) unless a person "
                   "stops it for reason 3 (the draft is in the review) before then; do not approve it")}


def utc(t: int | None) -> str:
    return "unknown" if t is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


def due_to_say(prev, mark: list, now: int, deadline: int | None) -> bool:
    """Whether an alarm said before (`prev`: {"mark", "at", "before"}) is to be said again now: its verdict, action or
    reasons changed, REPEAT_S passed since it was last taken by the channel, or its deadline came within one of BEFORE_S
    it was not said in yet."""
    if not isinstance(prev, dict) or prev.get("mark") != mark:
        return True
    if now - int(prev.get("at", 0)) >= REPEAT_S:
        return True
    return deadline is not None and any(deadline - now <= b and b not in prev.get("before", []) for b in BEFORE_S)


def write(out: pathlib.Path, doc: dict) -> pathlib.Path:
    path = out / f"{doc['pool']}-{doc['id']}.json"
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def one_pass(record: dict, out: pathlib.Path, prices, said: dict, alarm, **read) -> list[dict]:
    """Reads the chain once (`read` goes to `payout_review.read_chain`: the kept logs, the candles, the archive node, the
    time budget), writes a document for each payout still waiting, raises the alarms; returns the reviews. An alarm is
    remembered in `said` only once the channel took it (`alarm(..., wait=True)` not False): one that did not go out is
    said again at the next pass. The pass's end is written to status.json beside the documents, for the stops' loop's
    watch of the review's age (`stop_filler.StopLoop`)."""
    chain = pr.read_chain(record, prices, **read)
    out.mkdir(parents=True, exist_ok=True)
    reviews = [pr.review(chain, x["pool"], x["id"]) for x in chain["payouts"] if x["state"] == "Due"]
    now = int(chain["now"])
    for r in reviews:
        write(out, pr.card(r, chain))
        a = alarm_of(r)
        key = f"{r['pool']}-{r['id']}"
        if a is None:
            said[key] = None
            continue
        mark = [a["action"], a["kinds"], a["reasons"]]
        if due_to_say(said.get(key), mark, now, a["deadline"]):
            took = alarm("payout_held" if a["action"] == "hold" else "payout_needs_a_stop", wait=True, **a)
            if took is not False:
                said[key] = {"mark": mark, "at": now, "before": [b for b in BEFORE_S
                                                                 if a["deadline"] is not None and a["deadline"] - now <= b]}
            else:
                core.log("review_alarm_not_taken", payout=key, then="said again at the next pass")
    write_status(out, {"at": int(time.time()), "chainNow": now, "block": chain["latest"], "due": len(reviews),
                       "held": sum(r["action"] == "hold" for r in reviews)})
    core.log("review_done", payouts=len(reviews), block=chain["latest"],
             suspicious=sum(r["verdict"] == "suspicious" for r in reviews))
    return reviews


def write_status(out: pathlib.Path, status: dict) -> None:
    tmp = out / "status.json.part"
    tmp.write_text(json.dumps(status) + "\n")
    os.replace(tmp, out / "status.json")


def read_prices(path: pathlib.Path, expect: dict | None = None, second: pathlib.Path | None = None,
                lo_t: int | None = None, hi_t: int | None = None):
    """The stops' loop's journal and the second writer's (`payout_review.read_journals`: their day files of the stops'
    time, each of this deployment), or None where there is none to read: then no stop is checked in every block, and
    each payout with a stop is held (`payout_review.stop_doubt`), never passed as clean."""
    try:
        got = pr.read_journals([path] + ([second] if second else []), expect, lo_t, hi_t)
        if got is None:
            raise FileNotFoundError(path.name)
        return got
    except OSError as exc:
        core.log("prices_unread", path=str(path), error=type(exc).__name__, then="every payout with a stop is held")
        return None


def second_stale(path: pathlib.Path | None, now: float, said: dict, alarm, limit: int = 1_800) -> None:
    """The second writer's journal not written to for `limit` seconds: an alarm, every REPEAT_S while it lasts. Its
    blocks are then the keeper's journal's alone, and a restart of the keeper holds payouts again."""
    if path is None:
        return
    files = sorted(path.parent.glob(f"{path.stem}-????-??-??{path.suffix}"))
    newest = max((f.stat().st_mtime for f in files), default=None)
    if newest is not None and now - newest < limit:
        said.pop("_second", None)
        return
    prev = said.get("_second")
    if prev is not None and now - prev < REPEAT_S:
        return
    took = alarm("second_journal_stale", wait=True, journal=path.name, newest=utc(int(newest)) if newest else "none",
                 why="the second writer's journal of prices is not coming: a restart of the keeper now holds the "
                     "payouts whose stops stood through it")
    if took is not False:
        said["_second"] = now


def confirmed(args, label: str) -> None:
    """On mainnet, the deployment's label in --confirm-mainnet even though nothing is sent: what this decides (approve
    refuses without its document) is on that network's money, and a unit copied from testnet must not start there
    without someone writing the label into its instance's file."""
    if c.NETWORK.name == "mainnet" and getattr(args, "confirm_mainnet", None) != label:
        raise SystemExit(f"refusing to run on mainnet: --confirm-mainnet {label} is needed, the deployment's label")


def unit_failed(name: str, flag: pathlib.Path | None, stamp: pathlib.Path) -> int:
    """What a unit's OnFailure= runs (colosseum-unit-failed@.service): an alarm that the unit `name` failed (a pass that
    raised, ran past its time or was killed for its memory), at most once in FAILED_EVERY_S a unit."""
    try:
        if time.time() - stamp.stat().st_mtime < FAILED_EVERY_S:
            return 0
    except OSError:
        pass
    took = sf.Alarm(flag)("unit_failed", wait=True, unit=name,
                          why="a host service failed: for the payout review, no document is fresh until it runs again, "
                              "and approve refuses every payout")
    if took is not False:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployment")
    ap.add_argument("--out", default=str(pr.REVIEWS_DIR), help="where each payout's document is written")
    ap.add_argument("--prices", help="the stops' loop's journal of prices (stop_filler.PriceJournal, its name: the day "
                    "files beside it are read): every block of each stop's time is judged by it")
    ap.add_argument("--prices2", default=os.environ.get("COLOSSEUM_PRICES2", ""),
                    help="the second writer's journal (ops.stop_filler alone, on another machine and node), brought "
                         "here by its sync: a block is held where either journal has it, and the two must agree")
    ap.add_argument("--candle-bps", type=int, default=None,
                    help=f"how far the venue's candle may stand from a stop before it contradicts the journal (default "
                         f"{pr.CANDLE_BPS} on mainnet, {TESTNET_CANDLE_BPS} on testnet; COLOSSEUM_CANDLE_BPS)")
    ap.add_argument("--no-candles", action="store_true", help="fetch no candles (then every payout with a stop is held)")
    ap.add_argument("--archive-rpc", default=os.environ.get("COLOSSEUM_ARCHIVE_RPC_URL", ""),
                    help="an optional node that answers for old blocks: a third check of the journal; none, none")
    ap.add_argument("--said", help="the file that remembers the alarms raised (default <out>/said.json)")
    ap.add_argument("--flag", help="the flag file each alarm is added to")
    ap.add_argument("--unit-failed", metavar="UNIT", help="say that UNIT failed (its OnFailure=) and end")
    sn.add_args(ap, service=True)
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)
    flag = pathlib.Path(args.flag) if args.flag else None
    if args.unit_failed:
        return unit_failed(args.unit_failed, flag, out / f"failed-{args.unit_failed}.at")
    if not args.deployment:
        ap.error("--deployment is required")
    with chk.node_errors():
        sn.confirm_service(args, args.deployment, c, sends=False)
    confirmed(args, args.deployment)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    said_path = pathlib.Path(args.said) if args.said else out / "said.json"
    try:
        said = json.loads(said_path.read_text())
    except (OSError, ValueError):
        said = {}
    alarm = sf.Alarm(flag)
    expect = {"chain": c.CHAIN_ID, "factory": record["SimPoolFactory"], "label": args.deployment}
    second = pathlib.Path(args.prices2) if args.prices2 else None
    prices = None
    if args.prices:
        prices = lambda lo, hi: read_prices(pathlib.Path(args.prices), expect, second, lo, hi)  # noqa: E731
    bps = args.candle_bps
    if bps is None:
        bps = int(os.environ.get("COLOSSEUM_CANDLE_BPS", "") or
                  (pr.CANDLE_BPS if c.NETWORK.name == "mainnet" else TESTNET_CANDLE_BPS))
    archive = pr.archive_oracle(args.archive_rpc) if args.archive_rpc else None
    try:
        candles = None if args.no_candles else fetch_candles(out)
        with chk.node_errors():
            one_pass(record, out, prices, said, alarm, scan=out / "scan.json", candles=candles, candle_bps=bps,
                     archive=archive, until=time.monotonic() + PASS_BUDGET_S)
        said.pop("_failed", None)
    except pr.Behind as exc:
        core.log("review_behind", why=str(exc), then="no document this pass; the next one goes on")
    except BaseException as exc:
        if time.time() - float(said.get("_failed", 0)) >= FAILED_EVERY_S:
            if alarm("review_failed", wait=True, error=chk.quiet(exc, 200),
                     why="the payout review's pass failed: no document is fresh, approve refuses every payout, and a "
                         "held payout is not said again until it works") is not False:
                said["_failed"] = time.time()
        save_said(said_path, said)
        raise
    second_stale(second, time.time(), said, alarm)
    save_said(said_path, said)
    return 0


TESTNET_CANDLE_BPS = 300  # testnet's perp trades thinly and away from the oracle: a wider screen there


def fetch_candles(out: pathlib.Path):
    """The venue's candles brought up to date in `out` (ops/venue_candles.py) and a reader of the days a pass needs; a
    fetch that fails leaves the files as they were (the minutes it lacks are said by the review)."""
    from ops import venue_candles as vc
    store = vc.Store(out, c.NETWORK.name)
    try:
        vc.update(store, vc.platform(), int(time.time()))
    except Exception as exc:  # the info API down: what is on disk is read, and the minutes missing are said
        core.log("candles_unread", error=type(exc).__name__, then="the minutes not on disk are said by the review")
    return lambda lo, hi: store.load(lo - 86_400, hi)


def save_said(path: pathlib.Path, said: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(said) + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
