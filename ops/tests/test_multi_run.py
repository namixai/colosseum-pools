"""ops/multi_run.py: the parts that turn what a person types into what a contract takes, and back."""
import pathlib
import re
import unittest

from eth_utils import keccak

from ops import multi_run as r

ROOT = pathlib.Path(__file__).resolve().parents[2]
MULTI = (ROOT / "src/multi/MultiPool.sol").read_text()
EXAM = (ROOT / "src/exam/PaperExam.sol").read_text()
POOL = (ROOT / "src/Pool.sol").read_text()
FREE = (ROOT / "src/multi/FreeExams.sol").read_text()


def enum_names(text: str, name: str) -> tuple:
    body = re.search(r"enum " + name + r" \{(.*?)\}", text, re.S).group(1)
    lines = [l.strip().rstrip(",") for l in body.split("\n")]
    return tuple(l for l in lines if l and not l.startswith("//"))


def struct_types(text: str, name: str, enums: tuple) -> list:
    body = re.search(r"struct " + name + r" \{(.*?)\n    \}", text, re.S).group(1)
    fields = [l.strip() for l in body.split("\n") if l.strip().endswith(";")]
    return ["uint8" if f.split(" ")[0] in enums else f.split(" ")[0] for f in fields]


class Copies(unittest.TestCase):
    def test_names_follow_the_contracts_enums(self):
        self.assertEqual(r.EXAM_STATUS, enum_names(EXAM, "Status"))
        self.assertEqual(r.SEAT_STATE, enum_names(MULTI, "SeatState"))
        self.assertEqual(r.STAGE, enum_names(POOL, "Stage"))

    def test_seat_info_is_the_struct_the_contract_declares(self):
        self.assertEqual(r.SEAT_INFO, struct_types(MULTI, "SeatInfo", ("SeatState",)))

    def test_event_topics_are_the_events_the_contract_emits(self):
        self.assertIn("event ExamSold(address indexed exam, address indexed trader, uint256 line, uint64 size, uint64 price);", MULTI)
        self.assertIn("event Claimed(address indexed exam, address indexed trader, address indexed seat);", MULTI)
        self.assertEqual(r.EXAM_SOLD, "0x" + keccak(text="ExamSold(address,address,uint256,uint64,uint64)").hex())
        self.assertEqual(r.CLAIMED, "0x" + keccak(text="Claimed(address,address,address)").hex())

    def test_the_calls_it_sends_exist(self):
        for sig in ("function buy(uint256 line)", "function claim(address exam)", "function open(address seat)",
                    "function rearm(address seat)", "function finish(address seat)", "function sweep(address seat)",
                    "function release(address exam)", "function withdraw(uint64 amount1e8)",
                    "function noteActivity(address seat)",
                    "function closeIdle(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)",
                    "function withdrawEvm(uint256 amount)", "function extend(uint32 term, uint64 out1e8, uint256 outEvm)",
                    "function endSeat(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)",
                    "function hitTarget(address seat, Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)",
                    "function sellFree(address trader, uint8 line)", "function noteTrade(address seat)"):
            self.assertIn(sig, MULTI)
        for sig in ("function place(uint32 asset, bool isBuy, uint64 sz)", "function fill()", "function graduate()"):
            self.assertIn(sig, EXAM)
        self.assertIn("function stopFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets, bytes32 salt)", POOL)
        self.assertIn("function settleFunded(Cancel[] calldata cancels, uint32[] calldata extraAssets)", POOL)
        for sig in ("function take(uint8 line, uint64 nonce, uint64 expiresAt, uint8 v, bytes32 r, bytes32 s)",
                    "function withdraw(uint64 nonce)", "function inviter()", "address public immutable inviter;"):
            self.assertTrue(sig in FREE or sig.replace("function inviter()", "inviter") in FREE, sig)


