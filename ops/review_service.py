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
fails is an alarm too (once an hour), and so is a unit that failed (`--unit-failed`, its OnFailure=). A pass that ran
out of its time before it read to the head is no failure: it saved what it read, says `review_behind` with the block it
read to and the head, writes no document, and ends with code 0; passes that end so for over an hour without one that
reads to the head are the alarm `review_behind_long`, said again every hour while it lasts. One hold waits
with its alarm: a payout held by nothing but the buyers' funding still being read (each new buyer starts that reading
again, for every payout waiting) is said only once that has lasted over an hour without a break, or at once from 24
hours before the payout goes out by itself (`links_only`); its document says the hold from the first pass.

Every block of each stop's time is judged by the journals of prices (--prices, the keeper's; --prices2, the second
writer's from another machine), the venue's minute candles kept in --out (`ops/venue_candles.py`, fetched each pass),
and, when there is one, a node that answers for old blocks (--archive-rpc). The logs read are kept in --out/scan.json:
each pass reads only the new blocks. The end of each pass goes to --out/status.json, whose age the stops' loop
watches; it says too how far the reading of the buyers' funding is (`linksLeft`: buyers not read, blocks left), and,
after a pass that did not read to the head, how far that reading is (`behind`: the block read to, the head, and since
when).

It sends nothing and holds no key. On the host: colosseum-payout-review@<name>.service run by its .timer every 10
minutes (ops/host). Testnet unless both COLOSSEUM_NETWORK=mainnet and --network mainnet name mainnet.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import stat
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
# A pass that ends so (`payout_review.Behind`) is no failure: the next one goes on from what it saved. Passes that end
# so for longer than BEHIND_LONG_S with no pass between them that read to the head are an alarm (no document is fresh
# meanwhile), said again every BEHIND_EVERY_S while it lasts.
BEHIND_LONG_S = 3_600
BEHIND_EVERY_S = 3_600
# Seconds the alarms of one pass may take in all, once every document is written (review 15 of #338): a channel that
# hangs (30 s an alarm at most) cannot keep the documents from the disk or run the pass past its unit's time; an alarm
# not said in it is not remembered, and the next pass says it.
ALARM_BUDGET_S = 60
# A payout held by nothing but the buyers' funding still being read (`payout_review.LINKS_UNREAD`) asks nothing of a
# person: the passes read on by themselves, and each new buyer starts that reading again for every payout waiting. Its
# alarm waits until such a hold has lasted longer than this without a break (the pass that first saw it is kept in
# `said`, `linksSince`); the hold itself is in the document at once.
LINKS_WAIT_S = 3_600


def links_only(a: dict, now: int) -> bool:
    """Whether the alarm `a` is one that may wait LINKS_WAIT_S: of a payout held, with no reason but the buyers' funding
    still being read, and further than the first of BEFORE_S from the moment it goes out by itself and from its last
    moment for a stop. Nearer than that nothing waits: it is said at once, and again as each of BEFORE_S comes. A
    payout whose time of going out is not known is taken as near."""
    if a["action"] != "hold" or not a["kinds"] or set(a["kinds"]) != {pr.LINKS_UNREAD}:
        return False
    near = [t for t in (a["paysAt"], a["deadline"]) if t is not None]
    return a["paysAt"] is not None and min(near) - now > BEFORE_S[0]


