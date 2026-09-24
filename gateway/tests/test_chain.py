"""The gateway's chain reads when the RPC throttles. Offline: the HTTP session is a fake.

    spike/.venv/bin/python -m unittest discover -s gateway/tests -t .
"""

from __future__ import annotations

import unittest

import requests

from gateway import chain

FACTORY = "0x00000000000000000000000000000000000000F1"
REGISTRY = "0x00000000000000000000000000000000000000F2"


class Reply:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


class Session:
    def __init__(self, replies):
        self.replies, self.posts = list(replies), 0

    def post(self, url, json=None, timeout=None):
        self.posts += 1
        return self.replies.pop(0)


THROTTLED = Reply(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}})
OK = Reply(200, {"jsonrpc": "2.0", "id": 1, "result": "0x3e6"})


class Throttling(unittest.TestCase):
    def reader(self, replies):
        self.slept = []
        r = chain.JsonRpcReader("http://rpc.invalid", FACTORY, REGISTRY, sleep=self.slept.append)
        r._session = Session(replies)
        return r

    def test_a_throttled_read_is_retried_briefly(self):
        r = self.reader([Reply(429), THROTTLED, OK])
        self.assertEqual(r._rpc("eth_chainId", []), "0x3e6")
        self.assertEqual((r._session.posts, self.slept), (3, [0.25, 0.5]))

    def test_it_gives_up_in_the_end_and_says_it_was_throttled(self):
        """Five tries over about four seconds. Three inside one second was what the node
        refused on 24 Sep 2026 while the same reads went through moments later, and the trader
        was told the gateway was broken. The type is its own: waiting helps, so the caller can
        be told to wait rather than that something failed."""
        r = self.reader([THROTTLED, Reply(429), THROTTLED, Reply(429), THROTTLED])
        with self.assertRaisesRegex(chain.Throttled, "rate limited 5 times"):
            r._rpc("eth_call", [])
        self.assertEqual((r._session.posts, self.slept), (5, [0.25, 0.5, 1.0, 2.0]))

    def test_any_other_error_is_raised_at_once(self):
        r = self.reader([Reply(200, {"error": {"code": -32000, "message": "execution reverted"}})])
        with self.assertRaisesRegex(RuntimeError, "execution reverted"):
            r._rpc("eth_call", [])
        r = self.reader([Reply(502)])
        with self.assertRaises(requests.HTTPError):
            r._rpc("eth_call", [])
        self.assertEqual((r._session.posts, self.slept), (1, []))


if __name__ == "__main__":
    unittest.main()
