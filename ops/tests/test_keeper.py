"""Keeper tests. Offline: the chain, the Hyperliquid info API and sending are one fake.

    spike/.venv/bin/python -m unittest discover -s ops/tests -t .
"""

from __future__ import annotations

import pathlib
import re
import tempfile
import unittest
from unittest import mock

from ops import keeper

ROOT = pathlib.Path(__file__).resolve().parents[2]
FACTORY = "0x00000000000000000000000000000000000000F1"
POOL_A = "0x00000000000000000000000000000000000000A1"
POOL_B = "0x00000000000000000000000000000000000000B1"
POOL_C = "0x00000000000000000000000000000000000000C1"
CHALLENGE_A = "0x00000000000000000000000000000000000000A2"
TRADER = "0x00000000000000000000000000000000000000D1"
STRANGER = "0x00000000000000000000000000000000000000E1"
ZERO = keeper.ZERO
DAY = 86400
NOON = 20_000 * DAY + 12 * 3600
JUST_AFTER_MIDNIGHT = 20_000 * DAY + 60


def topic(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:].lower()


def challenge_log(block: int, pool: str, emitter: str = FACTORY, challenge: str = CHALLENGE_A) -> dict:
    return {"address": emitter.lower(), "blockNumber": hex(block),
            "topics": [keeper.CHALLENGE_CREATED, topic(challenge), topic(pool), topic(TRADER)]}


def pool_created_log(block: int, pool: str) -> dict:
    other = "0x" + "11" * 32
    return {"address": FACTORY.lower(), "blockNumber": hex(block), "topics": [other, topic(pool), topic(TRADER)]}


class FakeChain:
    """Pools, challenges, logs and the info API, with every send recorded."""

    def __init__(self):
        self.latest = 100
        self.logs: list[dict] = []
        self.honour_address_filter = True
        self.log_queries: list[dict] = []
        self.pools: dict[str, dict] = {}
        self.challenges: dict[str, dict] = {}
        self.orders: dict[str, list] = {}
        self.positions: dict[str, list] = {}
        self.sent: list[tuple[str, str, list]] = []
        self.fail_sends_to: set[str] = set()

    def add_pool(self, addr, stage=keeper.IDLE, challenge=ZERO, **over):
        self.pools[addr.lower()] = {"stage": stage, "challenge": challenge, "day": 19_999, "violation": 0,
                                    "assets": (3,), **over}

    def add_challenge(self, addr, status, **over):
        self.challenges[addr.lower()] = {"status": status, "capitalArrived": False, "createdAt": NOON - 60,
                                         "day": 19_999, "violation": 0, "deadline": NOON + DAY, "assets": (3,),
                                         **over}

    # spike.hlspike.common, as the keeper uses it
    def rpc(self, method, params=()):
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getLogs":
            q = params[0]
            self.log_queries.append(q)
            lo, hi = int(q["fromBlock"], 16), int(q["toBlock"], 16)
            out = []
            for entry in self.logs:
                if not lo <= int(entry["blockNumber"], 16) <= hi:
                    continue
                if self.honour_address_filter and entry["address"].lower() != q["address"].lower():
                    continue
                if entry["topics"][0] not in q["topics"]:
                    continue
                out.append(entry)
            return out
        raise AssertionError(f"unexpected rpc {method}")

    def call_view(self, to, signature, types, args, out):
        name = signature.split("(")[0]
        state = self.pools.get(to.lower()) or self.challenges.get(to.lower())
        if state is None:
            raise AssertionError(f"read from an address the keeper should not touch: {to}")
        if name == "rules":
            return ((500, 1000, 300, state["assets"]),)
        if name == "violation":
            return (state["violation"],)
        return (state[name],)

    def info_post(self, body):
        kind = body["type"]
        if kind == "meta":
            return {"universe": [{"name": n} for n in ("SOL", "APT", "ATOM", "BTC", "ETH")]}
        if kind == "openOrders":
            return self.orders.get(body["user"].lower(), [])
        if kind == "clearinghouseState":
            return {"assetPositions": [{"position": {"coin": coin}} for coin in self.positions.get(body["user"].lower(), [])]}
        raise AssertionError(f"unexpected info call {kind}")

    def transact(self, wallet, to, signature, types, args):
        if to.lower() in self.fail_sends_to:
            raise RuntimeError("execution reverted")
        self.sent.append((to.lower(), signature.split("(")[0], args))
        return {"transactionHash": "0x" + "ab" * 32}

    def calls_to(self, addr):
        return [(fn, args) for to, fn, args in self.sent if to == addr.lower()]


