"""The agents' gateway client. Offline: HTTP is captured and the chain is a fake.

    spike/.venv/bin/python -m unittest discover -s agents/tests -t .

What matters is that a request the client builds clears the gateway's own checks, and that
its prices and sizes are the ones the browser app would send. The app's vectors are read
from its test file, so a case added there is checked here too.
"""

from __future__ import annotations

import pathlib
import re
import time
import unittest
from unittest import mock

from eth_account import Account

from agents import client
from agents.client import GatewayClient, order_url, round_price, round_size
from gateway.checks import DECIMAL, NonceBook, Request, check

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_TESTS = (ROOT / "app" / "tests" / "lib.test.mjs").read_text()
ACCOUNT = "0x00000000000000000000000000000000000000A1"


class Chain:
    """ACCOUNT is trading with `key`, bound to `trader`, on perp 3 only."""

    def __init__(self, key: str, trader: str):
        self.key, self.trader = key, trader

    def is_account(self, account):
        return account.lower() == ACCOUNT.lower()

    def trading_key(self, account):
        return self.key

    def is_bound(self, key, account, trader):
        return key == self.key and account.lower() == ACCOUNT.lower() and trader.lower() == self.trader.lower()

    def allowed_assets(self, account):
        return {3}


class Response:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = "<html>bad gateway</html>"

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class BodiesClearTheGateway(unittest.TestCase):
    def setUp(self):
        self.trader = Account.create()
        self.chain = Chain(Account.create().address, self.trader.address)
        self.nonces = NonceBook()
        self.sent: list[tuple[str, dict, dict]] = []
        self.reply = Response(200, {"status": "ok"})

        def post(url, json, timeout, headers):
            self.sent.append((url, json, headers))
            return self.reply

        patcher = mock.patch.object(client.requests, "post", side_effect=post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = GatewayClient(self.trader, "https://gateway.test/")

    def cleared(self, body: dict):
        req = Request.from_json(body)
        return req, check(req, self.chain, int(time.time() * 1000), self.nonces)

    def test_order_clears_every_gateway_check(self):
        self.client.order(ACCOUNT.lower(), 3, True, "60000", "0.0002", tif="Alo")
        url, body, _ = self.sent[-1]
        self.assertEqual(url, "https://gateway.test/v1/order")
        req, cleared = self.cleared(body)
        self.assertEqual(cleared.trader, self.trader.address)
        self.assertEqual(req.action(), {
            "type": "order",
            "orders": [{"a": 3, "b": True, "p": "60000", "s": "0.0002", "r": False, "t": {"limit": {"tif": "Alo"}}}],
            "grouping": "na",
        })

    def test_cancel_clears_every_gateway_check(self):
        self.client.cancel(ACCOUNT, 3, 42)
        req, cleared = self.cleared(self.sent[-1][1])
        self.assertEqual(cleared.trader, self.trader.address)
        self.assertEqual(req.action(), {"type": "cancel", "cancels": [{"a": 3, "o": 42}]})

    def test_reduce_only_reaches_the_action(self):
        self.client.order(ACCOUNT, 3, False, "59000", "0.0002", tif="Ioc", reduce_only=True)
        req, _ = self.cleared(self.sent[-1][1])
        order = req.action()["orders"][0]
        self.assertIs(order["r"], True)
        self.assertEqual(order["t"], {"limit": {"tif": "Ioc"}})

    def test_nonces_never_repeat_within_a_millisecond(self):
        with mock.patch.object(client.time, "time", return_value=1_800_000_000.0):
            for _ in range(3):
                self.client.order(ACCOUNT, 3, True, "60000", "0.0002")
        nonces = [body["order"]["nonce"] for _, body, _ in self.sent]
        self.assertEqual(len(set(nonces)), 3)
        self.assertEqual(nonces, sorted(nonces))

    def test_headers_name_the_client_only(self):
        self.client.cancel(ACCOUNT, 3, 42)
        headers = self.sent[-1][2]
        self.assertEqual(headers, {"User-Agent": "colosseum-pools-agent"})

    def test_a_non_json_reply_is_reported(self):
        self.reply = Response(502)
        out = self.client.cancel(ACCOUNT, 3, 42)
        self.assertEqual((out["http"], out["status"]), (502, "bad_response"))


class GatewayUrl(unittest.TestCase):
    def test_https_unless_the_gateway_runs_here(self):
        self.assertEqual(order_url("http://127.0.0.1:8787"), "http://127.0.0.1:8787/v1/order")
        self.assertEqual(order_url("http://localhost:8787/"), "http://localhost:8787/v1/order")
        self.assertEqual(order_url("http://[::1]:8787"), "http://[::1]:8787/v1/order")
        self.assertEqual(order_url("https://gateway.example.org/pools"), "https://gateway.example.org/pools/v1/order")
        for bad in ("http://gateway.example.org", "http://127.0.0.1.example.org", "ftp://127.0.0.1"):
            with self.assertRaises(ValueError, msg=bad):
                order_url(bad)


class NumbersMatchTheApp(unittest.TestCase):
    def cases(self, fn: str) -> list[tuple[float, int, str]]:
        found = re.findall(rf'assert\.equal\({fn}\(([\d.]+), (\d)\), "([\d.]+)"\)', APP_TESTS)
        return [(float(v), int(d), want) for v, d, want in found]

    def refused(self, fn: str) -> list[tuple[float, int]]:
        found = re.findall(rf"assert\.throws\(\(\) => {fn}\(([\d.]+), (\d)\)", APP_TESTS)
        return [(float(v), int(d)) for v, d in found]

    def test_prices(self):
        cases = self.cases("roundPrice")
        self.assertGreaterEqual(len(cases), 9, "the app's price vectors were not found")
        for px, szd, want in cases:
            got = round_price(px, szd)
            self.assertEqual(got, want, (px, szd))
            self.assertRegex(got, DECIMAL)

    def test_sizes(self):
        cases = self.cases("roundSize")
        self.assertGreaterEqual(len(cases), 4, "the app's size vectors were not found")
        for sz, szd, want in cases:
            got = round_size(sz, szd)
            self.assertEqual(got, want, (sz, szd))
            self.assertRegex(got, DECIMAL)

    def test_refusals(self):
        refused = self.refused("roundPrice") + self.refused("roundSize")
        self.assertGreaterEqual(len(refused), 2, "the app's refusal cases were not found")
        for fn, cases in ((round_price, self.refused("roundPrice")), (round_size, self.refused("roundSize"))):
            for v, d in cases:
                with self.assertRaises(ValueError, msg=(fn.__name__, v, d)):
                    fn(v, d)


if __name__ == "__main__":
    unittest.main()
