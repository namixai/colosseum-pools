"""The terms of a pool of many traders, every clause written out from the plan and the deployment's
bounds. The text is kept on chain (`TermsBook`) under its keccak256; the pool names the hash, and
whoever accepts the terms names it in their own call. Anyone can render the text again from the
plan the pool holds and the template version below, and compare the hash.

Every number in a clause is a parameter of the contract, and the clause says which: `[plan.pool.
term_days]` is the plan file's key, and the value beside it is what the contract was given. The
money and risk clauses, D1 to D11, are the CFO's text (INBOX 2026-10-15T13-46-24, section 1),
approved by Alex on 16 October 2026, here word for word in English. Two changes, each on the CTO's
word: the numbers of D5 are the deployment's bounds, which are the CFO's 14 and 90 days in the
product; and D8 states the free exams' second limit, a quarter of the seats at once, as the code
has it. docs/POOL-TERMS.md has them in Russian.

Template 4 (17 October 2026): money leaves at the end of the term or after it, at the pool's value
then, never in its last days, as D5 and D6 say and the code now does (Alex, "делаем А", DECISIONS
103; the deep audit's F38). Template 3 let it leave in the last days with an extension. Also in 4:
D12, the CFO's clause on referral payouts; the funded account's assets (the deep audit's F27: two
plans that differed only there had the same terms); and a shared pool's second line, which names
its raise (N5).
"""

from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak

TEMPLATE_VERSION = "4"
DAY = 86_400
# PoolShares and FreeExams constants the clauses state; a test holds them against the source.
RAISE_TIME = 30 * DAY
RAISE_FEE_BPS = 50
VALUE_PER_FREE_EXAM_USDC = 10_000


def _days(seconds: int) -> str:
    return f"{seconds / DAY:g} days"


def _clause(n: int, text: str, *params: tuple[str, object]) -> str:
    named = "; ".join(f"{k} = {v}" for k, v in params)
    return f"{n}. {text}" + (f" [{named}]" if named else "")


def money_and_risk(raw: dict, notice: int, renewal: int) -> list[str]:
    """The CFO's clauses D1 to D12, word for word, each naming the settings it states and their values."""
    share, seats = raw["funded"]["trader_share_bps"], raw["pool"]["max_seats"]
    lev = (raw["exam_rules"]["max_leverage_x100"], raw["funded_rules"]["max_leverage_x100"])
    return [
        "D1. The return shown when the pool is created is a model estimate, not a promise or a guarantee. It is "
        "not based on live trading with real money yet. The actual result can be below the lower number and can "
        "be a loss; you may get back less than you put in.",
        "D2. How many traders buy challenges; how they trade and how many pass; market moves — on a sharp day a "
        "stop can fill beyond the drawdown line; the pool's terms — stricter terms may attract fewer traders than "
        "the estimate assumes; the term and the size of the pool; the exchange and the network working as expected.",
        f"D3. Funded traders trade with the pool's money: their profit is shared with them at the share set in the "
        f"pool's terms; their loss stays with the pool. [plan.funded.trader_share_bps = {share}]",
        f"D4. Higher leverage carries the risk of larger losses. [plan.exam_rules.max_leverage_x100 = {lev[0]}; "
        f"plan.funded_rules.max_leverage_x100 = {lev[1]}]",
        f"D5. Your deposit is locked until the end of the pool's term; there is no early withdrawal. If you have not "
        f"said \"I'm leaving\" at least {_days(notice)} before the end of the term, your deposit stays in the pool for "
        f"another {_days(renewal)} — and so at the end of every following term. Deposits are in stablecoin only. "
        f"[bounds.notice = {notice}; bounds.renewal = {renewal}]",
        "D6. If you said \"I'm leaving\", your share is paid out at the end of the term at the pool's value at that "
        "moment, less the withdrawal fee; a pool with several investors keeps working. A single-investor pool ends "
        "only when its owner says \"stop\": no new challenges are sold, positions are closed and profit is split. If "
        "at the end of a term there is not enough money to pay those leaving, the pool stops in the same way.",
        f"D7. The creator sets the amount to raise; the raise lasts {RAISE_TIME // DAY} days. If the amount is "
        f"reached, the pool starts and its term begins. If not, every deposit is returned less "
        f"{RAISE_FEE_BPS / 100:g} % — the platform's withdrawal fee. [PoolShares.RAISE_TIME = {RAISE_TIME}; "
        f"PoolShares.RAISE_FEE_BPS = {RAISE_FEE_BPS}]",
        f"D8. The pool's creator may give away free challenges — no more than 10 a month for every "
        f"${10 * VALUE_PER_FREE_EXAM_USDC:,} in the pool, and no more than a quarter of the pool's seats at a time. "
        f"The pool earns nothing on a free challenge, while the result of a trader who passes it falls on the pool "
        f"as with a paid one; the seat is taken from the start of the challenge. "
        f"[FreeExams.VALUE_PER_FREE_EXAM = {VALUE_PER_FREE_EXAM_USDC} USDC; plan.pool.max_seats = {seats}, "
        f"a quarter = {seats // 4}]",
        "D9. The owner of a single-investor pool and the creator of a pool with several investors set its terms "
        "themselves. A depositor in a pool with several investors accepts the terms set by the creator by signing "
        "when depositing.",
        "D10. The platform takes 0.5 % of each challenge price and 0.5 % of every withdrawal, including the return "
        "of a deposit when a raise falls short; both are already in the estimate.",
        "D11. You alone are responsible for your money.",
        # Accepted on Alex's word of 16 October 2026 (DECISIONS 100, 103), the CFO's text in
        # _hub/FINANCE/TERMS-EDITS-2026-10-16.md. No setting yet: the pool does not pay referrers,
        # and the clause names its settings once it does.
        "D12. Referral payouts. The owner of a single-investor pool, or the creator of a pool with several "
        "investors, may pay referrers out of the pool's money. The payout (a share of the exam price) and the "
        "yearly cap on payouts are set when the pool is created. They are written in the pool's terms, you see "
        "them before you deposit, and they do not change during the term. These payouts reduce what the pool "
        "earns from exams.",
    ]