class KeeperTest(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain()
        patcher = mock.patch.object(keeper, "c", self.chain)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = mock.patch.object(keeper.time, "time", return_value=NOON)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        quiet = mock.patch.object(keeper, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = pathlib.Path(self.tmp.name) / "keeper.json"

    def make(self, dry=False, window=50, max_windows=50, start=1):
        return keeper.Keeper(FACTORY, object(), dry, self.state, start, window=window, max_windows=max_windows)

    def at(self, t):
        self.clock.stop()
        self.clock = mock.patch.object(keeper.time, "time", return_value=t)
        self.clock.start()

    def follow_a(self, pool_stage=keeper.CHALLENGE, **challenge):
        self.chain.add_pool(POOL_A, stage=pool_stage, challenge=CHALLENGE_A)
        self.chain.add_challenge(CHALLENGE_A, **challenge)
        self.chain.logs.append(challenge_log(10, POOL_A))


class Following(KeeperTest):
    def test_follows_pools_named_by_the_factorys_challenge_events_only(self):
        self.follow_a(status=keeper.ACTIVE)
        self.chain.add_pool(POOL_B)
        self.chain.add_pool(POOL_C)
        self.chain.logs += [pool_created_log(5, POOL_B), pool_created_log(6, POOL_C)]
        k = self.make()
        k.one_pass()
        self.assertEqual(k.live, {keeper.to_checksum_address(POOL_A)})
        self.assertTrue(all(q["address"].lower() == FACTORY.lower() for q in self.chain.log_queries))

    def test_an_rpc_that_ignores_the_address_filter_changes_nothing(self):
        # Anyone can emit an event that looks like the factory's. If the keeper followed it, an
        # attacker's contract that answers like a broken pool would get the keeper's stop
        # transactions, and their gas, on every pass.
        self.follow_a(status=keeper.ACTIVE)
        self.chain.add_pool(STRANGER, stage=keeper.FUNDED, violation=1)
        self.chain.logs.append(challenge_log(11, STRANGER, emitter=STRANGER))
        self.chain.honour_address_filter = False
        k = self.make()
        k.one_pass()
        self.assertEqual(k.live, {keeper.to_checksum_address(POOL_A)})
        self.assertEqual(self.chain.calls_to(STRANGER), [])

    def test_idle_pool_is_dropped_and_comes_back_with_its_next_challenge(self):
        self.chain.add_pool(POOL_A)  # idle, no challenge: the last one has settled
        self.chain.logs.append(challenge_log(10, POOL_A))
        k = self.make()
        k.one_pass()
        self.assertEqual(k.live, set())
        self.chain.add_pool(POOL_A, stage=keeper.CHALLENGE, challenge=CHALLENGE_A)
        self.chain.add_challenge(CHALLENGE_A, status=keeper.CREATED)
        self.chain.logs.append(challenge_log(150, POOL_A))
        self.chain.latest = 200
        k.one_pass()
        self.assertEqual(k.live, {keeper.to_checksum_address(POOL_A)})

    def test_logs_are_read_in_windows_and_the_state_resumes(self):
        self.follow_a(status=keeper.ACTIVE)
        self.chain.latest = 35
        k = self.make(window=10, max_windows=2, start=0)
        k.one_pass()
        spans = [(int(q["fromBlock"], 16), int(q["toBlock"], 16)) for q in self.chain.log_queries]
        self.assertEqual(spans, [(0, 9), (10, 19)])
        self.assertEqual(k.next_block, 20)

        again = self.make(window=10, max_windows=2, start=0)  # a restart reads the state file
        self.assertEqual((again.next_block, again.live), (20, k.live))
        again.one_pass()
        spans = [(int(q["fromBlock"], 16), int(q["toBlock"], 16)) for q in self.chain.log_queries[2:]]
        self.assertEqual(spans, [(20, 29), (30, 35)])
        self.assertEqual(again.next_block, 36)

    def test_log_windows_stay_within_what_hyperevm_accepts(self):
        self.chain.latest = 130
        self.make(start=0).one_pass()
        spans = [int(q["toBlock"], 16) - int(q["fromBlock"], 16) + 1 for q in self.chain.log_queries]
        self.assertEqual(spans, [50, 50, 31])
        for bad in (0, 51, 1000):
            with self.assertRaises(SystemExit, msg=bad):
                self.make(window=bad)

    def test_a_pass_always_reads_logs(self):
        # With no windows a pass would read nothing and still report itself done.
        for bad in (0, -1):
            with self.assertRaises(SystemExit, msg=bad):
                self.make(max_windows=bad)
        self.chain.latest = 5
        k = self.make(max_windows=1, start=0)
        k.one_pass()
        self.assertEqual(len(self.chain.log_queries), 1)
        self.assertEqual(k.next_block, 6)

    def test_a_failed_pass_keeps_its_place(self):
        self.follow_a(status=keeper.ACTIVE)
        k = self.make()
        self.chain.rpc = mock.Mock(side_effect=RuntimeError("eth_blockNumber: rate limited"))
        with self.assertRaises(RuntimeError):
            k.one_pass()
        self.assertEqual(k.next_block, 1)  # nothing skipped

    def test_the_service_logs_a_failed_pass_instead_of_dying(self):
        record = {"chain_id": 998, "PoolFactory": FACTORY, "block": 1}
        with mock.patch.object(keeper.deployments, "load", return_value=record), \
                mock.patch.object(self.chain, "assert_testnet", create=True), \
                mock.patch.object(keeper.Keeper, "one_pass", side_effect=RuntimeError("rate limited")), \
                mock.patch.object(keeper.sys, "argv", ["keeper", "--deployment", "demo", "--once", "--dry-run",
                                                       "--state", str(self.state)]):
            self.assertEqual(keeper.main(), 1)
        events = [call.args[0] for call in keeper.log.call_args_list]
        self.assertIn("pass_failed", events)

    def test_state_of_another_factory_is_refused(self):
        self.make().save()
        with self.assertRaises(SystemExit):
            keeper.Keeper("0x00000000000000000000000000000000000000F2", None, True, self.state, 1)


class ChallengeCalls(KeeperTest):
    def test_activates_once_the_capital_is_there(self):
        self.follow_a(status=keeper.CREATED, capitalArrived=True)
        self.make().one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("activate", [])])

    def test_aborts_only_after_the_start_window(self):
        self.follow_a(status=keeper.CREATED, createdAt=NOON - keeper.START_WINDOW)
        k = self.make()
        k.one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [])
        self.at(NOON + 1)
        k.one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("abort", [])])

    def test_breach_names_open_orders_and_positions_outside_the_rules(self):
        self.follow_a(status=keeper.ACTIVE, violation=4)
        self.chain.orders[CHALLENGE_A.lower()] = [{"coin": "BTC", "oid": 7}, {"coin": "@107", "oid": 8}]
        self.chain.positions[CHALLENGE_A.lower()] = ["BTC", "ETH"]
        self.make().one_pass()
        [(fn, args)] = self.chain.calls_to(CHALLENGE_A)
        self.assertEqual(fn, "breach")
        cancels, extra, salt = args
        self.assertEqual((cancels, extra), ([(3, 7)], [4]))
        self.assertEqual(len(salt), 32)

    def test_expires_after_the_deadline_when_no_rule_is_broken(self):
        self.follow_a(status=keeper.ACTIVE, deadline=NOON)
        k = self.make()
        k.one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [])
        self.at(NOON + 1)
        k.one_pass()
        self.assertEqual([fn for fn, _ in self.chain.calls_to(CHALLENGE_A)], ["expire"])

    def test_checkpoint_only_just_after_midnight_and_once_a_day(self):
        self.follow_a(status=keeper.ACTIVE, deadline=NOON + 5 * DAY)
        k = self.make()
        k.one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [])
        self.at(JUST_AFTER_MIDNIGHT)
        k.one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("checkpoint", [])])
        self.chain.challenges[CHALLENGE_A.lower()]["day"] = JUST_AFTER_MIDNIGHT // DAY
        k.one_pass()
        self.assertEqual(self.chain.calls_to(CHALLENGE_A), [("checkpoint", [])])

    def test_every_stopped_challenge_is_settled_and_a_settled_one_is_left_alone(self):
        for status in keeper.STOPPED + (keeper.SETTLED,):
            self.chain.sent.clear()
            self.follow_a(status=status)
            self.make().one_pass()
            fns = [fn for fn, _ in self.chain.calls_to(CHALLENGE_A)]
            self.assertEqual(fns, [] if status == keeper.SETTLED else ["settle"], status)
            self.state.unlink()

    def test_aborted_is_one_of_the_stopped_states(self):
        self.assertIn(keeper.ABORTED, keeper.STOPPED)
        self.assertNotIn(keeper.ACTIVE, keeper.STOPPED)


