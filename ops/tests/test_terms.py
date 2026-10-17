"""ops/terms.py: every clause written from a setting of the contract, named beside its value."""
import copy
import json
import pathlib
import unittest

from eth_utils import keccak

from ops import deploy_multi as d
from ops import terms as t

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLAN = json.loads((ROOT / "ops" / "plans" / "multi-testnet-run.json").read_text())
BOUNDS = d.bounds_of(180, 730, 14)
NAME = b"\x11" * 32  # a plan's hash, as deploy_multi.plan_name gives it
SHARES = (ROOT / "src/multi/PoolShares.sol").read_text()
FREE = (ROOT / "src/multi/FreeExams.sol").read_text()

# The CFO's English, as approved by Alex on 16 October 2026 (INBOX 2026-10-15T13-46-24, section 1),
# with the product's bounds; D8 with the quarter the CTO asked for, as the code has it.
CFO_EN = [
    "The return shown when the pool is created is a model estimate, not a promise or a guarantee. It is not based "
    "on live trading with real money yet. The actual result can be below the lower number and can be a loss; you "
    "may get back less than you put in.",
    "How many traders buy challenges; how they trade and how many pass; market moves — on a sharp day a stop can "
    "fill beyond the drawdown line; the pool's terms — stricter terms may attract fewer traders than the estimate "
    "assumes; the term and the size of the pool; the exchange and the network working as expected.",
    "Funded traders trade with the pool's money: their profit is shared with them at the share set in the pool's "
    "terms; their loss stays with the pool.",
    "Higher leverage carries the risk of larger losses.",
    "Your deposit is locked until the end of the pool's term; there is no early withdrawal. If you have not said "
    "\"I'm leaving\" at least 14 days before the end of the term, your deposit stays in the pool for another 90 days "
    "— and so at the end of every following term. Deposits are in stablecoin only.",
    "If you said \"I'm leaving\", your share is paid out at the end of the term at the pool's value at that moment, "
    "less the withdrawal fee; a pool with several investors keeps working. A single-investor pool ends only when "
    "its owner says \"stop\": no new challenges are sold, positions are closed and profit is split. If at the end of "
    "a term there is not enough money to pay those leaving, the pool stops in the same way.",
    "The creator sets the amount to raise; the raise lasts 30 days. If the amount is reached, the pool starts and "
    "its term begins. If not, every deposit is returned less 0.5 % — the platform's withdrawal fee.",
    "The pool's creator may give away free challenges — no more than 10 a month for every $100,000 in the pool, "
    "and no more than a quarter of the pool's seats at a time. The pool earns nothing on a free challenge, while "
    "the result of a trader who passes it falls on the pool as with a paid one; the seat is taken from the start "
    "of the challenge.",
    "The owner of a single-investor pool and the creator of a pool with several investors set its terms "
    "themselves. A depositor in a pool with several investors accepts the terms set by the creator by signing "
    "when depositing.",
    "The platform takes 0.5 % of each challenge price and 0.5 % of every withdrawal, including the return of a "
    "deposit when a raise falls short; both are already in the estimate.",
    "You alone are responsible for your money.",
    "Referral payouts. The owner of a single-investor pool, or the creator of a pool with several investors, may pay "
    "referrers out of the pool's money. The payout (a share of the exam price) and the yearly cap on payouts are set "
    "when the pool is created. They are written in the pool's terms, you see them before you deposit, and they do "
    "not change during the term. These payouts reduce what the pool earns from exams.",
]


