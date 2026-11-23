"""The review of every payout still waiting, on a timer, and an alarm for each one a person has to act on.

    spike/.venv/bin/python -m ops.review_service --deployment sim1 [--prices journal.json] [--out ops/state/reviews]

One pass reads the chain once (`ops/payout_review.py`, `read_chain`), reviews each payout still waiting and writes its
document, `<pool>-<id>.json` (schema payout-review/3), into --out: what `deploy_sim approve` reads before it approves
(each payout only on a fresh review that says approve, `payout_review.card_problem`), and what the operator's page
shows. A document is written whole or not at all (a file beside it, then renamed).

An alarm (the journal marked ALARM, the flag file, COLOSSEUM_ALERT_CMD when the host has a channel) for each payout
that is suspicious and that nothing on chain will settle by itself: a stop of reason 1 or 2 the checker sends itself
(action "stop") is no alarm; a payout held ("hold") waits for a person (a stop of reason 3, or its ceiling). A payout
the pool will pay by itself without anyone's approval (a pool without approvals, a payout approved already, or one
whose approvals were not read) is an alarm at once with its deadline: the last moment a person can still stop it for
reason 3 (`payout_review.stop_deadline`). Each payout is said once while it stays as it is: a payout whose verdict,
action or reasons change is said again.

It sends nothing and holds no key. On the host: colosseum-payout-review@<name>.service run by its .timer every 10
minutes (ops/host). Testnet unless both COLOSSEUM_NETWORK=mainnet and --network mainnet name mainnet.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading

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


def alarm_of(r: dict) -> dict | None:
    """The alarm a review calls for, or None: suspicious and not settled by a stop the checker sends itself."""
    if r["verdict"] != "suspicious" or r["action"] == "stop":
        return None
    by_itself = r["action"] != "hold" and (r["approvals"] is not True or r["approved"])
    return {"pool": r["pool"], "id": r["id"], "trader": r["trader"], "action": r["action"], "kinds": r["kinds"],
            "reasons": r["reasons"], "deadline": stop_deadline(r),
            "do": ("it pays by itself: a person stops it for reason 3 (the draft is in the review) before the deadline"
                   if by_itself else "held: a person decides, a stop of reason 3 or its ceiling; do not approve it")}


def write(out: pathlib.Path, doc: dict) -> pathlib.Path:
    path = out / f"{doc['pool']}-{doc['id']}.json"
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def one_pass(record: dict, out: pathlib.Path, prices: dict | None, said: dict, alarm) -> list[dict]:
    """Reads the chain once, writes a document for each payout still waiting, raises the alarms; returns the reviews."""
    chain = pr.read_chain(record, prices)
    out.mkdir(parents=True, exist_ok=True)
    reviews = [pr.review(chain, x["pool"], x["id"]) for x in chain["payouts"] if x["state"] == "Due"]
    for r in reviews:
        write(out, pr.card(r, chain))
        a = alarm_of(r)
        key = f"{r['pool']}-{r['id']}"
        mark = None if a is None else [a["action"], a["kinds"], a["reasons"]]
        if a is not None and said.get(key) != mark:
            alarm("payout_held" if a["action"] == "hold" else "payout_needs_a_stop", **a)
        said[key] = mark
    core.log("review_done", payouts=len(reviews), block=chain["latest"],
             suspicious=sum(r["verdict"] == "suspicious" for r in reviews))
    return reviews


def read_prices(path: pathlib.Path):
    """The stops' loop's journal (`payout_review.load_prices`), or None where there is none to read: then no stop is
    checked in every block, and each payout with a stop is held (`payout_review.stop_doubt`), never passed as clean."""
    try:
        return pr.load_prices(path.read_text())
    except OSError as exc:
        core.log("prices_unread", path=str(path), error=type(exc).__name__, then="every payout with a stop is held")
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deployment", required=True)
    ap.add_argument("--out", default=str(pr.REVIEWS_DIR), help="where each payout's document is written")
    ap.add_argument("--prices", help="the stops' loop's journal of prices (stop_filler.PriceJournal): every block of each "
                    "stop's time is judged by it; missing, every payout with a stop is held")
    ap.add_argument("--said", help="the file that remembers the alarms raised (default <out>/said.json)")
    ap.add_argument("--flag", help="the flag file each alarm is added to")
    sn.add_args(ap)
    args = ap.parse_args(argv)
    with chk.node_errors():
        sn.confirm_service(args, args.deployment, c, sends=False)
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    out = pathlib.Path(args.out)
    said_path = pathlib.Path(args.said) if args.said else out / "said.json"
    try:
        said = json.loads(said_path.read_text())
    except (OSError, ValueError):
        said = {}
    prices = read_prices(pathlib.Path(args.prices)) if args.prices else None
    alarm = sf.Alarm(pathlib.Path(args.flag) if args.flag else None)
    with chk.node_errors():
        one_pass(record, out, prices, said, alarm)
    # The channel runs in threads of their own (stop_filler.Alarm): a run of the timer waits for them before it ends.
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(35)
    said_path.parent.mkdir(parents=True, exist_ok=True)
    said_path.write_text(json.dumps(said) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