def alarm_of(r: dict) -> dict | None:
    """The alarm a review calls for, or None: suspicious and not settled by a stop the checker sends itself. What it
    tells the person to do is what can still be done: a stop only where the pool would take one (`stoppable`; a payout
    stopped once already, its stop lifted, takes no second, and then there is no deadline for one), and the draft of a
    stop of reason 3 only where the review made one (`stopDraft`)."""
    if r["verdict"] != "suspicious" or r["action"] == "stop":
        return None
    by_itself = r["action"] != "hold" and (r["approvals"] is not True or r["approved"])
    pays = r["dueAt"] if by_itself else r["hardAt"]
    stoppable = r.get("stoppable") is not False
    if stoppable:
        stop = ("a person stops it for reason 3 (the draft is in the review)" if r.get("stopDraft") else
                "a person stops it for reason 3 (the review made no draft of one: its reasons are what there is)")
        do = (f"it pays by itself: {stop} before the deadline" if by_itself else
              f"held: it is not approved, and it goes out by itself at its ceiling ({utc(pays)}) unless {stop} before "
              "then; do not approve it")
    else:
        none = ("the pool would refuse a stop of it now (it was stopped once already, or its time to stop is over): "
                "there is none to send")
        do = (f"it pays by itself ({utc(pays)}), and {none}" if by_itself else
              f"held: it is not approved, and it goes out by itself at its ceiling ({utc(pays)}); {none}; do not "
              "approve it")
    return {"pool": r["pool"], "id": r["id"], "trader": r["trader"], "action": r["action"], "kinds": r["kinds"],
            "reasons": r["reasons"], "deadline": stop_deadline(r) if stoppable else None, "paysAt": pays, "do": do}


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
    time budget), writes a document for each payout still waiting and the pass's end, then raises the alarms, ALARM_BUDGET_S
    for all of them (a hung channel keeps no document from the disk); returns the reviews. An alarm is remembered in
    `said` only once the channel took it (`alarm(..., wait=True)` not False): one that did not go out, raised, or was
    not said within the budget, is said again at the next pass, and keeps none of the others from being said. The pass's end is written to status.json beside the documents,
    for the stops' loop's watch of the review's age (`stop_filler.StopLoop`). The alarm of a payout held only while the
    buyers' funding is read waits LINKS_WAIT_S of that (`links_only`); every other is said in the pass that finds it."""
    chain = pr.read_chain(record, prices, **read)
    out.mkdir(parents=True, exist_ok=True)
    reviews = [pr.review(chain, x["pool"], x["id"]) for x in chain["payouts"] if x["state"] == "Due"]
    now = int(chain["now"])
    for r in reviews:
        write(out, pr.card(r, chain))
    write_status(out, {"at": int(time.time()), "chainNow": now, "block": chain["latest"], "due": len(reviews),
                       "held": sum(r["action"] == "hold" for r in reviews), "linksLeft": links_left(chain)})
    until = time.monotonic() + ALARM_BUDGET_S
    for r in reviews:
        a = alarm_of(r)
        key = f"{r['pool']}-{r['id']}"
        if a is None:
            said[key] = None
            continue
        mark = [a["action"], a["kinds"], a["reasons"]]
        since = None
        if links_only(a, now):
            prev = said.get(key)
            since = prev.get("linksSince") if isinstance(prev, dict) else None
            if isinstance(since, bool) or not isinstance(since, int):
                since = now  # the first pass that finds it held so: what was said of it before was of another hold
                said[key] = {"linksSince": since}
                core.log("review_alarm_waits", payout=key, why="held only while the buyers' funding is read",
                         then=f"said if it is still so after {utc(since + LINKS_WAIT_S)}")
            if now - since <= LINKS_WAIT_S:
                continue
        elif isinstance(said.get(key), dict):
            # Held by something else now, or near its time: the hour of the funding alone is broken, whether or not
            # this pass's alarm goes out, and starts anew if the funding is the only reason again.
            said[key].pop("linksSince", None)
        if due_to_say(said.get(key), mark, now, a["deadline"]):
            if time.monotonic() > until:
                core.log("review_alarm_not_said", payout=key, why=f"the pass's {ALARM_BUDGET_S} s for alarms are spent",
                         then="said at the next pass")
                continue
            try:
                took = alarm("payout_held" if a["action"] == "hold" else "payout_needs_a_stop", wait=True, **a)
            except Exception as exc:  # this alarm's own failure: not taken, and the alarms after it still go
                core.log("review_alarm_raised", payout=key, error=type(exc).__name__)
                took = False
            if took is not False:
                said[key] = {"mark": mark, "at": now, "before": [b for b in BEFORE_S
                                                                 if a["deadline"] is not None and a["deadline"] - now <= b],
                             **({} if since is None else {"linksSince": since})}
            else:
                core.log("review_alarm_not_taken", payout=key, then="said again at the next pass")
    core.log("review_done", payouts=len(reviews), block=chain["latest"],
             suspicious=sum(r["verdict"] == "suspicious" for r in reviews))
    return reviews