class ThePlanItKeeps(unittest.TestCase):
    def test_the_plan_is_read_back_from_its_code(self):
        from eth_abi import encode
        from ops import deploy_multi as d
        import json
        plan = d.plan_of(json.loads((ROOT / "ops/plans/multi-testnet-run.json").read_text()),
                         d.bounds_of(0.125, 1, 0.0104166667), (b"\x33" * 32, "0x" + "44" * 20))
        code = b"\x00" + encode([d.PLAN_T], [plan])
        back = r.decode_plan(code)
        self.assertEqual(back[16], b"\x33" * 32)
        self.assertEqual(back[8], plan[8])
        with self.assertRaises(SystemExit):
            r.decode_plan(b"\x60" + code[1:])

    def test_the_owners_steps(self):
        p = r.build_parser()
        self.assertEqual(p.parse_args(["--label", "m", "begin", "--who", "op"]).cmd, "begin")
        a = p.parse_args(["--label", "m", "stopterm", "--who", "op", "--undo"])
        self.assertEqual((a.cmd, a.undo), ("stopterm", True))
        for sig in ("function begin(bytes32 terms_)", "function setStop(bool stop)", "address public immutable planData;"):
            self.assertIn(sig, MULTI)


class Invitations(unittest.TestCase):
    def test_an_invitation_is_the_typed_data_any_wallet_signs(self):
        # The digest put together by hand is EIP-712's, so an invitation signed here and one signed
        # in a wallet are the same signature, and the contract builds the same digest.
        from eth_account import Account
        key = Account.create()
        desk, trader = "0x" + "11" * 20, "0x" + "22" * 20
        by_hand = key.unsafe_sign_hash(r.invite_digest(desk, trader, 2, 7, 1_800_000_000))
        typed = Account.sign_typed_data(key.key, full_message={
            "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                       {"name": "chainId", "type": "uint256"},
                                       {"name": "verifyingContract", "type": "address"}],
                      "Invite": [{"name": "trader", "type": "address"}, {"name": "line", "type": "uint8"},
                                 {"name": "nonce", "type": "uint64"}, {"name": "expiresAt", "type": "uint64"}]},
            "primaryType": "Invite",
            "domain": {"name": "FreeExams", "version": "1", "chainId": 998, "verifyingContract": desk},
            "message": {"trader": trader, "line": 2, "nonce": 7, "expiresAt": 1_800_000_000}})
        self.assertEqual((by_hand.v, by_hand.r, by_hand.s), (typed.v, typed.r, typed.s))

    def test_the_type_strings_are_the_contracts(self):
        self.assertIn('keccak256("Invite(address trader,uint8 line,uint64 nonce,uint64 expiresAt)")', FREE)
        self.assertIn('keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")', FREE)
        self.assertIn('keccak256("FreeExams"), keccak256("1")', FREE)

    def test_the_steps_take_what_an_invitation_needs(self):
        p = r.build_parser()
        a = p.parse_args(["--label", "m", "invite", "--who", "op", "--trader", "0xT", "--line", "1", "--nonce", "3",
                          "--days", "7"])
        self.assertEqual((a.cmd, a.trader, a.line, a.nonce, a.days), ("invite", "0xT", 1, 3, 7.0))
        a = p.parse_args(["--label", "m", "free", "--who", "t", "--invite", "{}"])
        self.assertEqual((a.cmd, a.invite), ("free", "{}"))
        with self.assertRaises(SystemExit):
            p.parse_args(["--label", "m", "invite", "--who", "op", "--trader", "0xT", "--line", "1"])


class Numbers(unittest.TestCase):
    def test_a_size_is_whole_steps_of_the_asset(self):
        self.assertEqual(r.size_units(0.0002, 5), 20)
        self.assertEqual(r.size_units(0.04, 4), 400)
        self.assertEqual(r.size_units(1, 0), 1)
        for size, decimals in ((0.000001, 5), (0.00025, 4), (0, 5), (-0.001, 5)):
            with self.assertRaises(SystemExit):
                r.size_units(size, decimals)

    def test_a_mark_carries_six_minus_the_size_decimals(self):
        self.assertEqual(r.mark_usd(764000, 5), 76400.0)
        self.assertEqual(r.mark_usd(241370, 4), 2413.70)
        self.assertEqual(r.mark_usd(150_123_400, 0), 150.1234)

    def test_days_become_the_contracts_seconds(self):
        self.assertEqual(r.seconds(0.0416666667), 3_600)
        self.assertEqual(r.seconds(180), 15_552_000)

    def test_the_gap_is_the_contracts_mark_over_the_exchanges(self):
        self.assertAlmostEqual(r.gap_bps(100.5, 100.0), 50.0)
        self.assertAlmostEqual(r.gap_bps(99.0, 100.0), -100.0)
        self.assertEqual(r.gap_bps(100.0, 100.0), 0.0)


