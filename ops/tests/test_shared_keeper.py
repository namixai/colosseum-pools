"""Shared keeper tests. Offline: the chain, the precompiles, the info API and sending are one fake.

    spike/.venv/bin/python -m unittest ops.tests.test_shared_keeper
"""

from __future__ import annotations

import pathlib
import re
import unittest
from unittest import mock

from ops import keeper as core
from ops import shared_keeper as sk

ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "src" / "shared" / "SharedPool.sol").read_text()
POOL = "0x00000000000000000000000000000000000000A0"
SEAT = "0x00000000000000000000000000000000000000B1"
SEAT_2 = "0x00000000000000000000000000000000000000B2"
TICKET_1 = "0x00000000000000000000000000000000000000C1"
TICKET_2 = "0x00000000000000000000000000000000000000C2"
CLOSED = "0x00000000000000000000000000000000000000C9"
KEY = "0x00000000000000000000000000000000000000D1"
OTHER_KEY = "0x00000000000000000000000000000000000000D2"
KEEPER = "0x00000000000000000000000000000000000000E1"
ZERO = sk.ZERO
MIN = 20 * 10**8
NEED = 11 * 10**8
NOW = 1_790_300_000


class Refused(RuntimeError):
    pass


class FakeChain:
    """A shared pool, its seats and tickets, the precompiles and the info API, with every send
    recorded. `refuse` names calls the contract would revert, by function name."""

    USDC_TOKEN = 0

    def __init__(self):
        self.pool = {"seats": [], "queuedShares": 0, "minDeposit": MIN, "open": [], "closed": [],
                     "fundedSince": {}, "fundedKey": {}, "fundedTerm": {}}
        self.seats: dict[str, dict] = {}
        self.spots: dict[str, int] = {}
        self.refuse: dict[str, str] = {}
        self.gas: dict[str, int] = {}
        self.gas_per_ticket = 0
        self.estimates: list[tuple[str, str, bytes]] = []
        self.sent: list[tuple[str, str, list]] = []
        self.orders: dict[str, list] = {}
        self.positions: dict[str, list] = {}

    def add_seat(self, addr, stage=sk.IDLE, challenge=ZERO, spot=0, ready=False, key=ZERO, assets=(3,)):
        self.pool["seats"].append(addr)
        self.seats[addr.lower()] = {"stage": stage, "challenge": challenge, "capitalNeeded": NEED,
                                   "accountReady": ready, "agentKey": key, "assets": assets}
        self.spots[addr.lower()] = spot

    # spike.hlspike.common, as the keepers use it
    def call_view(self, to, signature, types, args, out):
        name = signature.split("(")[0]
        if to.lower() == POOL.lower():
            p = self.pool
            if name == "seats":
                return (list(p["seats"]),)
            if name == "openTicketCount":
                return (len(p["open"]),)
            if name == "openTickets":
                start, count = args
                return (p["open"][start:start + count],)
            if name == "closedTickets":
                return (list(p["closed"]),)
            if name in ("fundedSince", "fundedKey", "fundedTerm"):
                return (p[name].get(args[0].lower(), ZERO if name == "fundedKey" else 0),)
            return (p[name],)
        seat = self.seats.get(to.lower())
        if seat is None:
            raise AssertionError(f"read from an address the keeper should not touch: {to}")
        if name == "rules":
            return ((500, 1000, 300, seat["assets"]),)
        return (seat[name],)

    def core_spot_balance(self, user, token):
        return {"total": self.spots.get(user.lower(), 0), "hold": 0, "entryNtl": 0}

    def encode_call(self, signature, types=(), args=()):
        # The signature and, for settle, how many tickets it names: all the fake needs to answer.
        named = len(args[0]) if args and isinstance(args[0], list) else 0
        return f"{signature}|{named}".encode()

    def rpc(self, method, params=()):
        if method != "eth_estimateGas":
            raise AssertionError(f"unexpected rpc {method}")
        q = params[0]
        signature, named = bytes.fromhex(q["data"][2:]).decode().split("|")
        name = signature.split("(")[0]
        self.estimates.append((q["to"].lower(), name, q["from"]))
        if name in self.refuse:
            raise Refused(f"eth_estimateGas: {{'code': 3, 'message': 'execution reverted', 'data': '{self.refuse[name]}'}}")
        if name == "settle" and self.gas_per_ticket:
            return hex(self.gas.get(name, 150_000) + self.gas_per_ticket * int(named))
        return hex(self.gas.get(name, 150_000))

    def transact(self, wallet, to, signature, types, args):
        self.sent.append((to.lower(), signature.split("(")[0], list(args)))
        return {"transactionHash": "0x" + "ab" * 32}

    def info_post(self, body):
        kind = body["type"]
        if kind == "meta":
            return {"universe": [{"name": n} for n in ("SOL", "APT", "ATOM", "BTC", "ETH")]}
        if kind == "openOrders":
            return self.orders.get(body["user"].lower(), [])
        if kind == "clearinghouseState":
            return {"assetPositions": [{"position": {"coin": coin}} for coin in self.positions.get(body["user"].lower(), [])]}
        raise AssertionError(f"unexpected info call {kind}")

    def calls(self, name=None):
        return [(to, fn, args) for to, fn, args in self.sent if name is None or fn == name]