def links_left(chain: dict) -> dict | None:
    """What status.json says of the reading of the buyers' funding (`payout_review.LogScan.links_left`): how many
    buyers are not read to the end and how many blocks are left for them, so that a person looking at payouts held as
    `links-unread` sees it move. Two whole numbers and nothing else of what the chain's reading holds; None where the
    reading gave none."""
    left = chain.get("linksLeft")
    try:
        return {"buyers": int(left["buyers"]), "blocks": int(left["blocks"])}
    except (TypeError, KeyError, ValueError):
        return None


def write_status(out: pathlib.Path, status: dict) -> None:
    tmp = out / "status.json.part"
    tmp.write_text(json.dumps(status) + "\n")
    os.replace(tmp, out / "status.json")


def is_time(value) -> bool:
    """Whether `value` is a time as `said` keeps one: a number, and no true or false."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fell_behind(out: pathlib.Path, behind: pr.Behind, said: dict, now: float) -> None:
    """A pass that ran out of its time before it read to the head (`payout_review.Behind`): no failure. It says
    `review_behind` with the block it read to and the head, and writes its end to status.json: its own time (`at`: the
    review is alive), `behind` (`readTo`, `head`, and `since`: the end of the first such pass in a row), and how many
    payouts waited and were held at the last pass that read to the head (`due`, `held`, where there was one), so that
    whoever watches the file's age tells a review that is catching up from one that stands, and holds one that then
    stops to the payouts that waited. No document is written by such a pass: `read_chain` raises before any payout is
    reviewed. `since` is kept in `said` ("_behind") until a pass reads to the head (`main`)."""
    kept = said.get("_behind")
    if not isinstance(kept, dict) or not is_time(kept.get("since")):
        kept = said["_behind"] = {"since": int(now)}
    since = int(kept["since"])
    core.log("review_behind", read_to=behind.read_to, head=behind.head, since=utc(since),
             then="no document this pass; the next one goes on from there")
    try:
        last = json.loads((out / "status.json").read_text())
    except (OSError, ValueError):
        last = None
    waited = {k: last[k] for k in ("due", "held") if isinstance(last, dict) and is_time(last.get(k))}
    out.mkdir(parents=True, exist_ok=True)
    write_status(out, {"at": int(now), **waited, "behind": {"readTo": behind.read_to, "head": behind.head, "since": since}})


def behind_long(behind: pr.Behind, now: float, said: dict, alarm) -> None:
    """The passes have ended behind for longer than BEHIND_LONG_S in a row (since `fell_behind` first kept it): the
    alarm `review_behind_long`, said again every BEHIND_EVERY_S while it lasts, remembered only once the channel took
    it. Only a pass that reads to the head ends the row; one that fails between two that fell behind does not. An
    alarm that raises is one not taken, as in `one_pass`: the pass fell behind and did not fail, and ends so."""
    kept = said["_behind"]
    if now - kept["since"] <= BEHIND_LONG_S:
        return
    if is_time(kept.get("said")) and now - kept["said"] < BEHIND_EVERY_S:
        return
    try:
        took = alarm("review_behind_long", wait=True, read_to=behind.read_to, head=behind.head, since=utc(int(kept["since"])),
                     why="the payout review has not read the chain to its head for over an hour: each pass runs out of its "
                         "time and the next goes on from there; no document is fresh meanwhile, approve refuses every "
                         "payout, and a held payout is not said again")
    except Exception as exc:  # the alarm's own failure: not taken, and said at the next pass
        core.log("review_alarm_raised", what="review_behind_long", error=type(exc).__name__)
        took = False
    if took is not False:
        kept["said"] = now


def read_prices(path: pathlib.Path, expect: dict | None = None, second: pathlib.Path | None = None,
                lo_t: int | None = None, hi_t: int | None = None, want: dict | None = None, unread: list | None = None):
    """The stops' loop's journal and the second writer's (`payout_review.read_journals`: their day files of the stops'
    time, each of this deployment), or None where there is none to read: then no stop is checked in every block, and
    each payout with a stop is held (`payout_review.stop_doubt`), never passed as clean. A second journal whose
    reading raised is added to `unread` (its blocks rest on one journal; `second_unread` says it)."""
    try:
        got = pr.read_journals([path] + ([second] if second else []), expect, lo_t, hi_t, want)
        if got is None:
            raise FileNotFoundError(path.name)
        if unread is not None:
            unread += got.unread
        return got
    except OSError as exc:
        core.log("prices_unread", path=str(path), error=type(exc).__name__, then="every payout with a stop is held")
        return None


def second_unread(found: list, now: float, said: dict, alarm) -> None:
    """The second writer's journal could not be read to its end this pass (`found`, from `read_prices`): an alarm, every
    REPEAT_S while it lasts. The pass went on: its documents are written, and the stops standing in the blocks that
    journal did not give are held as resting on one journal."""
    if not found:
        said.pop("_second_unread", None)
        return
    prev = said.get("_second_unread")
    if prev is not None and now - prev < REPEAT_S:
        return
    if alarm("second_journal_unread", wait=True, errors=sorted(set(found))[:5],
             why="the second writer's journal of prices could not be read: the payouts whose stops stood in its blocks "
                 "are held as resting on one journal") is not False:
        said["_second_unread"] = now


def second_stale(path: pathlib.Path | None, now: float, said: dict, alarm, limit: int = 1_800) -> None:
    """The second writer's journal not written to for `limit` seconds: an alarm, every REPEAT_S while it lasts. Its
    blocks are then the keeper's journal's alone, and a restart of the keeper holds payouts again."""
    if path is None:
        return
    # Only a plain day file counts, as only one is read (`payout_review.journal_files`): a link, a file gone since it
    # was listed or a folder that cannot be listed is no journal coming, and that is the alarm, never a failed pass
    # (review 16 of #338).
    times = []
    try:
        for f in path.parent.glob(f"{path.stem}-????-??-??{path.suffix}"):
            try:
                st = os.lstat(f)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                times.append(st.st_mtime)
    except OSError:
        times = []
    newest = max(times, default=None)
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


# The watch of the host from the second machine (`watch_host`, review 15 of #338): the stops' journal is written every
# block, so three minutes without a line is the loop or the host down; the review runs every 10 minutes, so 30 without
# a pass is the review down (the stops' loop's own `review_stale` says the same, from the host, while it runs). A host
# not answering, or numbers older than these, is the alarm `host_not_alive`, said again every hour while it lasts.
HOST_PRICES_STALE_S = 180
HOST_REVIEW_STALE_S = 1_800
HOST_REPEAT_S = 3_600
# How far the answer's own time may stand from this machine's clock (review 16 of #338): an answer stamped further off
# is an old one (a cache between the two machines, a replay) or a clock gone wrong, and says nothing of the host now.
HOST_CLOCK_APART_S = 120
HEALTH_MAX_BYTES = 64 * 1024


def fetch_health(url: str, timeout: float = 10.0) -> dict:
    """The host's health route (gateway/simindex.py: its pulse, `prices_at` and `review_at`), as JSON."""
    import secrets
    import urllib.request
    if not url.startswith(("https://", "http://")):
        raise ValueError("the health route must be an http(s) address")
    # A parameter no request before had: a cache between the two machines has no answer kept for it (the route itself
    # says no-store, and the answer's own time is held to this machine's clock, `host_problem`).
    fresh = f"{url}{'&' if '?' in url else '?'}r={secrets.token_hex(8)}"
    with urllib.request.urlopen(urllib.request.Request(fresh, headers={"Cache-Control": "no-cache"}), timeout=timeout) as resp:
        got = json.loads(resp.read(HEALTH_MAX_BYTES + 1)[:HEALTH_MAX_BYTES])
    if not isinstance(got, dict):
        raise ValueError("not a JSON object")
    return got


