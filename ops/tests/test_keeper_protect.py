"""The keeper's check that every open position has its stop on Hyperliquid itself. Offline: the
chain and the info API are the fakes of test_keeper.py, answering frontendOpenOrders as well.

    spike/.venv/bin/python -m unittest ops.tests.test_keeper_protect
"""

from __future__ import annotations

import unittest
from unittest import mock

from ops import keeper
from ops.tests.test_keeper import CHALLENGE_A, POOL_A, FakeChain, KeeperTest, challenge_log


def stop_order(coin="BTC", side="A", sz="0.0", position_tpsl=True, kind="Stop Market"):
    return {"coin": coin, "side": side, "sz": sz, "isTrigger": True, "reduceOnly": True,
            "isPositionTpsl": position_tpsl, "orderType": kind, "oid": 1, "triggerPx": "57000"}


class Chain(FakeChain):
    def __init__(self):
        super().__init__()
        self.sizes: dict[str, list[tuple[str, str]]] = {}
        self.front: dict[str, list[dict]] = {}
        self.front_fails = False

    def info_post(self, body):
        user = body.get("user", "").lower()
        if body["type"] == "frontendOpenOrders":
            if self.front_fails:
                raise RuntimeError("429 Too Many Requests")
            return self.front.get(user, [])
        if body["type"] == "clearinghouseState":
            return {"assetPositions": [{"position": {"coin": coin, "szi": szi}} for coin, szi in self.sizes.get(user, [])]}
        return super().info_post(body)


class ProtectionCheck(KeeperTest):
    def setUp(self):
        super().setUp()
        self.chain = Chain()
        patcher = mock.patch.object(keeper, "c", self.chain)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged = []
        loud = mock.patch.object(keeper, "log", side_effect=lambda event, **f: self.logged.append((event, f)))
        loud.start()
        self.addCleanup(loud.stop)

    def active_challenge(self, sizes, orders, **challenge):
        self.chain.add_pool(POOL_A, stage=keeper.CHALLENGE, challenge=CHALLENGE_A)
        self.chain.add_challenge(CHALLENGE_A, status=keeper.ACTIVE, **challenge)
        self.chain.logs.append(challenge_log(10, POOL_A))
        self.chain.sizes[CHALLENGE_A.lower()] = sizes
        self.chain.front[CHALLENGE_A.lower()] = orders

    def missing(self):
        return [f for event, f in self.logged if event == "protection_missing"]

    def test_an_open_position_without_a_stop_is_called_out(self):
        self.active_challenge([("BTC", "0.005"), ("ETH", "0.0")], [])
        self.make().one_pass()
        self.assertEqual(self.missing(), [{"account": CHALLENGE_A, "coin": "BTC", "size": "0.005"}])

    def test_a_stop_for_the_whole_position_on_the_closing_side_is_enough(self):
        self.active_challenge([("BTC", "0.005"), ("ETH", "-0.1")],
                              [stop_order("BTC", "A"), stop_order("ETH", "B", sz="0.1", position_tpsl=False)])
        self.make().one_pass()
        self.assertEqual(self.missing(), [])

    def test_a_take_a_stop_on_the_wrong_side_or_a_smaller_stop_is_not_a_stop(self):
        for orders in ([stop_order(kind="Take Profit Market")], [stop_order(side="B")],
                       [stop_order(sz="0.004", position_tpsl=False)], [stop_order(coin="ETH")],
                       [dict(stop_order(), reduceOnly=False)], [dict(stop_order(), isTrigger=False)]):
            self.logged.clear()
            self.chain.challenges.clear()
            self.active_challenge([("BTC", "0.005")], orders)
            self.make().one_pass()
            self.assertEqual(len(self.missing()), 1, orders)

    def test_a_funded_pool_is_checked_and_a_closing_one_is_not(self):
        self.chain.add_pool(POOL_A, stage=keeper.FUNDED)
        self.chain.logs.append(challenge_log(10, POOL_A))
        self.chain.sizes[POOL_A.lower()] = [("SOL", "-2")]
        self.make().one_pass()
        self.assertEqual(self.missing(), [{"account": POOL_A, "coin": "SOL", "size": "-2"}])
        self.logged.clear()
        self.chain.add_pool(POOL_A, stage=keeper.CLOSING)
        self.make().one_pass()
        self.assertEqual(self.missing(), [])

    def test_an_account_being_stopped_is_not_checked(self):
        self.active_challenge([("BTC", "0.005")], [], violation=1)
        self.make().one_pass()
        self.assertEqual(self.missing(), [])
        self.assertIn("breach", [fn for fn, _ in self.chain.calls_to(CHALLENGE_A)])

    def test_a_failed_read_is_logged_and_the_pass_goes_on(self):
        self.active_challenge([("BTC", "0.005")], [])
        self.chain.front_fails = True
        self.make().one_pass()
        failed = [f for event, f in self.logged if event == "protection_check_failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("429", failed[0]["error"])
        self.assertIn("pass_done", [event for event, _ in self.logged])


if __name__ == "__main__":
    unittest.main()
