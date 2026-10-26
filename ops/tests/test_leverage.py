import unittest

from ops import leverage as lv


class AssetCaps(unittest.TestCase):
    """Each asset's own leverage cap by default (the CTO, 21 October 2026), at most the account's."""

    def test_the_defaults(self):
        names = ["BTC", "ETH", "SOL", "HYPE", "xyz:SP500", "xyz:XYZ100", "xyz:CL", "xyz:BRENTOIL"]
        self.assertEqual(lv.asset_caps(names, 1000), [1000, 1000, 500, 500, 1000, 800, 300, 200])
        self.assertEqual(lv.asset_caps(names, 500), [500, 500, 500, 500, 500, 500, 300, 200])

    def test_an_asset_with_no_default_takes_the_accounts_cap(self):
        self.assertEqual(lv.asset_caps(["DOGE", "xyz:TSLA"], 400), [400, 400])

    def test_one_named_by_its_name_or_its_coin(self):
        self.assertEqual(lv.asset_caps(["xyz:CL", "BTC"], 1000, {"CL": 150, "BTC": 700}), [150, 700])
        self.assertEqual(lv.asset_caps(["xyz:CL"], 1000, {"xyz:CL": 120}), [120])

    def test_what_the_contracts_refuse(self):
        self.assertEqual(lv.bad_caps(["BTC", "ETH", "SOL"], [99, 100, 501], 500), ["BTC", "SOL"])

    def test_as_the_terms_say_it(self):
        self.assertEqual(lv.caps_text(["BTC", "xyz:CL"], [500, 250]), "BTC 5x, xyz:CL 2.5x")


if __name__ == "__main__":
    unittest.main()