def host_problem(body: dict, now: float) -> tuple[str, str] | None:
    """What is wrong with the host by its pulse, (kind, words), or None. The answer's own time (`now`) must stand
    within HOST_CLOCK_APART_S of this machine's clock: one further off, or with no time, is not known to be the host
    as it is now (kind "clock"). Within it, the ages are counted on the host's clock, so the seconds the two machines
    stand apart are no age of a journal."""
    at = body.get("now")
    if not isinstance(at, int) or isinstance(at, bool):
        return "clock", "the host's answer carries no time of its own: it cannot be told from an old one"
    if abs(at - now) > HOST_CLOCK_APART_S:
        return "clock", (f"the host's answer is stamped {int(abs(at - now))} s {'before' if at < now else 'after'} this "
                         "machine's clock: an old answer (a cache, a replay) or a clock gone wrong, and no word of the "
                         "host as it is now")
    clock = at
    prices, review = body.get("prices_at"), body.get("review_at")
    if not isinstance(prices, int) or isinstance(prices, bool):
        return "prices", "the host says no time of the stops' journal: the stops' loop is not writing it"
    if clock - prices > HOST_PRICES_STALE_S:
        return "prices", (f"the stops' journal on the host has not been written for {clock - prices} s: the stops' loop "
                          "or the keeper is down, and no stop is carried out")
    if not isinstance(review, int) or isinstance(review, bool):
        return "review", "the host says no pass of the payout review: no document is fresh, approve refuses every payout"
    if clock - review > HOST_REVIEW_STALE_S:
        return "review", (f"the payout review on the host has not finished a pass for {clock - review} s: no document "
                          "is fresh, and a held payout is not said again")
    return None


