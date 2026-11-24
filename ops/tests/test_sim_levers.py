"""The levers a person pulls on a simulated pool that no program does (ops/deploy_sim.py): a stop of reason 3, sent by
hand from the checker's key on the review's draft and a written analysis; a line of the menu switched off; a key of the
platform replaced in the factory. Offline: the chain is what each test says it is, nothing is sent, no key is read."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from eth_abi import decode as abi_decode
from eth_utils import keccak, to_checksum_address

from ops import deploy_sim as d
from ops import payout_review as pr
from ops import sim_check as chk

ROOT = pathlib.Path(__file__).resolve().parents[2]
POOL, FACTORY = to_checksum_address("0x" + "a1" * 20), to_checksum_address("0x" + "99" * 20)
CHECKER, OTHER = to_checksum_address("0x" + "c1" * 20), to_checksum_address("0x" + "0e" * 20)
ACCOUNT, TRADER = to_checksum_address("0x" + "11" * 20), to_checksum_address("0x" + "77" * 20)
TRADES = ["0x" + "5a" * 32, "0x" + "5b" * 32]
DAY = 86_400
NOW, DUE = 1_800_000_000, 1_800_000_000 + 2 * DAY
CHAIN = d.c.CHAIN_ID
# payout(id) of a pool with approvals: account, trader, amount, dueAt, state, wasStopped, stoppedAt, approved.
WAITING = (ACCOUNT, TRADER, 800_000_000, DUE, 1, False, 0, False)


def review(**over) -> dict:
    """A payout's review as the review service writes it for a result that does not count up: held, with the draft of
    a stop of reason 3 naming the transactions and leaving the analysis to a person."""
    doc = {"schema": pr.SCHEMA, "chainId": CHAIN, "pool": POOL, "id": 3, "trader": TRADER, "amount": "800000000",
           "claimedAt": NOW - DAY, "dueAt": DUE, "verdict": "suspicious", "action": "hold",
           "readAt": {"block": 4_000, "time": NOW - 60},
           "stopDraft": {"kind": "stop-draft", "to": POOL, "call": chk.STOP, "reason": 3, "paired": pr.ZERO_ADDRESS,
                         "trades": list(TRADES), "links": [pr.ANALYSIS], "data": None, "note": "a person writes it"}}
    doc.update(over)
    return doc


def draft(**over) -> dict:
    return review(stopDraft={**review()["stopDraft"], **over})


class Chain:
    """The pool, its factory and the node, as one test says they are."""

    def __init__(self, payouts=(WAITING,), checker=CHECKER, approvals=True, refuses: str | None = None, count=5):
        self.payouts, self.checker, self.approvals, self.refuses, self.count = list(payouts), checker, approvals, refuses, count
        self.reads = 0
        self.calls: list[dict] = []

    def view(self, to, sig, types, values, out):
        if sig == "approver()":
            if not self.approvals:
                raise RuntimeError("eth_call: execution reverted")
            return [OTHER]
        if sig == "payout(uint256)":
            self.reads += 1
            p = self.payouts[min(self.reads, len(self.payouts)) - 1]
            return [p if self.approvals else p[:7]]
        return {"factory()": [FACTORY], "checker()": [self.checker], "payoutCount()": [self.count],
                "STOP_RESERVE()": [14 * DAY]}[sig]

    def rpc(self, method, params=()):
        if method == "eth_chainId":
            return hex(CHAIN)
        if method == "eth_call":
            self.calls.append(params[0])
            if self.refuses:
                raise RuntimeError(f"eth_call: {{'code': 3, 'message': 'execution reverted: {self.refuses}'}}")
            return "0x"
        raise AssertionError(f"not a read of this command: {method}")


class TheStopOfReason3(unittest.TestCase):
    """A result disputed without a pair is stopped by a person: the review's draft names the pool and the transactions,
    the person writes and publishes the analysis, and the checker's key sends."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.analysis = self.dir / "analysis.md"
        self.analysis.write_bytes(b"The oracle price of block 7 was not the venue's.\n")
        self.link = keccak(self.analysis.read_bytes())

    def run_stop(self, chain: Chain, doc: dict | None = None, *flags, key=CHECKER, analysis=True, now=NOW, sent=None,
                 confirm=None):
        path = self.dir / "review.json"
        path.write_text(json.dumps(review() if doc is None else doc))
        argv = ["stop", "--review", str(path), *(["--analysis", str(self.analysis)] if analysis else []), *flags]
        if "--signer" not in flags:
            argv += ["--key", "checker"]
        out = io.StringIO()
        sent = mock.Mock(return_value={"transactionHash": "0x01"}) if sent is None else sent
        with mock.patch.object(d.c, "call_view", side_effect=chain.view), mock.patch.object(d.c, "rpc", side_effect=chain.rpc), \
                mock.patch.object(d, "chain_now", return_value=now), mock.patch.object(d.c, "address_of", return_value=key), \
                mock.patch.object(d.c, "assert_testnet"), mock.patch.object(d.c, "account", return_value="the checker's key") as wallet, \
                mock.patch.object(d.c, "transact", sent), mock.patch.object(chk, "log"), \
                mock.patch.object(d.sn, "confirm_send", confirm or mock.Mock()) as confirmed, contextlib.redirect_stdout(out):
            code = d.main(argv)
        return code, out.getvalue(), sent, wallet, confirmed

    def refused(self, chain: Chain, doc: dict | None = None, *flags, **kw) -> str:
        sent = mock.Mock()
        with self.assertRaises(SystemExit) as stop:
            self.run_stop(chain, doc, *flags, sent=sent, **kw)
        sent.assert_not_called()
        return str(stop.exception)

    def values(self) -> list:
        return [3, 3, d.ZERO, [bytes.fromhex(t[2:]) for t in TRADES], [self.link]]

    def test_without_send_it_shows_what_would_go_and_sends_nothing(self):
        chain = Chain()
        code, said, sent, wallet, confirmed = self.run_stop(chain)
        self.assertEqual(code, 0)
        sent.assert_not_called()
        wallet.assert_not_called()
        confirmed.assert_not_called()
        # The pool, the payout, the reason, each transaction and the analysis's hash: what a person checks.
        self.assertIn(f"deploy_sim stop: pool {POOL}, stopPayout of payout 3 for reason 3 from checker ({CHECKER}), "
                      "the factory's checker", said)
        self.assertIn(f"payout 3: 800 USDC to {TRADER}, waiting", said)
        self.assertIn("reason 3: The result is disputed: a flaw of the exam, the oracle or the filler", said)
        for i, tx in enumerate(TRADES):
            self.assertIn(f"transaction {i + 1} of 2: {tx}", said)
        self.assertIn(f"the analysis: keccak256 0x{self.link.hex()}, of {self.analysis}, {self.analysis.stat().st_size} bytes", said)
        self.assertIn("for 14 days", said)
        self.assertIn("deploy_sim: nothing was sent (--send sends).", said)
        # The pool was asked, by eth_call from the checker, with the call that would be sent.
        self.assertEqual(len(chain.calls), 1)
        self.assertEqual((chain.calls[0]["from"], chain.calls[0]["to"]), (CHECKER, POOL))
        self.assertEqual(chain.calls[0]["data"], "0x" + d.c.encode_call(chk.STOP, chk.STOP_TYPES, self.values()).hex())

    def test_the_call_is_the_drafts_with_the_analysis_as_its_one_link(self):
        code, said, sent, _, _ = self.run_stop(Chain(), None, "--signer", CHECKER)
        self.assertEqual(code, 0)
        sent.assert_not_called()
        batch = json.loads(said[said.index('{\n "version"'):])
        (tx,) = batch["transactions"]
        self.assertEqual((tx["to"], tx["value"], batch["chainId"]), (POOL, "0", str(CHAIN)))
        data = bytes.fromhex(tx["data"][2:])
        self.assertEqual(data[:4], keccak(text="stopPayout(uint256,uint8,address,bytes32[],bytes32[])")[:4])
        pid, reason, paired, trades, links = abi_decode(chk.STOP_TYPES, data[4:])
        self.assertEqual((pid, reason, int(paired, 16)), (3, 3, 0), "reason 3 names no paired account")
        self.assertEqual(["0x" + t.hex() for t in trades], TRADES)
        self.assertEqual(list(links), [self.link], "one link: the keccak256 of the analysis, byte for byte as the file is")
        self.assertNotEqual(self.link, keccak(self.analysis.read_bytes().strip()), "not of the text trimmed")
        # What the contract takes for reason 3 (SimPoolStops.stop): no paired account, a transaction, one link.
        stops = (ROOT / "src" / "sim" / "SimPoolStops.sol").read_text()
        self.assertIn("if (paired != address(0) || trades.length == 0 || links.length != 1) revert SimPool.BadReason();", stops)
        # And the draft is what the review writes.
        source = (ROOT / "ops" / "payout_review.py").read_text()
        self.assertIn('out["stopDraft"] = {"kind": "stop-draft", "to": pool, "call": chk.STOP, "reason": 3, "paired": ZERO_ADDRESS,', source)
        self.assertIn('"trades": list(dict.fromkeys(flagged)), "links": [ANALYSIS], "data": None,', source)

    def test_a_hash_made_elsewhere_is_taken_as_the_link(self):
        given = "0x" + "ee" * 32
        code, said, _, _, _ = self.run_stop(Chain(), None, "--signer", CHECKER, "--analysis-hash", given, analysis=False)
        data = bytes.fromhex(json.loads(said[said.index('{\n "version"'):])["transactions"][0]["data"][2:])
        self.assertEqual(list(abi_decode(chk.STOP_TYPES, data[4:])[4]), [bytes.fromhex("ee" * 32)])
        self.assertIn(f"the analysis: keccak256 {given}, given as a hash", said)

    def test_with_send_it_goes_from_the_checkers_key_after_the_confirmation(self):
        order = []
        confirm = mock.Mock(side_effect=lambda *a, **k: order.append("confirmed"))
        sent = mock.Mock(side_effect=lambda *a, **k: order.append("sent") or {"transactionHash": "0x01"})
        chain = Chain()
        code, said, sent, wallet, confirmed = self.run_stop(chain, None, "--send", sent=sent, confirm=confirm)
        self.assertEqual((code, order), (0, ["confirmed", "sent"]))
        wallet.assert_called_once_with("checker")
        self.assertEqual(sent.call_args[0], ("the checker's key", POOL, chk.STOP, chk.STOP_TYPES, self.values()))
        self.assertIn("deploy_sim: sent, tx 0x01", said)
        # The confirmation is the one every send of this script has: the phrase at a terminal on mainnet.
        what = confirmed.call_args[0][1]
        self.assertIn(f"on {POOL} from the key 'checker'", what)
        self.assertEqual(chain.reads, 2, "the payout is read once more after the confirmation, before the send")

    def test_not_confirmed_nothing_is_sent(self):
        said = self.refused(Chain(), None, "--send", confirm=mock.Mock(side_effect=SystemExit("not confirmed; nothing was sent")))
        self.assertEqual(said, "not confirmed; nothing was sent")

    def test_a_payout_paid_or_stopped_while_the_person_read_is_not_sent(self):
        for state, was, why in ((3, False, "is Paid, not waiting"), (2, True, "is Stopped, not waiting"),
                                (1, True, "was stopped once already")):
            after = WAITING[:4] + (state, was) + WAITING[6:]
            with self.subTest(why):
                self.assertIn(why, self.refused(Chain(payouts=(WAITING, after)), None, "--send"))

    def test_a_payout_the_pool_takes_no_stop_of_is_refused(self):
        names = d.PAYOUT_STATE
        for state in ("None", "Stopped", "Paid", "Held", "Refused"):
            p = WAITING[:4] + (names.index(state), False) + WAITING[6:]
            with self.subTest(state):
                self.assertIn(f"BadPayout: payout 3 is {state}, not waiting: the pool takes no stop of it now",
                              self.refused(Chain(payouts=(p,))))
        lifted = WAITING[:5] + (True,) + WAITING[6:]
        self.assertIn("BadPayout: payout 3 was stopped once already and its stop lifted", self.refused(Chain(payouts=(lifted,))))
        self.assertIn("BadPayout: payout 3 does not exist", self.refused(Chain(count=3)))
        # A pool from before approvals pays at its time by itself: within 5 minutes of it the stop is not sent.
        self.assertIn("its time to stop is over", self.refused(Chain(approvals=False), now=DUE - chk.MARGIN_S))
        code, *_ = self.run_stop(Chain(approvals=False), now=DUE - chk.MARGIN_S - 1)
        self.assertEqual(code, 0)
        # A pool with approvals takes a stop until the payout is paid, past its due time too.
        self.assertEqual(self.run_stop(Chain(), now=DUE + 3 * DAY)[0], 0)

    def test_only_the_factorys_checker_stops(self):
        said = self.refused(Chain(), key=OTHER)
        self.assertIn(f"NotChecker: {OTHER} is not the factory's checker ({CHECKER})", said)
        self.assertIn("the pool would refuse it: eth_call: {'code': 3, 'message': 'execution reverted: BadReason()'}",
                      self.refused(Chain(refuses="BadReason()")))

    def test_a_review_of_another_chain_pool_or_claim_stops_nothing(self):
        self.assertIn("a review of another chain", self.refused(Chain(), review(chainId=CHAIN + 1)))
        self.assertIn("a review of another chain", self.refused(Chain(), review(chainId=None)))
        self.assertIn(f"not a {pr.SCHEMA} document", self.refused(Chain(), review(schema="payout-review/3")))
        self.assertIn(f"not a {pr.SCHEMA} document", self.refused(Chain(), {"schema": pr.SCHEMA, "x": 1} and []))
        for field, value in (("trader", OTHER), ("amount", "800000001"), ("dueAt", DUE + 1), ("amount", "a lot")):
            with self.subTest(field):
                self.assertIn("the review is of another claim than payout 3", self.refused(Chain(), review(**{field: value})))
        (self.dir / "broken.json").write_text("{")
        with self.assertRaises(SystemExit) as stop, mock.patch.object(d.c, "rpc", side_effect=Chain().rpc):
            d.main(["stop", "--review", str(self.dir / "broken.json"), "--analysis", str(self.analysis), "--key", "checker"])
        self.assertIn("cannot be read as JSON", str(stop.exception))

    def test_a_draft_that_is_not_the_reviews_is_sent_not_at_all(self):
        self.assertIn("made no draft of a stop", self.refused(Chain(), review(stopDraft=None)))
        bad = {"another reason": dict(reason=1), "a paired account": dict(paired=OTHER), "another pool": dict(to=FACTORY),
               "another call": dict(call="liftStop(uint256)"), "no transaction": dict(trades=[]),
               "one twice": dict(trades=[TRADES[0], TRADES[0]]), "not a hash": dict(trades=["0x12"]),
               "an analysis named in the document": dict(links=["0x" + "ee" * 32]), "no links": dict(links=[]),
               "another kind": dict(kind="stop"), "trades not a list": dict(trades="0x" + "5a" * 32)}
        for what, over in bad.items():
            with self.subTest(what):
                self.assertIn("is not a draft of a stop of reason 3 on its own pool", self.refused(Chain(), draft(**over)))
        self.assertIn("is not a draft of a stop of reason 3", self.refused(Chain(), review(pool="0x12", stopDraft={**review()["stopDraft"], "to": "0x12"})))

    def test_the_analysis_is_named_once_and_is_something(self):
        self.assertIn("--analysis <the written analysis", self.refused(Chain(), analysis=False))
        self.assertIn("one of the two", self.refused(Chain(), None, "--analysis-hash", "0x" + "ee" * 32))
        self.assertIn("is not a keccak256", self.refused(Chain(), None, "--analysis-hash", "0xee", analysis=False))
        self.analysis.write_bytes(b" \n")
        self.assertIn("is empty", self.refused(Chain()))
        self.analysis.unlink()
        self.assertIn("No such file", self.refused(Chain()))

    def test_a_pool_not_read_is_not_stopped(self):
        chain = Chain()
        with mock.patch.object(chk, "takes_approvals", return_value=None):
            self.assertIn("was not read (approver())", self.refused(chain))