class Terms(unittest.TestCase):
    def test_every_setting_is_named_with_its_value(self):
        text = t.render(PLAN, BOUNDS, NAME)
        for key, value in (("plan.pool.term_days", PLAN["pool"]["term_days"]), ("bounds.renewal", 90 * 86_400),
                           ("bounds.notice", 14 * 86_400), ("plan.pool.in_work_bps", PLAN["pool"]["in_work_bps"]),
                           ("plan.funded.idle_days", PLAN["funded"]["idle_days"]),
                           ("plan.free.per_month", PLAN["free"]["per_month"]),
                           ("plan.exam.cost_ppm", PLAN["exam"]["cost_ppm"])):
            self.assertIn(f"{key} = {value}", text)
        # Each in its own clause, not only somewhere: D5 names the renewal too.
        self.assertIn("runs on for 90 days, again and again, on the same terms. [bounds.renewal = 7776000]", text)
        self.assertTrue(text.split("\n")[1].startswith(f"Terms of a pool of many traders on Hyperliquid, template {t.TEMPLATE_VERSION}."))

    def test_a_different_setting_is_different_terms(self):
        base = t.terms_hash(t.render(PLAN, BOUNDS, NAME))
        for path, value in (("pool.term_days", 0.5), ("pool.in_work_bps", 7000), ("funded.trader_share_bps", 7000),
                            ("exam.cost_ppm", 950), ("free.per_month", 3), ("funded_rules.assets", ["ETH"])):
            raw = copy.deepcopy(PLAN)
            node = raw
            *parents, leaf = path.split(".")
            for k in parents:
                node = node[k]
            node[leaf] = value
            self.assertNotEqual(t.terms_hash(t.render(raw, BOUNDS, NAME)), base, path)
        self.assertNotEqual(t.terms_hash(t.render(PLAN, d.bounds_of(180, 730, 14, 120), NAME)), base, "the renewal")

    def test_the_cfos_clauses_are_word_for_word(self):
        text = t.render(PLAN, BOUNDS, NAME)
        for i, clause in enumerate(CFO_EN, 1):
            self.assertIn(f"D{i}. {clause}", text)
        self.assertNotIn("pending", text)
        self.assertEqual(t.TEMPLATE_VERSION, "4")

    def test_the_cfos_numbers_are_the_deployments(self):
        text = t.render(PLAN, d.bounds_of(180, 730, 7, 45), NAME)
        self.assertIn("at least 7 days before the end of the term, your deposit stays in the pool for another 45 days",
                      text)
        self.assertIn(f"bounds.notice = {7 * 86_400}; bounds.renewal = {45 * 86_400}", text)
        self.assertIn(f"plan.funded.trader_share_bps = {PLAN['funded']['trader_share_bps']}", text)
        self.assertIn(f"plan.pool.max_seats = {PLAN['pool']['max_seats']}, a quarter = {PLAN['pool']['max_seats'] // 4}",
                      text)

    def test_the_constants_the_clauses_state_are_the_contracts(self):
        self.assertIn("uint32 public constant RAISE_TIME = 30 days;", SHARES)
        self.assertEqual(t.RAISE_TIME, 30 * 86_400)
        self.assertIn(f"uint16 public constant RAISE_FEE_BPS = {t.RAISE_FEE_BPS};", SHARES)
        self.assertIn(f"uint256 public constant VALUE_PER_FREE_EXAM = {t.VALUE_PER_FREE_EXAM_USDC:_}e8;", FREE)

    def test_free_exams_are_stated_with_their_limits(self):
        raw = copy.deepcopy(PLAN)
        raw["free"] = {"per_month": 3, "inviter": "0xABaBaBaBABabABabAbAbABAbABabababaBaBABaB"}
        raw["pool"]["max_seats"] = 8
        text = t.render(raw, BOUNDS, NAME)
        self.assertIn("3 exams a month may be given away free", text)
        self.assertIn("no more than a quarter of the seats (2) running free at once", text)

    def test_the_terms_begin_with_the_plans_own_hash(self):
        text = t.render(PLAN, BOUNDS, NAME)
        self.assertTrue(text.startswith("Plan 0x" + "11" * 32 + "\nTerms of a pool of many traders on Hyperliquid, "
                                        "template 4."), text[:120])
        # The hash is the plan's as the pool is given it, its terms fields left out: what
        # MultiPool._checkTerms works out (deep audit F13).
        run = d.bounds_of(0.125, 1, 0.0104166667)  # the testnet plan's own deployment
        plan = d.plan_of(PLAN, run)
        self.assertEqual(d.plan_name(plan), d.plan_name(d.plan_of(PLAN, run, (b"\x33" * 32, "0x" + "44" * 20))))
        other = copy.deepcopy(PLAN)
        other["funded"]["trader_share_bps"] = 9_000
        self.assertNotEqual(d.plan_name(plan), d.plan_name(d.plan_of(other, run)))
        src = (ROOT / "src/multi/MultiPool.sol").read_text()
        self.assertIn('abi.encodePacked("Plan ", Strings.toHexString(uint256(keccak256(abi.encode(p))), 32), "\\n")', src)

    def test_a_shared_pool_states_its_raise(self):
        text = t.render(PLAN, BOUNDS, NAME, shares={"target_usdc": 100_000, "min_deposit_usdc": 100})
        self.assertIn("shares.target_usdc = 100000", text)
        self.assertIn("shares.min_deposit_usdc = 100", text)
        self.assertIn("less the platform's 0.5 %", text)
        self.assertNotIn("shares.target_usdc", t.render(PLAN, BOUNDS, NAME))

    def test_deep_N5_a_shared_pools_second_line_names_its_raise(self):
        # PoolShares.bind takes no terms whose second line is not `Shares 0x` and the hash of its own
        # amount and smallest deposit, both in 1e8 units, encoded as two uint256.
        shares = {"target_usdc": 100_000, "min_deposit_usdc": 100}
        lines = t.render(PLAN, BOUNDS, NAME, shares=shares).split("\n")
        self.assertEqual(lines[1], "Shares 0x" + t.shares_name(shares).hex())
        self.assertTrue(lines[2].startswith(f"Terms of a pool of many traders on Hyperliquid, template {t.TEMPLATE_VERSION}."))
        self.assertEqual(t.shares_name(shares), keccak((100_000 * 10**8).to_bytes(32, "big") + (100 * 10**8).to_bytes(32, "big")))
        self.assertNotEqual(t.shares_name(shares), t.shares_name({**shares, "target_usdc": 0}))
        self.assertNotEqual(t.shares_name(shares), t.shares_name({**shares, "min_deposit_usdc": 1}))
        src = (ROOT / "src/multi/PoolShares.sol").read_text()
        self.assertIn('abi.encodePacked("Shares ", Strings.toHexString(uint256(keccak256(abi.encode(target, uint256(minDeposit)))), 32), "\\n")', src)
        self.assertIn("if (text[72 + i] != head[i]) revert WrongTerms();", src)


if __name__ == "__main__":
    unittest.main()