class Receipts(unittest.TestCase):
    EXAM_ADDR = "0x00000000000000000000000000000000000000Aa"
    SEAT_ADDR = "0x00000000000000000000000000000000000000bB"

    def topic(self, addr: str) -> str:
        return "0x" + "0" * 24 + addr[2:].lower()

    def test_the_exam_and_the_seat_are_read_from_the_event(self):
        receipt = {"transactionHash": "0x1", "logs": [
            {"topics": ["0xfeed", self.topic(self.SEAT_ADDR)]},
            {"topics": [r.EXAM_SOLD, self.topic(self.EXAM_ADDR), self.topic(self.SEAT_ADDR)]},
            {"topics": [r.CLAIMED, self.topic(self.EXAM_ADDR), self.topic(self.EXAM_ADDR), self.topic(self.SEAT_ADDR)]},
        ]}
        self.assertEqual(r.indexed(receipt, r.EXAM_SOLD, 1).lower(), self.EXAM_ADDR.lower())
        self.assertEqual(r.indexed(receipt, r.CLAIMED, 3).lower(), self.SEAT_ADDR.lower())

    def test_no_event_or_two_is_a_refusal_not_a_guess(self):
        one = {"topics": [r.EXAM_SOLD, self.topic(self.EXAM_ADDR), self.topic(self.SEAT_ADDR)]}
        for logs in ([], [one, one]):
            with self.assertRaises(SystemExit):
                r.indexed({"transactionHash": "0x1", "logs": logs}, r.EXAM_SOLD, 1)


class Parser(unittest.TestCase):
    def test_every_step_names_who_signs_and_what_it_acts_on(self):
        p = r.build_parser()
        a = p.parse_args(["--label", "multi-1", "place", "--who", "trader", "--exam", "0xE", "--asset", "BTC",
                          "--side", "sell", "--size", "0.0002"])
        self.assertEqual((a.cmd, a.who, a.exam, a.asset, a.side, a.size, a.dry_run), ("place", "trader", "0xE", "BTC", "sell", 0.0002, False))
        a = p.parse_args(["--label", "multi-1", "--dry-run", "finish", "--who", "trader", "--seat", "0xS"])
        self.assertEqual((a.cmd, a.seat, a.dry_run), ("finish", "0xS", True))
        a = p.parse_args(["--label", "multi-1", "extend", "--who", "op", "--term-days", "0.5", "--usdc", "10"])
        self.assertEqual((a.cmd, a.term_days, a.usdc, a.usdc_evm), ("extend", 0.5, 10.0, 0.0))
        for bad in (["--label", "x", "buy", "--line", "0"],  # nobody signs
                    ["--label", "x", "open", "--who", "t"],  # no seat
                    ["--label", "x", "place", "--who", "t", "--exam", "0xE", "--asset", "DOGE", "--side", "buy", "--size", "1"],
                    ["status"]):  # no deployment named
            with self.assertRaises(SystemExit):
                p.parse_args(bad)

    def test_every_step_the_parser_takes_is_a_step_the_run_has(self):
        p = r.build_parser()
        steps = next(a for a in p._actions if a.dest == "cmd").choices
        for name in steps:
            self.assertTrue(callable(getattr(r.Run, name, None)), name)