def selector(signature: str) -> str:
    return "0x" + sk.keccak(text=signature)[:4].hex()


class SharedKeeperTest(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain()
        for module in (sk, core):
            patcher = mock.patch.object(module, "c", self.chain)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.clock = mock.patch.object(sk.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.logged: list[tuple[str, dict]] = []
        quiet = mock.patch.object(sk, "log", side_effect=lambda event, **f: self.logged.append((event, f)))
        quiet.start()
        self.addCleanup(quiet.stop)

    def make(self, dry=False, arm=True, queue_every=300):
        return sk.SharedKeeper(POOL, object(), KEEPER, dry, arm=arm, queue_every=queue_every)

    def at(self, t):
        self.clock.stop()
        self.clock = mock.patch.object(sk.time, "time", return_value=t)
        self.clock.start()

    def events(self, name):
        return [f for e, f in self.logged if e == name]


class Points(SharedKeeperTest):
    def test_a_deposit_on_a_ticket_runs_a_point_that_names_it(self):
        self.chain.pool["open"] = [TICKET_1, TICKET_2]
        self.chain.spots[TICKET_1.lower()] = MIN
        self.chain.spots[TICKET_2.lower()] = MIN - 1  # below the minimum: naming it would do nothing
        self.make().one_pass()
        self.assertEqual(self.chain.calls("settle"), [(POOL.lower(), "settle", [[sk.to_checksum_address(TICKET_1)]])])

    def test_an_empty_ticket_is_read_again_only_every_few_passes(self):
        self.chain.pool["open"] = [TICKET_1]
        reads = []
        real = self.chain.core_spot_balance

        def counted(user, token):
            reads.append(user.lower())
            return real(user, token)

        k = self.make()
        with mock.patch.object(self.chain, "core_spot_balance", side_effect=counted):
            for _ in range(sk.RECHECK_EVERY):
                k.one_pass()
            self.assertEqual(reads.count(TICKET_1.lower()), 1, "empty once, then left alone")
            self.chain.spots[TICKET_1.lower()] = MIN  # the deposit lands late
            k.one_pass()
        self.assertEqual(reads.count(TICKET_1.lower()), 2)
        self.assertEqual(self.chain.calls("settle"), [(POOL.lower(), "settle", [[sk.to_checksum_address(TICKET_1)]])])

    def test_a_new_ticket_is_read_at_once(self):
        self.chain.pool["open"] = [TICKET_1]
        k = self.make()
        k.one_pass()
        self.chain.pool["open"] = [TICKET_1, TICKET_2]
        self.chain.spots[TICKET_2.lower()] = MIN
        k.one_pass()
        self.assertEqual(self.chain.calls("settle"), [(POOL.lower(), "settle", [[sk.to_checksum_address(TICKET_2)]])])

    def test_nothing_to_take_in_or_pay_runs_no_point(self):
        self.chain.pool["open"] = [TICKET_1]
        self.chain.spots[TICKET_1.lower()] = 0
        self.make().one_pass()
        self.assertEqual(self.chain.calls("settle"), [])
        self.assertNotIn("settle", [name for _, name, _ in self.chain.estimates])

    def test_a_queue_alone_runs_a_point_at_most_once_a_period(self):
        self.chain.pool["queuedShares"] = 5 * 10**8
        k = self.make(queue_every=300)
        k.one_pass()
        self.at(NOW + 299)
        k.one_pass()
        self.assertEqual(len(self.chain.calls("settle")), 1)
        self.at(NOW + 300)
        k.one_pass()
        self.assertEqual(len(self.chain.calls("settle")), 2)

    def test_a_deposit_does_not_wait_for_the_queues_period(self):
        self.chain.pool["queuedShares"] = 5 * 10**8
        k = self.make(queue_every=300)
        k.one_pass()
        self.chain.pool["open"] = [TICKET_1]
        self.chain.spots[TICKET_1.lower()] = MIN
        self.at(NOW + 10)
        k.one_pass()
        self.assertEqual(len(self.chain.calls("settle")), 2)

    def test_money_left_on_a_closed_ticket_runs_a_point_to_sweep_it(self):
        self.chain.pool["closed"] = [CLOSED]
        self.chain.spots[CLOSED.lower()] = sk.SWEEP_MIN
        self.make().one_pass()
        self.assertEqual(self.chain.calls("settle"), [(POOL.lower(), "settle", [[]])])
        self.chain.sent.clear()
        self.chain.spots[CLOSED.lower()] = sk.SWEEP_MIN - 1  # dust the pool forgets
        self.make().one_pass()
        self.assertEqual(self.chain.calls("settle"), [])

    def test_a_point_is_tried_once_before_it_is_sent(self):
        self.chain.pool["open"] = [TICKET_1]
        self.chain.spots[TICKET_1.lower()] = MIN
        self.make().one_pass()
        self.assertEqual([name for _, name, _ in self.chain.estimates].count("settle"), 1)

    def test_a_point_the_pool_refuses_is_not_sent_and_says_why(self):
        self.chain.pool["open"] = [TICKET_1]
        self.chain.spots[TICKET_1.lower()] = MIN
        self.chain.refuse["settle"] = selector("NotQuiet(uint8,address)") + "00" * 64
        self.make().one_pass()
        self.assertEqual(self.chain.calls("settle"), [])
        self.assertIn({"to": sk.to_checksum_address(POOL), "call": "settle", "reason": "NotQuiet"}, self.events("not_due"))

    def test_a_point_too_big_for_a_small_block_names_fewer_tickets(self):
        tickets = [f"0x{i:040x}" for i in range(1, 11)]
        self.chain.pool["open"] = tickets
        for ticket in tickets:
            self.chain.spots[ticket.lower()] = MIN
        self.chain.gas["settle"] = 200_000
        self.chain.gas_per_ticket = 250_000
        self.make().one_pass()
        # Eight tickets would take 2.2M gas; four take 1.2M, which fits 2M with a quarter to spare.
        (to, fn, args), = self.chain.calls("settle")
        self.assertEqual(args[0], [sk.to_checksum_address(x) for x in tickets[:4]])
        self.assertEqual([e["tickets"] for e in self.events("point_too_big")], [8])

    def test_a_point_that_fits_no_small_block_without_tickets_is_not_sent(self):
        self.chain.pool["queuedShares"] = 5 * 10**8
        self.chain.gas["settle"] = 1_700_000
        self.make().one_pass()
        self.assertEqual(self.chain.calls("settle"), [])
        self.assertEqual(len(self.events("point_needs_big_block")), 1)

    def test_a_dry_run_sends_nothing_and_says_what_it_would(self):
        self.chain.pool["open"] = [TICKET_1]
        self.chain.spots[TICKET_1.lower()] = MIN
        self.make(dry=True).one_pass()
        self.assertEqual(self.chain.sent, [])
        self.assertIn({"to": sk.to_checksum_address(POOL), "call": "settle"}, self.events("would_send"))

    def test_every_call_is_tried_from_the_keepers_own_address(self):
        self.chain.pool["open"] = [TICKET_1]
        self.chain.spots[TICKET_1.lower()] = MIN
        self.make().one_pass()
        self.assertTrue(self.chain.estimates)
        self.assertTrue(all(sender == sk.to_checksum_address(KEEPER) for _, _, sender in self.chain.estimates))


class Seats(SharedKeeperTest):
    def test_an_idle_seat_short_of_capital_is_armed(self):
        self.chain.add_seat(SEAT, spot=0)
        self.make().one_pass()
        self.assertEqual(self.chain.calls("armSeat"), [(POOL.lower(), "armSeat", [sk.to_checksum_address(SEAT)])])

    def test_no_arm_leaves_the_seat_alone(self):
        self.chain.add_seat(SEAT, spot=0)
        self.make(arm=False).one_pass()
        self.assertEqual(self.chain.calls("armSeat"), [])

    def test_a_seat_the_pool_wont_arm_now_is_left_for_later(self):
        self.chain.add_seat(SEAT, spot=0)
        self.chain.refuse["armSeat"] = selector("QueueWaiting()")
        self.make().one_pass()
        self.assertEqual(self.chain.calls("armSeat"), [])
        self.assertIn({"to": sk.to_checksum_address(POOL), "call": "armSeat", "reason": "QueueWaiting"}, self.events("not_due"))

    def test_an_armed_seat_is_prepared_once(self):
        self.chain.add_seat(SEAT, spot=NEED, ready=False)
        self.make().one_pass()
        self.assertEqual(self.chain.calls("prepareAccount"), [(SEAT.lower(), "prepareAccount", [])])
        self.chain.sent.clear()
        self.chain.seats[SEAT.lower()]["accountReady"] = True
        self.make().one_pass()
        self.assertEqual(self.chain.sent, [])

    def test_a_busy_seat_is_neither_armed_nor_released(self):
        self.chain.add_seat(SEAT, stage=sk.CHALLENGE, challenge=OTHER_KEY, spot=8 * 10**8)
        self.chain.pool["queuedShares"] = 5 * 10**8
        self.make().one_pass()
        self.assertEqual(self.chain.calls("armSeat") + self.chain.calls("releaseSeat"), [])

    def test_while_the_queue_waits_an_idle_seats_capital_is_asked_back(self):
        self.chain.add_seat(SEAT, spot=NEED, ready=True)
        self.chain.add_seat(SEAT_2, spot=0)
        self.chain.pool["queuedShares"] = 5 * 10**8
        self.make().one_pass()
        self.assertEqual(self.chain.calls("releaseSeat"), [(POOL.lower(), "releaseSeat", [sk.to_checksum_address(SEAT)])])

    def test_without_a_queue_no_capital_is_asked_back(self):
        self.chain.add_seat(SEAT, spot=NEED, ready=True)
        self.make().one_pass()
        self.assertEqual(self.chain.calls("releaseSeat"), [])
        self.assertNotIn("releaseSeat", [name for _, name, _ in self.chain.estimates])


class FundedTerm(SharedKeeperTest):
    def funded(self, since=0, noted=ZERO, term=900):
        self.chain.add_seat(SEAT, stage=sk.FUNDED, spot=0, ready=True, key=KEY)
        self.chain.pool["fundedSince"][SEAT.lower()] = since
        self.chain.pool["fundedKey"][SEAT.lower()] = noted
        self.chain.pool["fundedTerm"][SEAT.lower()] = term

    def test_a_new_funded_stage_is_noted(self):
        self.funded()
        self.make().one_pass()
        self.assertEqual(self.chain.calls("noteFunded"), [(POOL.lower(), "noteFunded", [sk.to_checksum_address(SEAT)])])
        self.assertEqual(self.chain.calls("endFundedTerm"), [])

    def test_a_stage_noted_under_another_key_is_noted_again(self):
        self.funded(since=NOW - 10_000, noted=OTHER_KEY)
        self.make().one_pass()
        self.assertEqual(len(self.chain.calls("noteFunded")), 1)
        self.assertEqual(self.chain.calls("endFundedTerm"), [])

    def test_a_stage_within_its_term_is_left_alone(self):
        self.funded(since=NOW - 899, noted=KEY, term=900)
        self.make().one_pass()
        self.assertEqual(self.chain.calls("noteFunded") + self.chain.calls("endFundedTerm"), [])

    def test_a_stage_past_its_term_is_ended_with_its_orders_and_stray_positions(self):
        self.funded(since=NOW - 900, noted=KEY, term=900)
        self.chain.orders[SEAT.lower()] = [{"coin": "BTC", "oid": 77}]
        self.chain.positions[SEAT.lower()] = ["BTC", "SOL"]  # SOL is outside the seat's rules
        self.make().one_pass()
        (to, fn, args), = self.chain.calls("endFundedTerm")
        self.assertEqual(to, POOL.lower())
        seat, cancels, extra, salt = args
        self.assertEqual(seat, sk.to_checksum_address(SEAT))
        self.assertEqual(cancels, [(3, 77)])
        self.assertEqual(extra, [0])
        self.assertEqual(len(salt), 32)


class AgainstTheSource(unittest.TestCase):
    def test_the_constants_are_the_contracts(self):
        self.assertRegex(SOURCE, rf"MAX_PER_POINT = {sk.MAX_PER_POINT};")
        self.assertRegex(SOURCE, r"SWEEP_MIN = 1e8;")
        self.assertEqual(sk.SWEEP_MIN, 10**8)

    def test_every_error_the_keeper_names_is_the_contracts(self):
        for sig in sk.ERRORS:
            name, params = sig.split("(", 1)
            if name == "NotReady":  # Pool's, raised by prepareAccount
                continue
            m = re.search(rf"error {name}\(([^)]*)\);", SOURCE)
            self.assertIsNotNone(m, f"SharedPool.sol has no error {name}")
            types = [p.split()[0] for p in m.group(1).split(",") if p.strip()]
            types = ["uint8" if t == "Blocker" else t for t in types]
            self.assertEqual(",".join(types), params.rstrip(")"), name)

    def test_every_call_the_keeper_makes_is_the_contracts(self):
        text = (ROOT / "ops" / "shared_keeper.py").read_text()
        for name in ("noteFunded", "endFundedTerm", "releaseSeat", "settle", "armSeat", "openTicketCount",
                     "openTickets", "closedTickets", "fundedSince", "fundedKey", "fundedTerm", "queuedShares",
                     "minDeposit", "seats"):
            self.assertIn(f'"{name}(', text)
            self.assertRegex(SOURCE, rf"function {name}\(|public (?:immutable |constant )?{name};")


if __name__ == "__main__":
    unittest.main()