class PoolCalls(KeeperTest):
    def test_funded_pool_breach_and_closing_settles(self):
        self.chain.add_pool(POOL_A, stage=keeper.FUNDED, violation=1)
        self.chain.logs.append(challenge_log(10, POOL_A))
        k = self.make()
        k.one_pass()
        self.assertEqual([fn for fn, _ in self.chain.calls_to(POOL_A)], ["breach"])
        self.chain.add_pool(POOL_A, stage=keeper.CLOSING)
        k.one_pass()
        self.assertEqual([fn for fn, _ in self.chain.calls_to(POOL_A)], ["breach", "settleFunded"])

    def test_dry_run_sends_nothing(self):
        self.follow_a(status=keeper.ACTIVE, violation=1)
        self.make(dry=True).one_pass()
        self.assertEqual(self.chain.sent, [])

    def test_one_failed_call_does_not_stop_the_pass(self):
        # A passed challenge can still be settling while its pool trades funded. Its settle
        # reverting must not keep the pool's own stop from going out, nor the next pool's.
        self.follow_a(pool_stage=keeper.FUNDED, status=keeper.PASSED)
        self.chain.pools[POOL_A.lower()]["violation"] = 1
        self.chain.add_pool(POOL_B, stage=keeper.FUNDED, violation=1)
        self.chain.logs.append(challenge_log(12, POOL_B))
        self.chain.fail_sends_to.add(CHALLENGE_A.lower())
        self.make().one_pass()
        self.assertEqual([fn for fn, _ in self.chain.calls_to(POOL_A)], ["breach"])
        self.assertEqual([fn for fn, _ in self.chain.calls_to(POOL_B)], ["breach"])