class ExamReads(unittest.TestCase):
    def test_the_terms_and_the_order_are_the_structs_the_exam_declares(self):
        body = re.search(r"struct ExamTerms \{(.*?)\n\}", EXAM, re.S).group(1)
        types = [l.strip().split(" ")[0] for l in body.split("\n") if l.strip().endswith(";")]
        self.assertEqual(r.EXAM_TERMS, ["(" + ",".join(types) + ")"])
        self.assertEqual(r.PENDING, struct_types(EXAM, "Order", ()))
        for sig in ("function expire()", "function terms() external view returns (ExamTerms memory)",
                    "function notional() external view", "Order public pending;",
                    "mapping(uint32 asset => int256) public positionOf;"):
            self.assertIn(sig, EXAM)

    def test_the_target_and_the_cost_are_worked_out_as_the_exam_does(self):
        self.assertIn("int256 target = capital + (capital * int256(uint256(_terms.targetsBps[step]))) / int256(uint256(Units.BPS));", EXAM)
        self.assertIn("uint256 cost = (ntl * _terms.costPpm + PPM - 1) / PPM;", EXAM)
        self.assertEqual(r.target_equity(5_000_000, 10), 5_005_000)
        self.assertEqual(r.target_equity(5_000_001, 10), 5_005_001)  # 5000.001 of profit, rounded down
        self.assertEqual(r.close_cost(20_000_000, 100), 2_000)
        self.assertEqual(r.close_cost(20_000_001, 100), 2_001)  # rounded up, as the exam charges it
        self.assertEqual(r.close_cost(0, 100), 0)


class Autoclose(unittest.TestCase):
    # A 5 USDC exam, a 0.1 % target, 20 USDC of BTC open, a fill at 100 millionths: 5.005 to pass,
    # 0.002 to close.
    T, NTL, PPM = 5_005_000, 20_000_000, 100

    def move(self, equity, position=-26, asset_ntl=NTL, total_ntl=NTL, cushion=0):
        return r.autoclose_move(equity, self.T, position, asset_ntl, total_ntl, self.PPM, cushion)

    def test_it_closes_only_once_the_target_holds_after_the_closing_fill(self):
        self.assertEqual(self.move(5_006_999), ("wait",))
        self.assertEqual(self.move(5_007_000), ("close", True, 26))  # a short is bought back
        self.assertEqual(self.move(5_007_000, position=26), ("close", False, 26))  # a long is sold
        self.assertEqual(self.move(5_007_999, cushion=1_000), ("wait",))
        self.assertEqual(self.move(5_008_000, cushion=1_000), ("close", True, 26))

    def test_flat_it_passes_the_step_or_says_why_not(self):
        self.assertEqual(self.move(5_005_000, position=0, asset_ntl=0, total_ntl=0), ("graduate",))
        stop = self.move(5_004_999, position=0, asset_ntl=0, total_ntl=0)
        self.assertEqual(stop[0], "stop")
        self.assertIn("under the target 5.005000", stop[1])

    def test_another_asset_open_is_left_alone(self):
        self.assertEqual(self.move(6_000_000, total_ntl=self.NTL + 1)[0], "stop")
        self.assertEqual(self.move(6_000_000, position=0, asset_ntl=0, total_ntl=1)[0], "stop")

    def test_the_steps_parse(self):
        p = r.build_parser()
        a = p.parse_args(["--label", "m", "autoclose", "--who", "t", "--exam", "0xE", "--asset", "BTC"])
        self.assertEqual((a.cmd, a.cushion, a.minutes, a.every), ("autoclose", 0.0, 60.0, 3.0))
        a = p.parse_args(["--label", "m", "expire", "--who", "t", "--exam", "0xE"])
        self.assertEqual((a.cmd, a.exam), ("expire", "0xE"))


