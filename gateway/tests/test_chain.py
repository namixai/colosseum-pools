"""The gateway's chain reads when the RPC throttles. Offline: the HTTP session is a fake.

    spike/.venv/bin/python -m unittest discover -s gateway/tests -t .
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

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


class NoKeyOfTheNodeInALine(unittest.TestCase):
    """A node's address may hold its key, in its path or in its host name: `quiet` takes it out of an error's words
    before they reach a log line of the gateway or of the index."""

    def test_a_url_a_bare_path_and_a_named_host_are_taken_out(self):
        url = "https://rpc.example.com/v2/SECRETKEY123"
        for text, said in ((f"500 Server Error: Internal Server Error for url: {url}",
                            "500 Server Error: Internal Server Error for url: <node>"),
                           (f"Max retries exceeded with url: /v2/SECRETKEY123 (Caused by {url})",
                            "Max retries exceeded with url: <path> (Caused by <node>)"),
                           ("HTTPSConnectionPool(host='SECRETKEY123.provider.example', port=443): Read timed out.",
                            "HTTPSConnectionPool(host='<host>', port=443): Read timed out."),
                           ("eth_call: rate limited 5 times in a row", "eth_call: rate limited 5 times in a row")):
            self.assertEqual(chain.quiet(requests.HTTPError(text)), said)

    def test_the_configured_nodes_go_whole_the_longest_first_and_their_host_names_wherever_they_stand(self):
        short, long = "https://rpc.example.org", "https://rpc.example.org/SECRETKEY123"
        for gateway, index in ((short, long), (long, short)):
            with mock.patch.dict(os.environ, {"GATEWAY_RPC_URL": gateway, "SIM_INDEX_RPC_URL": index}):
                got = chain.quiet(RuntimeError(f"both: {short} and {long} and again {long}"), 400)
                self.assertEqual(got, "both: <node> and <node> and again <node>")
        keyed = "https://SECRETKEY123.provider.example/v2"
        for name in chain.NODE_SETTINGS:
            with mock.patch.dict(os.environ, {name: keyed}):
                got = chain.quiet(RuntimeError("Failed to resolve 'secretkey123.provider.example' ([Errno 8] nodename nor servname)"))
                self.assertEqual(got, "Failed to resolve '<host>' ([Errno 8] nodename nor servname)", name)

    def test_an_address_that_goes_on_past_a_configured_one_goes_whole(self):
        """The address an error names may begin with a configured one and go on: the same node at a longer path, a
        redirect, another node of the provider that is not among those configured. Taken out only as far as the
        configured one reaches, it would leave "<node>/v2/KEY", a text no later rule knows for an address."""
        for name in chain.NODE_SETTINGS:
            with mock.patch.dict(os.environ, {name: "https://rpc.example.org"}):
                for text, said in (("500 Server Error for url: https://rpc.example.org/v2/SECRETKEY123",
                                    "500 Server Error for url: <node>"),
                                   ("sent on to https://rpc.example.org/v2/SECRETKEY123?id=1 (twice)", "sent on to <node> (twice)"),
                                   ("down: https://rpc.example.org", "down: <node>")):
                    self.assertEqual(chain.quiet(RuntimeError(text)), said, name)

    def test_the_words_are_cut_after_the_address_is_gone_never_before(self):
        """Cut first, a host name that is the node's key stands there in part, `host='KEY.prov`, with no end for any
        rule to know it by."""
        for lead in range(150, 200):
            text = "x" * lead + " HTTPSConnectionPool(host='SECRETKEY123.provider.example', port=443): Max retries exceeded"
            got = chain.quiet(RuntimeError(text))
            self.assertNotIn("SECRETKEY123"[:4], got, lead)
            self.assertLessEqual(len(got), 200)
        self.assertEqual(len(chain.quiet(RuntimeError("y" * 500), None)), 500)


if __name__ == "__main__":
    unittest.main()
