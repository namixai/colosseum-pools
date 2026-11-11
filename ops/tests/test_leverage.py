import unittest
from unittest import mock

from ops import leverage as lv


class AssetCaps(unittest.TestCase):
    """Each asset's own leverage cap by default (the pools team, 21 October 2026), at most the account's."""

    def test_the_defaults(self):
        names = ["BTC", "ETH", "SOL", "HYPE", "xyz:SP500", "xyz:XYZ100", "xyz:CL", "xyz:GOLD"]
        self.assertEqual(lv.asset_caps(names, 1000), [1000, 1000, 500, 1000, 1000, 800, 300, 1000])
        self.assertEqual(lv.asset_caps(names, 500), [500, 500, 500, 500, 500, 500, 300, 500])

    def test_the_leverage_rule_v1_2_for_the_four_main_perps(self):
        # BTC 10x, ETH 10x, SOL 5x, HYPE 10x; HYPE's 10x is the most Hyperliquid lends on it (its meta's
        # maxLeverage, mainnet and testnet alike, read on 9 November 2026).
        self.assertEqual({k: lv.ASSET_LEVERAGE_X100[k] for k in ("BTC", "ETH", "SOL", "HYPE")},
                         {"BTC": 1000, "ETH": 1000, "SOL": 500, "HYPE": 1000})

    def test_an_asset_with_no_default_takes_the_accounts_cap(self):
        self.assertEqual(lv.asset_caps(["DOGE", "xyz:TSLA"], 400), [400, 400])

    def test_an_asset_whose_cap_is_still_to_come_is_refused_not_given_the_accounts_cap(self):
        # A market named in the table with no number yet is waiting for its listing: a pool that
        # names it is refused outright, whatever its plan or command line would choose.
        with mock.patch.dict(lv.ASSET_LEVERAGE_X100, {"NEXT": None}):
            for chosen in (None, {"xyz:NEXT": 300}, {"NEXT": 300}):
                with self.subTest(chosen=chosen), self.assertRaisesRegex(ValueError, "gives it no leverage cap"):
                    lv.asset_caps(["BTC", "xyz:NEXT"], 1000, chosen)

    def test_brentoil_is_withdrawn_and_a_pool_that_names_it_is_refused(self):
        # The leverage rule v1.3 (2 November 2026) withdrew xyz:BRENTOIL: no cap from the platform, so no
        # default, and no plan or command line can give it one either.
        self.assertIsNone(lv.ASSET_LEVERAGE_X100["BRENTOIL"])
        for chosen in (None, {"xyz:BRENTOIL": 200}, {"BRENTOIL": 200}):
            with self.subTest(chosen=chosen), self.assertRaisesRegex(ValueError, "^xyz:BRENTOIL: the platform gives it no leverage cap"):
                lv.asset_caps(["xyz:CL", "xyz:BRENTOIL"], 1000, chosen)

    def test_gold_is_20x_by_the_leverage_rule_v1_3(self):
        # The leverage rule v1.3 (2 November 2026) for xyz:GOLD: 20x, under the 25x Hyperliquid lends on
        # it (its meta's maxLeverage, mainnet and testnet alike, read on 10 November 2026). At most the
        # account's cap, as every default.
        self.assertEqual(lv.ASSET_LEVERAGE_X100["GOLD"], 2000)
        self.assertEqual(lv.asset_caps(["xyz:GOLD"], 2500), [2000])
        self.assertEqual(lv.asset_caps(["xyz:GOLD"], 1000), [1000])
        self.assertEqual(lv.caps_text(["xyz:GOLD"], lv.asset_caps(["xyz:GOLD"], 2500)), "xyz:GOLD 20x")

    def test_silver_is_15x_by_the_leverage_rule_v1_3(self):
        # The leverage rule v1.3 (2 November 2026) for xyz:SILVER: 15x, under the 25x Hyperliquid lends on
        # it (its meta's maxLeverage, mainnet and testnet alike, read on 11 November 2026). Before it was in
        # the table it took the account's cap: 20x on a 20x account. At most the account's cap.
        self.assertEqual(lv.ASSET_LEVERAGE_X100["SILVER"], 1500)
        self.assertEqual(lv.asset_caps(["xyz:SILVER"], 2000), [1500])
        self.assertEqual(lv.asset_caps(["xyz:SILVER"], 1000), [1000])
        self.assertEqual(lv.caps_text(["xyz:SILVER"], lv.asset_caps(["xyz:SILVER"], 2500)), "xyz:SILVER 15x")

    def test_one_named_by_its_name_or_its_coin(self):
        self.assertEqual(lv.asset_caps(["xyz:CL", "BTC"], 1000, {"CL": 150, "BTC": 700}), [150, 700])
        self.assertEqual(lv.asset_caps(["xyz:CL"], 1000, {"xyz:CL": 120}), [120])

    def test_what_the_contracts_refuse(self):
        self.assertEqual(lv.bad_caps(["BTC", "ETH", "SOL"], [99, 100, 501], 500), ["BTC", "SOL"])

    def test_as_the_terms_say_it(self):
        self.assertEqual(lv.caps_text(["BTC", "xyz:CL"], [500, 250]), "BTC 5x, xyz:CL 2.5x")


if __name__ == "__main__":
    unittest.main()