class FakeExam:
    """A paper exam as autoclose reads it: one asset, a mark that is a function of the block, and
    each sleep one block later. Units as the contract's: 1e6 = 1 USDC, sizes in the asset's steps."""

    def __init__(self, position, mark):
        self.block, self.mark, self.position = 1, mark, position
        self.cash = 5_000_000 - position * mark(1)  # equity 5 at the start
        self.pending = (0, False, 0, 0)
        self.status, self.sent = 1, []

    def equity(self):
        return self.cash + self.position * self.mark(self.block)

    def view(self, to, sig, out, types=(), args=()):
        return {"result()": lambda: (self.status, "0xT", 5_000_000, 0), "pending()": lambda: self.pending,
                "step()": lambda: (0,), "equity()": lambda: (self.equity(),),
                "positionOf(uint32)": lambda: (self.position,),
                "notional()": lambda: (abs(self.position) * self.mark(self.block),),
                "terms()": lambda: [(5_000_000, [10], 3_600, 0, 0, 100, 2, 300)]}[sig]()

    def send(self, what, who, to, sig, types=(), args=()):
        self.sent.append((sig.split("(")[0], self.block))
        if sig.startswith("place"):
            self.pending = (args[0], args[1], args[2], self.block)
        elif sig == "fill()":
            asset, is_buy, sz, placed = self.pending
            assert placed + 2 <= self.block < placed + 302, "filled outside its window"
            px, delta = self.mark(self.block), sz if is_buy else -sz
            self.position += delta
            self.cash -= delta * px + r.close_cost(sz * px, 100)
            self.pending = (0, False, 0, 0)
        elif sig == "graduate()":
            assert self.position == 0 and self.equity() >= 5_005_000
            self.status = 2
        self.block += 1
        return {"blockNumber": hex(self.block - 1)}

    def run(self, **kw):
        import argparse
        from unittest import mock
        clock = {"t": 0.0}

        def sleep(s):
            clock["t"] += s
            self.block += 1

        run = r.Run.__new__(r.Run)
        run.dry = False
        a = argparse.Namespace(**{"who": "t", "exam": "0xE", "asset": "BTC", "cushion": 0.0, "minutes": 5.0,
                                  "every": 3.0, **kw})
        fake_time = mock.Mock(time=lambda: clock["t"], sleep=sleep)
        with mock.patch.object(r, "view", self.view), mock.patch.object(r, "universe", lambda: {"BTC": (3, 5, 76400.0)}), \
                mock.patch.object(r.c, "rpc", lambda m, p=(): hex(self.block)), \
                mock.patch.object(r.c, "precompile", lambda n, t, a, o: (self.mark(self.block),)), \
                mock.patch.object(r, "time", fake_time), mock.patch.object(run, "send", self.send), \
                mock.patch("builtins.print"):
            run.autoclose(a)


class AutocloseRun(unittest.TestCase):
    def test_a_short_in_profit_is_closed_at_the_first_block_it_may_and_the_step_passed(self):
        # BTC falls 3 USD a block: 26 steps short make 0.00078 USDC a block.
        x = FakeExam(-26, lambda b: 764_000 - 30 * b)
        x.run()
        self.assertEqual(x.status, 2)
        (place, at), (fill, filled), (graduate, _) = x.sent
        self.assertEqual((place, fill, graduate), ("place", "fill", "graduate"))
        self.assertEqual(filled, at + 2, "filled at the first block it may")
        self.assertEqual(x.position, 0)

    def test_a_mark_that_turns_before_the_fill_leaves_it_flat_and_says_so(self):
        # The same fall, and a jump of 20 USD the block after the order goes in.
        x = FakeExam(-26, lambda b: 764_000 - 30 * b)
        x.mark = lambda b: 764_000 - 30 * b + (200 if x.sent and b > x.sent[0][1] else 0)
        x.run()
        self.assertEqual([s for s, _ in x.sent], ["place", "fill"])
        self.assertEqual((x.status, x.position), (1, 0))
        self.assertLess(x.equity(), 5_005_000)

    def test_an_order_it_did_not_place_is_not_filled(self):
        x = FakeExam(-26, lambda b: 764_000 - 30 * b)
        x.pending = (3, True, 26, 1)  # the trader's own, waiting
        x.run(minutes=30.0)
        self.assertNotIn("fill", [s for s, _ in x.sent if _ < 302])
        self.assertEqual(x.sent[0][0], "place")
        self.assertGreaterEqual(x.sent[0][1], 1 + 2 + 300, "placed only once the trader's order lapsed")


