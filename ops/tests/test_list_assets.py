import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from ops import list_assets as la

FACTORY = "0x00000000000000000000000000000000000000Fa"
SIGNER = "0x00000000000000000000000000000000000000De"
# perpDexs: the main dex (null), then builder dexes; xyz at index 2 here, so its first market is 120000.
DEXES = [None, {"name": "flx"}, {"name": "xyz"}, {"name": "usdh"}]
METAS = {
    "": {"universe": [{"name": "SOL"}, {"name": "OLD", "isDelisted": True}, {"name": "BTC"}]},
    "xyz": {"collateralToken": 0, "universe": [{"name": "xyz:XYZ100"}, {"name": "xyz:CL"}]},
    "usdh": {"collateralToken": 360, "universe": [{"name": "usdh:GOLD"}]},
    "flx": {"collateralToken": 0, "universe": [{"name": "flx:TSLA"}]},
}


class ListAssets(unittest.TestCase):
    """ops/list_assets.py (the CTO's 1/7/9): markets by their order id, a builder dex only if trusted
    and on USDC collateral (H-2, H-3), the factory asked before anything is sent, a dry run by default."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        (root / "deployments").mkdir()
        (root / "deployments" / "testnet-v3-demo3.json").write_text(json.dumps({"PoolFactory": FACTORY}))
        self.listed: set[int] = {2}
        self.sent: list = []
        self.refuse: Exception | None = None

        def info_post(body):
            if body["type"] == "perpDexs":
                return DEXES
            return METAS[body.get("dex", "")]

        def call_view(to, sig, types, args, out):
            return (args[0] in self.listed,)

        def rpc(method, params):
            if self.refuse:
                raise self.refuse
            return "0x"

        def transact(acct, to, sig, types, values):
            self.sent.append((sig, values))
            ids, _, allowed = values
            self.listed = (self.listed | set(ids)) if allowed else (self.listed - set(ids))
            return {"transactionHash": "0x01"}

        for name, fn in (("info_post", info_post), ("call_view", call_view), ("rpc", rpc), ("transact", transact)):
            p = mock.patch.object(la.c, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.object(la, "ROOT", root), mock.patch.object(la.c, "assert_testnet"),
                  mock.patch.object(la.c, "address_of", return_value=SIGNER), mock.patch.object(la.c, "account")):
            p.start()
            self.addCleanup(p.stop)

    def run_main(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = la.main(["--deployment", "v3-demo3", *argv])
        return code, out.getvalue()

    def test_ids_are_the_order_ids(self):
        markets, problems = la.resolve(["SOL", "BTC", "xyz:CL"], la.TRUSTED_DEXES, True)
        self.assertEqual(problems, [])
        self.assertEqual([(m["name"], m["id"]) for m in markets], [("SOL", 0), ("BTC", 2), ("xyz:CL", 120_001)])

    def test_a_dry_run_asks_the_factory_and_sends_nothing(self):
        code, out = self.run_main("--list", "platform", "--assets", "BTC,xyz:XYZ100")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [])
        self.assertIn("dry run: nothing was sent", out)
        self.assertIn("BTC (2): already listed", out)
        self.assertIn("setPlatformAssets([120000], ['xyz:XYZ100'], True)", out)
        la.c.rpc.assert_called_once()

    def test_send_lists_and_reads_it_back(self):
        code, out = self.run_main("--list", "funded", "--assets", "xyz:XYZ100,SOL", "--send")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [("setFundedAssets(uint32[],string[],bool)", [[120_000, 0], ["xyz:XYZ100", "SOL"], True])])
        self.assertIn("✓ xyz:XYZ100 (120000): isFundedAsset = True", out)

    def test_delisting(self):
        code, _ = self.run_main("--list", "platform", "--assets", "BTC", "--delist", "--send")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent[0][1][2], False)
        self.assertNotIn(2, self.listed)

    def test_h3_a_dex_not_trusted_is_refused_unless_named(self):
        code, out = self.run_main("--list", "platform", "--assets", "flx:TSLA", "--send")
        self.assertEqual((code, self.sent), (1, []))
        self.assertIn("dex flx is not one this run trusts (xyz, mkts)", out)
        code, _ = self.run_main("--list", "platform", "--assets", "flx:TSLA", "--trust", "flx", "--send")
        self.assertEqual(code, 0)
        self.assertEqual(self.sent[0][1][0], [110_000])

    def test_h2_a_dex_not_on_usdc_is_refused_even_when_trusted(self):
        code, out = self.run_main("--list", "platform", "--assets", "usdh:GOLD", "--trust", "usdh", "--send")
        self.assertEqual((code, self.sent), (1, []))
        self.assertIn("dex usdh's collateral is token 360, not USDC", out)

    def test_a_market_not_there_or_delisted_is_refused(self):
        code, out = self.run_main("--list", "platform", "--assets", "xyz:TSLA,OLD,abc:X", "--send")
        self.assertEqual((code, self.sent), (1, []))
        self.assertIn("xyz:TSLA: not in dex xyz's meta", out)
        self.assertIn("OLD: delisted on Hyperliquid", out)
        self.assertIn("abc:X: dex abc is not one this run trusts", out)

    def test_what_the_factory_refuses_is_named_and_nothing_is_sent(self):
        sel = next(s for s, n in la.BY_SELECTOR.items() if n == "NotThatMarket")
        self.refuse = RuntimeError(f"execution reverted: {{'data': '{sel}0000'}}")
        code, out = self.run_main("--list", "platform", "--assets", "xyz:XYZ100", "--send")
        self.assertEqual((code, self.sent), (1, []))
        self.assertIn("the factory refuses: NotThatMarket", out)

    def test_nothing_to_do(self):
        code, out = self.run_main("--list", "platform", "--assets", "BTC", "--send")
        self.assertEqual((code, self.sent), (0, []))
        self.assertIn("nothing to do", out)


if __name__ == "__main__":
    unittest.main()