class MatchesTheContracts(unittest.TestCase):
    @staticmethod
    def enum(path: str, name: str) -> list[str]:
        src = (ROOT / path).read_text()
        body = re.search(rf"enum {name} \{{(.*?)\}}", src, re.S).group(1)
        return [m.strip() for m in body.split(",") if m.strip()]

    def test_status_and_stage_numbers(self):
        status = self.enum("src/ChallengeAccount.sol", "Status")
        self.assertEqual([status.index(n) for n in ("Created", "Active", "Breached", "Expired", "Forfeited",
                                                     "Passed", "Aborted", "Settled")],
                         [keeper.CREATED, keeper.ACTIVE, keeper.BREACHED, keeper.EXPIRED, keeper.FORFEITED,
                          keeper.PASSED, keeper.ABORTED, keeper.SETTLED])
        stage = self.enum("src/Pool.sol", "Stage")
        self.assertEqual([stage.index(n) for n in ("Idle", "Challenge", "Funded", "Closing")],
                         [keeper.IDLE, keeper.CHALLENGE, keeper.FUNDED, keeper.CLOSING])

    def test_stopped_states_match_is_stopped(self):
        src = (ROOT / "src/ChallengeAccount.sol").read_text()
        body = re.search(r"function isStopped\(\).*?\{(.*?)\n    \}", src, re.S).group(1)
        named = re.findall(r"Status\.(\w+)", body)
        status = self.enum("src/ChallengeAccount.sol", "Status")
        self.assertEqual(sorted(status.index(n) for n in named), sorted(keeper.STOPPED))

    def test_checkpoint_window_and_start_window(self):
        ruled = (ROOT / "src/RuledAccount.sol").read_text()
        self.assertRegex(ruled, rf"CHECKPOINT_WINDOW = {keeper.CHECKPOINT_WINDOW // 60} minutes")
        challenge = (ROOT / "src/ChallengeAccount.sol").read_text()
        self.assertRegex(challenge, rf"START_WINDOW = {keeper.START_WINDOW // 3600} hours?")


if __name__ == "__main__":
    unittest.main()
