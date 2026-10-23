"""The terms of a pool of many traders, every clause written out from the plan and the deployment's
bounds. The text is kept on chain (`TermsBook`) under its keccak256; the pool names the hash, and
whoever accepts the terms names it in their own call. Anyone can render the text again from the
plan the pool holds and the template version below, and compare the hash.

Every number in a clause is a parameter of the contract, and the clause says which: `[plan.pool.
term_days]` is the plan file's key, and the value beside it is what the contract was given. The
money and risk clauses are the CFO's text, approved by Alex on 14 October 2026; until it is put in,
the section says so, and a deployment records that its terms are pending.
"""

from __future__ import annotations

from eth_utils import keccak

TEMPLATE_VERSION = "1"
DAY = 86_400
CFO_PENDING = "[pending: the CFO's footnote and six conditions, approved by Alex on 14 October 2026, go here word for word]"


def _days(seconds: int) -> str:
    return f"{seconds / DAY:g} days"


def _clause(n: int, text: str, *params: tuple[str, object]) -> str:
    named = "; ".join(f"{k} = {v}" for k, v in params)
    return f"{n}. {text}" + (f" [{named}]" if named else "")


def render(raw: dict, bounds: tuple[int, int, int, int], shares: dict | None = None,
           cfo: list[str] | None = None) -> str:
    """The terms as text. `raw` is the plan file, `bounds` the deployment's (seconds: shortest and
    longest term, notice, renewal), `shares` the pool of several investors' own settings when there
    is one (`target_usdc`), `cfo` the CFO's clauses once given."""
    min_term, max_term, notice, renewal = bounds
    e, f, p = raw["exam"], raw["funded"], raw["pool"]
    free = raw.get("free", {"per_month": 0, "inviter": None})
    lines = [f"Terms of a pool of many traders on Hyperliquid, template {TEMPLATE_VERSION}.",
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
        "of the term; in those last days what was said holds. Money leaves at the end, or, in those last "
        "days, together with an extension.", ("bounds.notice", notice))
    add("When the owner says the pool stops, nothing more is sold, and the term ends no earlier than the "
        "last exam sold has had all its time and its trader the time to take the account.")
    menu = ", ".join(f"{m['size_usdc']:g} for {m['price_usdc']:g}" for m in raw["menu"])
    add(f"Exams are sold off a menu of sizes and prices, in USDC: {menu}.", ("plan.menu", menu))
    r = raw["exam_rules"]
    add(f"An exam is traded on paper at the chain's mark: a daily loss of {r['daily_loss_bps']} bps, a drawdown "
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
        f"{fr['max_drawdown_bps']} bps and leverage of {fr['max_leverage_x100'] / 100:g}x at most.",
        ("plan.pool.claim_window_days", p["claim_window_days"]), ("plan.funded_rules.daily_loss_bps", fr["daily_loss_bps"]),
        ("plan.funded_rules.max_drawdown_bps", fr["max_drawdown_bps"]),
        ("plan.funded_rules.max_leverage_x100", fr["max_leverage_x100"]))
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
        add(f"The pool is shared: investors deposit USDC for shares. A raise collects {shares['target_usdc']:g} USDC "
            "within 30 days; short of it, every deposit is handed back less the platform's 0.5 %.",
            ("shares.target_usdc", shares["target_usdc"]))
        add("An investor who does not say they leave stays for the next term. Those who said so are paid "
            "what their shares are worth when the round closes; if the pool cannot pay them, it stops at "
            "the end of its term and pays them then.")
    lines += ["", "Money and risk (the CFO's text):"]
    lines += cfo if cfo else [CFO_PENDING]
    return "\n".join(lines) + "\n"


def terms_hash(text: str) -> bytes:
    return keccak(text.encode())


def pending(text: str) -> bool:
    return CFO_PENDING in text