def shares_name(shares: dict) -> bytes:
    """The hash the second line of a shared pool's terms names: its raise, the amount and the
    smallest deposit (1e8 = 1 USDC), as `PoolShares.bind` works it out (deep audit N5)."""
    return keccak(abi_encode(["uint256", "uint256"],
                             [round(shares["target_usdc"] * 10**8), round(shares["min_deposit_usdc"] * 10**8)]))


def render(raw: dict, bounds: tuple[int, int, int, int], plan_name: bytes, shares: dict | None = None) -> str:
    """The terms as text. `raw` is the plan file, `bounds` the deployment's (seconds: shortest and
    longest term, notice, renewal), `plan_name` the hash of the plan as the pool is given it with its
    two terms fields zero (`deploy_multi.plan_name`), `shares` the pool of several investors' own
    settings when there is one (`target_usdc`, `min_deposit_usdc`). The text begins `Plan
    0x<plan_name>`: the pool takes no terms that do not begin with its own plan's (deep audit F13).
    A shared pool's second line is `Shares 0x<shares_name>`: its shares contract takes no terms
    written for another raise (deep audit N5)."""
    min_term, max_term, notice, renewal = bounds
    e, f, p = raw["exam"], raw["funded"], raw["pool"]
    free = raw.get("free", {"per_month": 0, "inviter": None})
    lines = [f"Plan 0x{plan_name.hex()}"] + ([f"Shares 0x{shares_name(shares).hex()}"] if shares is not None else []) + [
        f"Terms of a pool of many traders on Hyperliquid, template {TEMPLATE_VERSION}.",
        "Each clause names the setting of the contract it states, and its value.", ""]
    n = 0

    def add(text: str, *params: tuple[str, object]) -> None:
        nonlocal n
        n += 1
        lines.append(_clause(n, text, *params))

    add("The money is USDC and nothing else; the pool counts no other token and sends no other out.")
    add(f"The term is {p['term_days']:g} days from the moment the owner starts the pool, within this "
        f"deployment's bounds of {_days(min_term)} to {_days(max_term)}.",
        ("plan.pool.term_days", p["term_days"]), ("bounds.min_term", min_term), ("bounds.max_term", max_term))
    add(f"A term nobody stops runs on for {_days(renewal)}, again and again, on the same terms.",
        ("bounds.renewal", renewal))
    add(f"The money is locked for the term. To leave, say so no later than {_days(notice)} before the end "
        "of the term; in those last days what was said holds. Money leaves at the end of the term or after "
        "it, at the pool's value then, and never in those last days.", ("bounds.notice", notice))
    add("When the owner says the pool stops, nothing more is sold, and the term ends no earlier than the "
        "last exam sold has had all its time and its trader the time to take the account.")
    menu = ", ".join(f"{m['size_usdc']:g} for {m['price_usdc']:g}" for m in raw["menu"])
    add(f"Exams are sold off a menu of sizes and prices, in USDC: {menu}.", ("plan.menu", menu))
    r = raw["exam_rules"]
    add(f"An exam is traded on paper at the chain's oracle price, filled by anyone but its trader: a daily loss of {r['daily_loss_bps']} bps, a drawdown "
        f"of {r['max_drawdown_bps']} bps and leverage of {r['max_leverage_x100'] / 100:g}x at most, on "
        f"{', '.join(r['assets'])}.",
        ("plan.exam_rules.daily_loss_bps", r["daily_loss_bps"]), ("plan.exam_rules.max_drawdown_bps", r["max_drawdown_bps"]),
        ("plan.exam_rules.max_leverage_x100", r["max_leverage_x100"]), ("plan.exam_rules.assets", "/".join(r["assets"])))
    add(f"An exam passes on targets of {', '.join(str(t) for t in e['targets_bps'])} bps, each step within "
        f"{e['step_days']:g} days; a fill costs {e['cost_ppm']} millionths of its notional.",
        ("plan.exam.targets_bps", "/".join(str(t) for t in e["targets_bps"])), ("plan.exam.step_days", e["step_days"]),
        ("plan.exam.cost_ppm", e["cost_ppm"]), ("plan.exam.max_idle_days", e["max_idle_days"]),
        ("plan.exam.min_trading_days", e["min_trading_days"]), ("plan.exam.fill_delay_blocks", e["fill_delay_blocks"]),
        ("plan.exam.fill_window_blocks", e["fill_window_blocks"]))
    fr = raw["funded_rules"]
    add(f"A trader who passes takes a funded account of the size bought, on the pool's money, within "
        f"{p['claim_window_days']:g} days, under a daily loss of {fr['daily_loss_bps']} bps, a drawdown of "
        f"{fr['max_drawdown_bps']} bps and leverage of {fr['max_leverage_x100'] / 100:g}x at most, on "
        f"{', '.join(fr['assets'])}.",
        ("plan.pool.claim_window_days", p["claim_window_days"]), ("plan.funded_rules.daily_loss_bps", fr["daily_loss_bps"]),
        ("plan.funded_rules.max_drawdown_bps", fr["max_drawdown_bps"]),
        ("plan.funded_rules.max_leverage_x100", fr["max_leverage_x100"]),
        ("plan.funded_rules.assets", "/".join(fr["assets"])))
    add(f"The trader's share of what a funded account makes is {f['trader_share_bps']} bps; at a target of "
        f"{f['target_bps']} bps the account {f['after_target'].replace('_', ' ')}.",
        ("plan.funded.trader_share_bps", f["trader_share_bps"]), ("plan.funded.target_bps", f["target_bps"]),
        ("plan.funded.after_target", f["after_target"]), ("plan.funded.max_retakes", f["max_retakes"]))
    add(f"A funded account that has not traded for {f['idle_days']:g} days may be closed by anyone, without a "
        "breach, its trader paid their share. A trade made inside the day is counted only when the noter "
        "named here records it.", ("plan.funded.idle_days", f["idle_days"]),
        ("plan.funded.noter", f.get("noter") or "nobody"))
    add(f"No more than {p['in_work_bps']} bps of the pool's value, less what is to leave, is spoken for at "
        f"once, in at most {p['max_seats']} sizes.", ("plan.pool.in_work_bps", p["in_work_bps"]),
        ("plan.pool.max_seats", p["max_seats"]))
    add(f"{free['per_month']} exams a month may be given away free, on invitations the inviter signs, no "
        "more than ten a month for every 100,000 USDC in the pool, and no more than a quarter of the seats "
        f"({p['max_seats'] // 4}) running free at once. Who is invited is the inviter's choice.",
        ("plan.free.per_month", free["per_month"]), ("plan.free.inviter", free.get("inviter") or "nobody"),
        ("plan.pool.max_seats", p["max_seats"]))
    if shares is not None:
        add(f"The pool is shared: investors deposit USDC for shares, {shares['min_deposit_usdc']:g} USDC at the least. "
            f"A raise collects {shares['target_usdc']:g} USDC within 30 days; short of it, every deposit is handed "
            "back less the platform's 0.5 %.",
            ("shares.target_usdc", shares["target_usdc"]), ("shares.min_deposit_usdc", shares["min_deposit_usdc"]))
        add("An investor who does not say they leave stays for the next term. Those who said so are paid "
            "at the end of the term or after it, what their shares are worth then; if the pool cannot pay "
            "them then, it stops and pays them once its accounts are closed.")
    lines += ["", "Money and risk (the CFO's clauses, approved by Alex on 16 October 2026):"]
    lines += money_and_risk(raw, notice, renewal)
    return "\n".join(lines) + "\n"


def terms_hash(text: str) -> bytes:
    return keccak(text.encode())

