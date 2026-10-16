"""ops/contract_errors.py: every error the contracts declare, named, and a refusal read back as one line."""
import pathlib
import re
import unittest

from eth_abi import encode
from eth_utils import keccak

from ops import contract_errors as e

ROOT = pathlib.Path(__file__).resolve().parents[2]


def declared(text: str) -> list:
    """`error Name(Type a, ...)` in ABI form: a type named with a capital is one of the contracts'
    enums, a uint8."""
    found = []
    for name, params in re.findall(r"^\s*error (\w+)\(([^)]*)\);", text, re.M):
        types = [p.strip().split()[0] for p in params.split(",") if p.strip()]
        found.append(f"{name}({','.join('uint8' if t[0].isupper() else t for t in types)})")
    return found


def selector(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


class TheList(unittest.TestCase):
    def test_it_is_every_error_the_contracts_declare(self):
        source = []
        for f in e.SOURCES:
            text = (ROOT / f).read_text()
            self.assertEqual(re.findall(r"^\s*error \w+\([^)]*$", text, re.M), [], f"{f}: an error over two lines")
            source += [s for s in declared(text) if s not in source]
        self.assertEqual(e.ERRORS, source)

    def test_no_two_errors_share_a_selector(self):
        everything = e.ERRORS + e.OPENZEPPELIN + e.BUILTIN
        self.assertEqual(len(everything), len(set(everything)))
        self.assertEqual(len(e.BY_SELECTOR), len(everything))


class Reading(unittest.TestCase):
    def test_a_name_with_its_arguments(self):
        self.assertEqual(e.name(selector("Ending()")), "Ending()")
        data = selector("StillTrading(uint256)") + encode(["uint256"], [1_790_000_000]).hex()
        self.assertEqual(e.name(data), "StillTrading(1790000000)")
        data = selector("Error(string)") + encode(["string"], ["no"]).hex()
        self.assertEqual(e.name(data), "Error('no')")
        self.assertEqual(e.name(selector("StillTrading(uint256)")), "StillTrading(uint256)", "data cut short")
        self.assertEqual(e.name("0xdeadbeef"), "0xdeadbeef", "not one of ours")

    def test_a_refusal_from_the_nodes_error(self):
        locked = selector("Locked()")
        self.assertEqual(e.refusal(RuntimeError(f"eth_estimateGas: {{'code': 3, 'message': 'execution reverted', 'data': '{locked}'}}")),
                         "Locked()")
        until = selector("StillTrading(uint256)") + encode(["uint256"], [1_790_000_000]).hex()
        self.assertEqual(e.refusal(RuntimeError(f"eth_estimateGas: {{'code': 3, 'data': '{until}'}}")),
                         "StillTrading(1790000000)", "the arguments come with the data")
        # No data field, and an address before the selector: the address's first digits are skipped.
        msg = f"eth_call to 0x1234567890abcdef1234567890abcdef12345678 reverted: custom error {locked}"
        self.assertEqual(e.refusal(RuntimeError(msg)), "Locked()")
        self.assertEqual(e.refusal(RuntimeError("eth_estimateGas:   rate limited\n 6 times")),
                         "eth_estimateGas: rate limited 6 times")


if __name__ == "__main__":
    unittest.main()