class StatusNotes(unittest.TestCase):
    """Two readings of the first testnet run (c4d68b51) that misled: value low for a minute after a
    payment out, and BufferNeeded from a pool already emptied."""
    T = 1_790_000_000

    def test_the_buyer_approves_the_price_and_no_more(self):
        # The platform's 0.5 % is out of the price (item 28): MultiPool takes the price and no more.
        self.assertIn("usdc.safeTransferFrom(msg.sender, address(this), m.price - fee);", MULTI)
        self.assertNotIn("PLATFORM_FEE_BPS", (ROOT / "ops/multi_run.py").read_text())

    def test_the_reserve_is_what_extend_takes_out_after_the_end(self):
        # DECISIONS 103: money leaves once the end it was reserved for has passed, never before.
        self.assertIn("function setReserve(uint256 reserve_) external onlyOwner", MULTI)
        self.assertIn("if (reserve == 0 || block.timestamp <= reserveFor) revert Locked();", MULTI)
        sent = []
        run = r.Run.__new__(r.Run)
        run.pool = "0x" + "11" * 20
        run.send = lambda *x: sent.append(x)
        run.reserve(r.build_parser().parse_args(["--label", "x", "reserve", "--who", "multi-operator", "--usdc", "10"]))
        self.assertEqual(sent, [("setReserve", "multi-operator", run.pool, "setReserve(uint256)", ["uint256"],
                                 [10 * r.USDC_1E8])])

    def test_the_wait_is_the_pools(self):
        self.assertIn("uint64 public constant ARM_WAIT = 1 minutes;", MULTI)
        self.assertIn("return block.timestamp > lastOutAt + ARM_WAIT ? 0 : outFlight;", MULTI)
        self.assertEqual(r.ARM_WAIT, 60)

    def test_a_payment_still_counted_as_leaving_is_said(self):
        notes = r.status_notes(80 * r.USDC_1E8, 0, 0, 8000, 10 * r.USDC_1E8, self.T, self.T + 60)
        self.assertIn("10 paid out counts as still leaving until", notes[0])
        notes = r.status_notes(80 * r.USDC_1E8, 0, 0, 8000, 10 * r.USDC_1E8, self.T, self.T + 61)
        self.assertNotIn("still leaving", " ".join(notes))
        self.assertNotIn("still leaving", " ".join(r.status_notes(80 * r.USDC_1E8, 0, 0, 8000, 0, self.T, self.T)))

    def test_an_empty_pool_says_so(self):
        self.assertEqual(r.status_notes(0, 0, 0, 8000, 0, 0, self.T),
                         ["the pool is empty: there is nothing to withdraw, and a withdraw answers BufferNeeded"])
        self.assertNotIn("empty", " ".join(r.status_notes(0, 1, 5_000_000, 8000, 0, 0, self.T)))

    def test_what_a_withdrawal_may_take_is_what_the_share_in_work_leaves(self):
        # 100 in the pool, 40 in work at 80 %: 50 has to stay, 50 may go (_keepShare).
        notes = r.status_notes(100 * r.USDC_1E8, 1, 40_000_000, 8000, 0, 0, self.T)
        self.assertEqual(notes, ["a withdrawal, once the pool is over, may take up to 50.000000"])
        self.assertIn("if (inWork * Units.SPOT_PER_PERP * Units.BPS > (v - out1e8) * _plan.inWorkBps) revert BufferNeeded();", MULTI)
        notes = r.status_notes(100 * r.USDC_1E8, 1, 40_000_001, 8000, 0, 0, self.T)
        self.assertEqual(notes, ["a withdrawal, once the pool is over, may take up to 49.999999"])


class Refusals(unittest.TestCase):
    def run_main(self, exc):
        import contextlib
        import io
        import sys
        from unittest import mock

        class Failing:
            def __init__(self, record, dry):
                pass

            def status(self, _a):
                raise exc

        out = io.StringIO()
        with mock.patch.object(r.c, "assert_testnet", lambda: None), \
                mock.patch.object(r.deployments, "load", lambda label: {"MultiPool": "0x1", "PoolFactory": "0x2"}), \
                mock.patch.object(r, "Run", Failing), mock.patch.object(sys, "argv", ["multi_run", "--label", "m", "status"]), \
                contextlib.redirect_stdout(out):
            code = r.main()
        return code, out.getvalue()

    def test_a_contracts_refusal_is_one_line_with_its_name(self):
        data = "0x" + keccak(text="Ending()")[:4].hex()
        code, out = self.run_main(r.c.NotSent(f"eth_estimateGas: {{'code': 3, 'message': 'execution reverted', 'data': '{data}'}}"))
        self.assertEqual((code, out), (1, "  refused: Ending()\n"))

    def test_a_mined_revert_is_one_line_too(self):
        code, out = self.run_main(r.c.Reverted("transaction 0xabc reverted"))
        self.assertEqual((code, out), (1, "  reverted on chain: transaction 0xabc reverted\n"))


if __name__ == "__main__":
    unittest.main()