class TheLine(unittest.TestCase):
    """A line of the menu switched off for new sales, by the pool's creator: our Safe."""

    MENU = [(5_000_000_000, 20_000_000), (25_000_000_000, 110_000_000)]

    def view(self, state=(True, True, True, 0)):
        def call_view(to, sig, types, values, out):
            return {"creator()": [OTHER], "menu()": [self.MENU], "lineState(uint256)": state}[sig]
        return call_view

    def test_the_pools_refusals_in_its_order(self):
        on, off = (True, True, True, 0), (False, False, True, 0)
        self.assertIn("line 1 is switched off: no new exam of it is sold", d.line_check(OTHER, OTHER, 2, 1, False, on))
        self.assertIn("line 1 is switched on", d.line_check(OTHER, OTHER, 2, 1, True, off))
        for args, why in (((OTHER, CHECKER, 2, 1, False, on), "NotCreator"), ((OTHER, OTHER, 2, 2, False, None), "NoSuchLine"),
                          ((OTHER, OTHER, 2, -1, False, on), "NoSuchLine"),
                          ((OTHER, OTHER, 2, 1, False, off), "line 1 is off already: nothing to send"),
                          ((OTHER, OTHER, 2, 1, True, on), "line 1 is on already: nothing to send"),
                          ((OTHER, OTHER, 2, 1, True, (False, False, False, 1_500_000)), "NotCovered: the pool is 1.5 USDC short"),
                          ((OTHER, OTHER, 2, 1, True, (False, False, False, 0)), "Closing: the pool is closed")):
            with self.subTest(why), self.assertRaises(SystemExit) as stop:
                d.line_check(*args)
            self.assertIn(why, str(stop.exception))
        # The pool's own words for them (SimPool.setLine, lineState).
        pool = (ROOT / "src" / "sim" / "SimPool.sol").read_text()
        self.assertIn("function setLine(uint256 line, bool on) external onlyCreator {", pool)
        self.assertIn("function lineState(uint256 line) external view returns (bool on, bool sells, bool canSwitchOn, uint256 short)", pool)

    def test_for_the_safe_it_prints_the_call_and_sends_nothing(self):
        out = io.StringIO()
        with mock.patch.object(d.c, "call_view", side_effect=self.view()), mock.patch.object(d.c, "account") as key, \
                mock.patch.object(d.c, "transact") as sent, contextlib.redirect_stdout(out):
            self.assertEqual(d.main(["line", "--pool", POOL, "--signer", OTHER, "--line", "1", "--off"]), 0)
        sent.assert_not_called()
        key.assert_not_called()
        said = out.getvalue()
        self.assertIn(f"setLine(1, false) from the signer ({OTHER}): the line of 25,000 USDC for 110 USDC", said)
        (tx,) = json.loads(said[said.index('{\n "version"'):])["transactions"]
        self.assertEqual(tx, {"to": POOL, "value": "0", "data": "0x" + (
            keccak(text="setLine(uint256,bool)")[:4] + (1).to_bytes(32, "big") + (0).to_bytes(32, "big")).hex()})

    def test_from_a_key_it_sends_only_with_send_and_one_of_off_and_on_is_said(self):
        with mock.patch.object(d.c, "call_view", side_effect=self.view((False, False, True, 0))), \
                mock.patch.object(d.c, "address_of", return_value=OTHER), mock.patch.object(d.c, "assert_testnet"), \
                mock.patch.object(d.c, "account"), mock.patch.object(d.c, "transact", return_value={"transactionHash": "0x01"}) as sent, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(["line", "--pool", POOL, "--key", "creator", "--line", "0", "--on"]), 0)
            sent.assert_not_called()
            self.assertEqual(d.main(["line", "--pool", POOL, "--key", "creator", "--line", "0", "--on", "--send"]), 0)
            self.assertEqual(sent.call_args[0][1:], (POOL, "setLine(uint256,bool)", ["uint256", "bool"], [0, True]))
        for flags in ([], ["--on", "--off"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                d.main(["line", "--pool", POOL, "--signer", OTHER, "--line", "0", *flags])


class TheRoles(unittest.TestCase):
    """A key of the platform replaced in the factory, by its operator: our Safe."""

    SAFE, FILLER, CLOSER, FEES = (to_checksum_address("0x" + x * 20) for x in ("5a", "f1", "c7", "fe"))
    NEW = to_checksum_address("0x" + "ab" * 20)
    RECORD = {"SimPoolFactory": FACTORY, "filler": FILLER, "checker": CHECKER}

    def now(self) -> dict:
        return {"filler": self.FILLER, "checker": CHECKER, "closer": self.CLOSER, "fee-recipient": self.FEES}

    def view(self, after: dict | None = None):
        state = dict(self.now())
        getters = {d.ROLES[r][1]: r for r in d.ROLES}

        def call_view(to, sig, types, values, out):
            return [self.SAFE] if sig == "operator()" else [state[getters[sig]]]
        return call_view, state

    def test_the_factorys_refusals_and_deploys_own(self):
        self.assertIn(f"{self.NEW} becomes the filler of the orders of every exam sold from now on. An exam already sold "
                      "keeps the filler it started with", d.role_check("filler", self.NEW, self.SAFE, self.SAFE, self.now()))
        for role, new, sender, why in (
                ("filler", self.NEW, self.FILLER, f"NotOperator: {self.FILLER} is not the factory's operator ({self.SAFE})"),
                ("checker", d.ZERO, self.SAFE, "BadRole: the checker is a real address, never 0x0"),
                ("filler", self.FILLER, self.SAFE, f"the factory's filler is {self.FILLER} already: nothing to send"),
                ("filler", CHECKER, self.SAFE, f"BadRole: {CHECKER} is the factory's checker"),
                ("checker", self.FILLER, self.SAFE, f"BadRole: {self.FILLER} is the factory's filler"),
                ("closer", CHECKER, self.SAFE, "the filler, the checker and the closer are three keys"),
                ("checker", self.SAFE, self.SAFE, "is the factory's operator: it opens our pools")):
            with self.subTest(why), self.assertRaises(SystemExit) as stop:
                d.role_check(role, new, self.SAFE, sender, self.now())
            self.assertIn(why, str(stop.exception))
        # The fee recipient is no key of the three: the operator itself may be it.
        self.assertIn("where the platform's fees go", d.role_check("fee-recipient", self.SAFE, self.SAFE, self.SAFE, self.now()))
        # Each role's setter and getter are the factory's own.
        factory = (ROOT / "src" / "sim" / "SimPoolFactory.sol").read_text()
        for role, (sig, getter, _) in d.ROLES.items():
            name, arg = sig.split("(")[0], sig.split("(")[1]
            self.assertEqual(arg, "address)")
            self.assertRegex(factory, rf"function {name}\(address \w+\) external onlyOperator \{{", role)
            self.assertIn(f"address public {getter[:-2]};", factory, role)

    def test_for_the_safe_it_prints_the_call_and_says_what_follows_on_the_host(self):
        view, _ = self.view()
        out = io.StringIO()
        with mock.patch.object(d.deployments, "load", return_value=self.RECORD), mock.patch.object(d.c, "call_view", side_effect=view), \
                mock.patch.object(d.c, "account") as key, mock.patch.object(d.c, "transact") as sent, contextlib.redirect_stdout(out):
            self.assertEqual(d.main(["role", "--deployment", "sim1", "--signer", self.SAFE, "--role", "filler",
                                     "--address", self.NEW]), 0)
        sent.assert_not_called()
        key.assert_not_called()
        said = out.getvalue()
        self.assertIn(f"factory {FACTORY}: setFiller({self.NEW}) from the signer ({self.SAFE}); the filler is {self.FILLER} now", said)
        self.assertIn("change `filler` in the record in a commit", said)
        (tx,) = json.loads(said[said.index('{\n "version"'):])["transactions"]
        self.assertEqual(tx, {"to": FACTORY, "value": "0", "data": "0x" + (
            keccak(text="setFiller(address)")[:4] + bytes(12) + bytes.fromhex(self.NEW[2:])).hex()})
        for role, selector in (("checker", "setChecker(address)"), ("closer", "setCloser(address)"),
                               ("fee-recipient", "setFeeRecipient(address)")):
            out = io.StringIO()
            with mock.patch.object(d.deployments, "load", return_value=self.RECORD), \
                    mock.patch.object(d.c, "call_view", side_effect=view), contextlib.redirect_stdout(out):
                d.main(["role", "--deployment", "sim1", "--signer", self.SAFE, "--role", role, "--address", self.NEW])
            said = out.getvalue()
            self.assertIn(keccak(text=selector)[:4].hex(), said, role)
            self.assertEqual("in the record in a commit" in said, role == "checker", role)

    def test_from_the_operators_key_it_sends_with_send_and_reads_the_role_back(self):
        view, state = self.view()
        with mock.patch.object(d.deployments, "load", return_value=self.RECORD), mock.patch.object(d.c, "call_view", side_effect=view), \
                mock.patch.object(d.c, "address_of", return_value=self.SAFE), mock.patch.object(d.c, "assert_testnet"), \
                mock.patch.object(d.c, "account"), contextlib.redirect_stdout(io.StringIO()) as out:
            argv = ["role", "--deployment", "sim1", "--key", "deployer", "--role", "closer", "--address", self.NEW]
            with mock.patch.object(d.c, "transact") as sent:
                self.assertEqual(d.main(argv), 0)
                sent.assert_not_called()
            took = lambda *a, **k: state.update(closer=self.NEW) or {"transactionHash": "0x01"}  # noqa: E731
            with mock.patch.object(d.c, "transact", side_effect=took) as sent:
                self.assertEqual(d.main(argv + ["--send"]), 0)
                self.assertEqual(sent.call_args[0][1:], (FACTORY, "setCloser(address)", ["address"], [self.NEW]))
            self.assertIn(f"deploy_sim: the factory's closer is {self.NEW}, tx 0x01", out.getvalue())
            state.update(closer=self.CLOSER)
            with mock.patch.object(d.c, "transact", return_value={"transactionHash": "0x02"}):
                with self.assertRaisesRegex(SystemExit, f"after setCloser the factory's closer reads {self.CLOSER}"):
                    d.main(argv + ["--send"])


if __name__ == "__main__":
    unittest.main()