def watch_host(url: str, state: dict, alarm, now: float, fetch=None) -> str | None:
    """One look at the host from the second machine: its health route read, an alarm on its own channel when it does
    not answer or its pulse is old (`host_problem`), said again every HOST_REPEAT_S while the same, at once when it
    changes; remembered in `state` only once the channel took it. Returns what is wrong, or None. The address of the
    route goes to no alarm and no log line."""
    try:
        found = host_problem((fetch or fetch_health)(url), now)
    except Exception as exc:
        found = ("silent", f"the host's health route did not answer ({type(exc).__name__}): the host, its index or its "
                           "network is down")
    if found is None:
        if state.get("kind"):
            core.log("host_alive_again", was=state.get("kind"))
        state.clear()
        return None
    kind, why = found
    if state.get("kind") == kind and now - float(state.get("at", 0)) < HOST_REPEAT_S:
        return why
    if alarm("host_not_alive", wait=True, kind=kind, why=why) is not False:
        state.update(kind=kind, at=now)
    return why


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
    ap.add_argument("--watch-host", metavar="URL", default=None,
                    help="on the second machine: read the host's health route once (COLOSSEUM_HOST_HEALTH_URL), raise "
                         "host_not_alive on this machine's channel if it does not answer or its pulse is old, and end")
    ap.add_argument("--state", help="with --watch-host: the file that remembers what was said")
    ap.add_argument("--say-test", action="store_true",
                    help="say one test alarm (alarm_test) on the channel, COLOSSEUM_ALERT_CMD, and end: exit 0 once its "
                         "command took it, 1 when it failed or there is no channel; run by hand when the channel is set up")
    sn.add_args(ap, service=True)
    args = ap.parse_args(argv)
    out = pathlib.Path(args.out)
    flag = pathlib.Path(args.flag) if args.flag else None
    if args.say_test:
        # A channel of spaces alone is none (a shell runs it and says nothing failed).
        took = bool(os.environ.get("COLOSSEUM_ALERT_CMD", "").strip()) and sf.Alarm(None)(
            "alarm_test", wait=True, why="a test of the alarm channel, said by hand: nothing is wrong")
        if took is not True:
            print("the alarm channel did not take the test: COLOSSEUM_ALERT_CMD is not set, or its command failed",
                  file=sys.stderr)
            return 1
        print("the alarm channel took the test alarm")
        return 0
    if args.unit_failed:
        return unit_failed(args.unit_failed, flag, out / f"failed-{args.unit_failed}.at")
    if args.watch_host is not None:
        url = args.watch_host or os.environ.get("COLOSSEUM_HOST_HEALTH_URL", "")
        if not url:
            ap.error("--watch-host needs the host's health route (or COLOSSEUM_HOST_HEALTH_URL)")
        # The watch is nothing but its alarm: with no channel it tells nobody, on any network (review 16 of #338).
        if not os.environ.get("COLOSSEUM_ALERT_CMD", "").strip():
            raise SystemExit("refusing to run the host's watch: COLOSSEUM_ALERT_CMD is not set (the alarm channel of this "
                             "machine): a watch with no channel tells nobody")
        state_path = pathlib.Path(args.state) if args.state else out / "host-watch.json"
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        watch_host(url, state if isinstance(state, dict) else {}, sf.Alarm(flag), time.time())
        save_said(state_path, state if isinstance(state, dict) else {})
        return 0
    if not args.deployment:
        ap.error("--deployment is required")
    with chk.node_errors():
        sn.confirm_service(args, args.deployment, c, sends=False)
    confirmed(args, args.deployment)
    sf.require_channel("the payout review")
    record = deployments.load(args.deployment)
    if "SimPoolFactory" not in record:
        raise SystemExit(f"deployment {args.deployment!r} has no SimPoolFactory")
    said_path = pathlib.Path(args.said) if args.said else out / "said.json"
    said = load_said(said_path)
    alarm = sf.Alarm(flag)
    expect = {"chain": c.CHAIN_ID, "factory": record["SimPoolFactory"], "label": args.deployment}
    second = pathlib.Path(args.prices2) if args.prices2 else None
    if second is not None and args.prices and pr.same_journal(pathlib.Path(args.prices), second):
        # One journal named twice is no second journal: every stop would be held as resting on one. Testnet says it at
        # each pass (the review's text); on mainnet the service does not start so.
        if c.NETWORK.name == "mainnet":
            raise SystemExit("refusing to run on mainnet: --prices2 names the same journal as --prices; the second "
                             "writer's journal is another machine's (docs/HOSTING.md)")
        core.log("second_journal_is_the_first", then="every payout with a stop is held as resting on one journal")
    prices = None
    unread: list = []
    if args.prices:
        prices = lambda lo, hi, want: read_prices(pathlib.Path(args.prices), expect, second, lo, hi, want, unread)  # noqa: E731
    bps = args.candle_bps
    if bps is None:
        bps = int(os.environ.get("COLOSSEUM_CANDLE_BPS", "") or
                  (pr.CANDLE_BPS if c.NETWORK.name == "mainnet" else TESTNET_CANDLE_BPS))
    archive = pr.archive_oracle(args.archive_rpc) if args.archive_rpc else None
    behind = None
    try:
        candles = None if args.no_candles else fetch_candles(out)
        try:
            # `Behind` is no failure of the node: it passes `node_errors` as it is. Taken for one, every pass of a
            # long first reading ended as `review_failed` with code 1, and the branch below was never reached.
            with chk.node_errors(pr.Behind):
                one_pass(record, out, prices, said, alarm, scan=out / "scan.json", candles=candles, candle_bps=bps,
                         archive=archive, until=time.monotonic() + PASS_BUDGET_S)
        except pr.Behind as exc:
            behind = exc
            fell_behind(out, exc, said, time.time())
        else:
            said.pop("_failed", None)
            said.pop("_behind", None)
    except BaseException as exc:
        if time.time() - float(said.get("_failed", 0)) >= FAILED_EVERY_S:
            if alarm("review_failed", wait=True, error=chk.quiet(exc, 200),
                     why="the payout review's pass failed: no document is fresh, approve refuses every payout, and a "
                         "held payout is not said again until it works") is not False:
                said["_failed"] = time.time()
        save_said(said_path, said)
        raise
    try:
        if behind is None:
            second_unread(unread, time.time(), said, alarm)
        else:  # no journal was read by a pass that fell behind: it says nothing of the second one's reading
            behind_long(behind, time.time(), said, alarm)
        second_stale(second, time.time(), said, alarm)
    finally:
        save_said(said_path, said)  # whatever these do, the alarms the pass said stay said
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


def load_said(path: pathlib.Path) -> dict:
    """What `save_said` wrote: the alarms raised, by payout, and the times a failed pass and a stale second journal
    were last said. A file not there, not JSON or not an object is no memory (`[]` parses, and every pass after would
    fail on it): each alarm is said again, and the file written anew. A time that is no number is forgotten alike."""
    try:
        said = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(said, dict):
        return {}
    for key in ("_failed", "_second", "_second_unread"):
        if key in said and (isinstance(said[key], bool) or not isinstance(said[key], (int, float))):
            del said[key]
    return said


def save_said(path: pathlib.Path, said: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(said) + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
