#!/usr/bin/env python3
"""Стресс-тест правил пула Colosseum на настоящих обвальных сутках (минутки).

Правила пула (контракт `RuledAccount`, структура `Rules`): дневной убыток 3 % от снимка на полночь
UTC, статическая просадка 6 % от капитала места, плечо до 5×. Правила исполняет сторож: он опрашивает
состояние раз в 30 с и закрывает позицию рыночным IOC. Между пробитием линии и закрытием цена уходит
дальше — этот перелёт модель экономики пулов берёт допущением (`Frictions.overshoot_mean = 0.5 %`
эквити). Здесь он меряется на настоящих обвалах, а не предполагается.

Что считается. Место с капиталом F. На входе трейдер держит номинал `плечо × эквити` одной монетой.
Эквити(P) = E_вход × (1 + сторона × плечо × (P/P_вход − 1)). Линия закрытия — та, что достигается
раньше (выше по эквити): дневная `снимок × (1 − 3 %)` или статическая `F × (1 − 6 %)`.

Задержка сторожа. Минутные свечи не разрешают 30 с: пробитие внутри минуты видно только по её
минимуму (лонг) или максимуму (шорт). Поэтому считаются три варианта, и они ограничивают истину
сверху и снизу:
  `--lag 0` — закрытие ровно по цене линии (идеальный сторож, перелёта нет; так считает модель);
  `--lag 1` — закрытие по ОТКРЫТИЮ следующей минуты (задержка от 30 до 90 с);
  `--lag 2` — по открытию через минуту (сторож проспал один опрос).

🔴 `--lag 1` — НЕ верхняя граница. Открытие следующей минуты иногда приходится на отскок, и место
закрывается лучше линии, а то и в плюс (так на 10.10.2025 вышло с BTC: минута 21:18 упала до
−1.3 % от входа и закрылась выше входа). Поэтому у исполнения два режима:
  `--exec open` (по умолчанию) — по открытию минуты k: что сторож увидит, то и возьмёт;
  `--exec worst` — по худшей цене внутри окна задержки: сторожу не повезло с моментом опроса.
Верхняя граница — `worst`, нижняя — `--lag 0`. Настоящий сторож между ними.

Закрытие платит комиссию тейкера на весь номинал (`--taker-bps`, Hyperliquid 4.5 bps).

Биржа впереди сторожа. Если эквити падает ниже поддерживающей маржи, позицию закрывает не сторож, а
ликвидация биржи: правила пула в этот момент уже не действуют. Порог — `--mm` доля номинала (по
умолчанию 2 %). Что остаётся месту — вилка по документации Hyperliquid: ликвидация через книгу
возвращает остаток обеспечения (поддерживающую маржу), резервная ликвидация его не возвращает.
Нижняя граница (`--exec open`) оставляет месту маржу, верхняя (`--exec worst`) — место теряет всё.

Край суток. Если линия пробита так поздно, что минуты `j + lag` в сутках уже нет, выход считается по
закрытию последней минуты (последняя цена, которую сторож увидит), а не по её открытию, которое стоит
до пробития.

    python3 pool_stress.py --days 2025-10-10 --entries hour          # один день, вход каждый час
    python3 pool_stress.py --top 8 --entries hour --json итог.json   # восемь худших суток с 10.10.2025
    python3 pool_stress.py --days 2025-10-10 --entries minute --seats-report
"""

from __future__ import annotations

import argparse
import calendar
import csv
import glob
import json
import os
import sys
import time

import numpy as np

# Каталог с минутками. По умолчанию — слепок рядом с этим файлом (`data/`), тот самый, на
# котором считаны числа README. `--data-dir` переводит на любой другой; формат — CSV
# `ts,open,high,low,close` (слепок) или `.npz` с теми же полями (исходный формат автора).
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DAY = 86_400
POOL_SEATS = (50_000.0, 25_000.0, 5_000.0, 5_000.0, 5_000.0)      # пул $100 тыс.: места (запас $10 тыс.)
SINCE = "2025-10-10"                                               # обвалы считаем с этой даты (наряд CTO)


def day_ts(d: str) -> int:
    return calendar.timegm(time.strptime(d, "%Y-%m-%d"))


def day_name(t: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))


_CSV_CACHE: dict[str, dict] = {}


def read_symbol(sym: str) -> dict | None:
    """Все минутки монеты: CSV слепка или npz автора. Читается один раз на прогон."""
    if sym in _CSV_CACHE:
        return _CSV_CACHE[sym]
    csv_path, npz_path = os.path.join(DATA, f"{sym}.csv"), os.path.join(DATA, f"{sym}.npz")
    if os.path.exists(csv_path):
        ts, o, h, l, c = [], [], [], [], []
        with open(csv_path, encoding="utf-8") as f:
            r = csv.reader(f)
            head = next(r, None)
            if head != ["ts", "open", "high", "low", "close"]:
                raise SystemExit(f"{csv_path}: ждал колонки ts,open,high,low,close, а там {head}")
            for row in r:
                ts.append(int(row[0])); o.append(float(row[1])); h.append(float(row[2]))
                l.append(float(row[3])); c.append(float(row[4]))
        z = {"ts": np.array(ts, dtype=np.int64), "open": np.array(o), "high": np.array(h),
             "low": np.array(l), "close": np.array(c)}
    elif os.path.exists(npz_path):
        z = dict(np.load(npz_path))
    else:
        return None
    _CSV_CACHE[sym] = z
    return z


def load_day(sym: str, t0: int) -> dict | None:
    """Минутки монеты за сутки [t0, t0+24ч): ts, open, high, low, close. None — суток нет в данных."""
    z = read_symbol(sym)
    if z is None:
        return None
    ts = z["ts"].astype(np.int64)
    k = (ts >= t0) & (ts < t0 + DAY)
    if k.sum() < 60:
        return None
    return {"ts": ts[k], "o": z["open"][k].astype(float), "h": z["high"][k].astype(float),
            "l": z["low"][k].astype(float), "c": z["close"][k].astype(float)}


def symbols() -> list[str]:
    found = {os.path.basename(p)[:-4] for p in glob.glob(os.path.join(DATA, "*.npz"))}
    found |= {os.path.basename(p)[:-4] for p in glob.glob(os.path.join(DATA, "*.csv"))}
    return sorted(found)


def line_price(px_in: float, side: int, lev: float, loss_frac: float) -> float:
    """Цена, при которой эквити падает на loss_frac от эквити входа при плече lev."""
    return px_in * (1.0 - side * loss_frac / lev)


def equity(px: float, px_in: float, side: int, lev: float, eq_in: float) -> float:
    return eq_in * (1.0 + side * lev * (px / px_in - 1.0))


def liq_price(px_in: float, side: int, lev: float, mm: float) -> float:
    """Цена ликвидации биржей: эквити / номинал = mm.

    эквити = E(1 + L·r), номинал = L·E·(1 + r) ⇒ (1 + L·r) = mm·L·(1 + r) ⇒ r = (mm·L − 1)/(L − mm·L).
    Для шорта номинал растёт при росте цены, знак r зеркальный.
    """
    r = (mm * lev - 1.0) / (lev - mm * lev) if side > 0 else (1.0 - mm * lev) / (lev + mm * lev)
    return px_in * (1.0 + r)


def episode(bar: dict, i: int, side: int, *, lev: float, daily_loss: float, max_dd: float,
            snapshot: float, start_eq: float, lag: int, taker_bps: float, mm: float,
            exec_mode: str = "open") -> dict:
    """Одно место, вход по открытию минуты i, выход по правилу пула, по ликвидации или в конце суток.

    snapshot — эквити на полночь UTC (база дневного убытка), start_eq — капитал места (база просадки).
    Возвращает доли от капитала места: сколько потеряно, где стояла линия, каков перелёт.
    """
    o, h, l, ts = bar["o"], bar["h"], bar["l"], bar["ts"]
    n = len(o)
    px_in = float(o[i])
    eq_in = start_eq                                        # место входит в сделку целым капиталом
    line_eq = max(snapshot * (1.0 - daily_loss), start_eq * (1.0 - max_dd))
    if line_eq >= eq_in:                                    # линия уже пробита до входа — сделки нет
        return {"вход": None}
    loss_to_line = 1.0 - line_eq / eq_in
    p_line = line_price(px_in, side, lev, loss_to_line)
    p_liq = liq_price(px_in, side, lev, mm)
    adverse = l if side > 0 else h
    hit = np.flatnonzero((adverse[i:] <= p_line) if side > 0 else (adverse[i:] >= p_line))
    fee = taker_bps / 1e4 * lev * eq_in                     # закрытие номинала по тейкеру
    if len(hit) == 0:                                       # правило не пробито — держим до конца суток
        eq_out = equity(float(bar["c"][n - 1]), px_in, side, lev, eq_in) - fee
        return {"вход": int(ts[i]), "пробито": False, "ликвидация": False,
                "итог_доля": eq_out / start_eq - 1.0, "линия_доля": -loss_to_line, "перелёт_доля": 0.0,
                "выход": int(ts[n - 1]), "минут_до_выхода": n - 1 - i}
    j = i + int(hit[0])
    if lag == 0:
        k, p_out, path_end = j, p_line, j
        if (side > 0 and float(o[j]) < p_line) or (side < 0 and float(o[j]) > p_line):
            p_out = float(o[j])                             # минута открылась за линией: раньше не исполнить
    elif j + lag <= n - 1:
        k, p_out, path_end = j + lag, float(o[j + lag]), j + lag
    else:
        # 🔴 Окно задержки выходит за край суток: минуты j + lag в данных нет. Открытие минуты j стоит
        # ДО пробития и выходом быть не может (так место выходило лучше линии — замечание ревью 25.09).
        # Берём закрытие последней минуты — последнюю цену, которую сторож увидит в этих сутках; путь
        # позиции при этом проходит минуту j целиком, включая её низ.
        k, p_out, path_end = n - 1, float(bar["c"][n - 1]), n
    if exec_mode == "worst" and lag:
        # Сторожу не повезло с моментом опроса: худшая цена внутри окна [j, k]. Это верхняя граница
        # перелёта; закрытие по открытию минуты k — не граница вовсе, оно бывает и лучше линии.
        bad = float(l[j:k + 1].min()) if side > 0 else float(h[j:k + 1].max())
        p_out = min(p_out, bad) if side > 0 else max(p_out, bad)
    # Биржа впереди сторожа: если цена прошла уровень ликвидации ДО момента закрытия, позицию снесло
    # раньше. Путь кончается на самом закрытии: минуты [i, path_end) целиком плюс цена выхода — всё, что
    # внутри минуты выхода после этой цены, случилось уже после выхода. Иначе при мгновенном стороже
    # (задержка 0) минимум минуты пробития засчитывался бы позиции, которой в ней уже нет.
    tail = p_out                                            # путь кончается на цене самого выхода
    if path_end <= i:                                       # вышли в минуту входа: путь — только выход
        deep = tail
    else:
        deep = min(float(l[i:path_end].min()), tail) if side > 0 else max(float(h[i:path_end].max()), tail)
    liquidated = bool((side > 0 and deep <= p_liq) or (side < 0 and deep >= p_liq))
    if liquidated:
        # Что остаётся месту после ликвидации — вилка, а не число. Документация Hyperliquid: при
        # ликвидации через книгу «any remaining collateral remains with the trader» (это поддерживающая
        # маржа, mm × номинал), при резервной ликвидации (эквити ниже 2/3 маржи) «the maintenance margin
        # is not returned to the user». Нижняя граница (`open`) оставляет маржу, верхняя (`worst`) — нет:
        # место теряет капитал целиком, комиссии платить не с чего.
        p_out = p_liq
        eq_out = 0.0 if exec_mode == "worst" else max(equity(p_liq, px_in, side, lev, eq_in) - fee, 0.0)
    else:
        eq_out = max(equity(p_out, px_in, side, lev, eq_in) - fee, 0.0)
    return {"вход": int(ts[i]), "пробито": True, "ликвидация": liquidated,
            "итог_доля": eq_out / start_eq - 1.0, "линия_доля": -loss_to_line,
            "перелёт_доля": max(line_eq - eq_out, 0.0) / start_eq,
            "выход": int(ts[k]), "минут_до_выхода": k - i}


def summarise(eps: list[dict], max_dd: float, label: str) -> dict:
    """Сводка по всем эпизодам: перелёт за линию и доля входов, где пул потерял больше разрешённого."""
    loss = np.array([-e["итог_доля"] for e in eps])
    over = np.array([e["перелёт_доля"] for e in eps if e["пробито"]])
    if not len(over):
        over = np.zeros(1)
    return {"сводка": label, "эпизодов": len(eps),
            "пробито_%": round(float(np.mean([e["пробито"] for e in eps])) * 100, 1),
            "перелёт_средн_пп": round(float(over.mean()) * 100, 2),
            "перелёт_медиана_пп": round(float(np.median(over)) * 100, 2),
            "перелёт_p95_пп": round(float(np.quantile(over, 0.95)) * 100, 2),
            "перелёт_p99_пп": round(float(np.quantile(over, 0.99)) * 100, 2),
            "перелёт_макс_пп": round(float(over.max()) * 100, 2),
            "убыток_средн_%": round(float(loss.mean()) * 100, 2),
            "убыток_макс_%": round(float(loss.max()) * 100, 2),
            "хуже_просадки_%": round(float((loss > max_dd + 1e-12).mean()) * 100, 2),
            "ликвидаций_%": round(float(np.mean([e["ликвидация"] for e in eps])) * 100, 2)}


def entries(n: int, mode: str) -> list[int]:
    if mode == "minute":
        return list(range(n))
    if mode == "hour":
        return list(range(0, n, 60))
    raise ValueError(mode)


def worst_days(syms: list[str], since: str, top: int) -> list[str]:
    """Сутки с наибольшей средней просадкой от открытия суток по всем монетам — «обвальные дни»."""
    t_lo = day_ts(since)
    acc: dict[int, list[float]] = {}
    for s in syms:
        z = read_symbol(s)
        if z is None:
            continue
        ts = z["ts"].astype(np.int64)
        k = ts >= t_lo
        ts, o, l = ts[k], z["open"][k].astype(float), z["low"][k].astype(float)
        d = ts // DAY
        ud, idx = np.unique(d, return_index=True)
        ends = np.r_[idx[1:], len(ts)]
        for dd, a, b in zip(ud, idx, ends):
            acc.setdefault(int(dd), []).append(float(l[a:b].min() / o[a] - 1.0))
    rank = sorted(acc, key=lambda dd: float(np.mean(acc[dd])))
    return [day_name(int(dd) * DAY) for dd in rank[:top]]


def run(days: list[str], syms: list[str], *, side: int, lev: float, daily_loss: float, max_dd: float,
        lag: int, taker_bps: float, mm: float, mode: str, exec_mode: str = "open", start_eq: float = 100.0) -> tuple[list[dict], list[dict]]:
    out: list[dict] = []
    every: list[dict] = []
    for d in days:
        t0 = day_ts(d)
        for s in syms:
            bar = load_day(s, t0)
            if bar is None:
                continue
            eps = [episode(bar, i, side, lev=lev, daily_loss=daily_loss, max_dd=max_dd, snapshot=start_eq,
                           start_eq=start_eq, lag=lag, taker_bps=taker_bps, mm=mm, exec_mode=exec_mode)
                   for i in entries(len(bar["o"]), mode)]
            eps = [e for e in eps if e.get("вход") is not None]
            if not eps:
                continue
            every += eps
            br = [e for e in eps if e["пробито"]]
            loss = np.array([-e["итог_доля"] for e in eps])
            over = np.array([e["перелёт_доля"] for e in br]) if br else np.zeros(0)
            out.append({
                "сутки": d, "монета": s, "сторона": "лонг" if side > 0 else "шорт", "входов": len(eps),
                "пробито_%": round(len(br) / len(eps) * 100, 1),
                "ликвидаций_%": round(sum(e["ликвидация"] for e in eps) / len(eps) * 100, 1),
                "убыток_средн_%": round(float(loss.mean()) * 100, 2),
                "убыток_макс_%": round(float(loss.max()) * 100, 2),
                "хуже_просадки_6_%": round(float((loss > max_dd + 1e-12).mean()) * 100, 1),
                "перелёт_средн_пп": round(float(over.mean()) * 100, 2) if len(over) else 0.0,
                "перелёт_макс_пп": round(float(over.max()) * 100, 2) if len(over) else 0.0,
                "перелёт_медиана_пп": round(float(np.median(over)) * 100, 2) if len(over) else 0.0,
            })
    return out, every


def entry_minutes(bars: dict[str, dict], t0: int, mode: str) -> list[tuple[int, dict[str, int]]]:
    """Минуты входа пула: отметка времени и индекс бара у КАЖДОЙ монеты на эту отметку.

    🔴 Места входят в одну и ту же минуту, поэтому сводить их надо по отметке времени, а не по номеру
    бара: при пропуске минуты у одной из монет номера расходятся, и пять «одновременных» входов
    оказались бы в разные моменты (замечание ревью 25.09). Берутся минуты, которые есть у всех монет;
    `hour` — только ровные часы от полуночи.
    """
    pos = {s: {int(t): idx for idx, t in enumerate(b["ts"].tolist())} for s, b in bars.items()}
    common = set.intersection(*[set(p) for p in pos.values()]) if pos else set()
    step = 60 if mode == "minute" else 3600
    return [(t, {s: pos[s][t] for s in pos}) for t in sorted(common) if (t - t0) % step == 0]


def seats_report(days: list[str], syms: list[str], *, side: int, lev: float, daily_loss: float,
                 max_dd: float, lag: int, taker_bps: float, mm: float, mode: str, exec_mode: str = "open",
                 seat_coins: list[str] | None = None) -> list[dict]:
    """Пул $100 тыс.: пять мест, каждое на своей монете, вход в одну и ту же минуту.

    Считает, сколько мест из пяти пробивает линию одновременно и сколько денег теряет пул.
    Монеты берутся по алфавиту — первые пять; это не выбор лучших, а фиксированное правило.
    """
    # По умолчанию — первые пять монет по алфавиту (правило автора: не выбор лучших, а
    # фиксированное правило). `--seat-coins` задаёт монету КАЖДОМУ месту поимённо и допускает
    # повторы: так считаются раскладки решения 20.09 («крупные места на спокойных монетах»,
    # «все места на SOL») — там пять мест сидят на трёх монетах.
    use = list(seat_coins) if seat_coins else syms[:len(POOL_SEATS)]
    if len(use) != len(POOL_SEATS):
        raise SystemExit(f"мест {len(POOL_SEATS)}, монет под них {len(use)}: {use}")
    out = []
    for d in days:
        t0 = day_ts(d)
        bars = {s: load_day(s, t0) for s in set(use)}
        if any(b is None for b in bars.values()):
            continue
        worst = None
        cnt: dict[int, int] = {}
        money_all: list[float] = []
        for t, at in entry_minutes(bars, t0, mode):
            eps = [episode(bars[s], at[s], side, lev=lev, daily_loss=daily_loss, max_dd=max_dd, snapshot=F,
                           start_eq=F, lag=lag, taker_bps=taker_bps, mm=mm, exec_mode=exec_mode)
                   for s, F in zip(use, POOL_SEATS)]
            eps = [e for e in eps if e.get("вход") is not None]
            if len(eps) != len(POOL_SEATS):
                continue
            k = sum(e["пробито"] for e in eps)
            cnt[k] = cnt.get(k, 0) + 1
            money = sum(-e["итог_доля"] * F for e, F in zip(eps, POOL_SEATS))
            money_all.append(money)
            if worst is None or money > worst[0]:
                worst = (money, t, k, sum(e["ликвидация"] for e in eps))
        if worst is None:
            continue
        total = sum(POOL_SEATS)
        mv = np.array(money_all) / total
        out.append({"сутки": d, "мест": len(use), "монеты": use,
                    "пробито_мест_распределение": {str(k): v for k, v in sorted(cnt.items())},
                    "все_пять_пробиты_%": round(cnt.get(len(use), 0) / max(len(money_all), 1) * 100, 1),
                    "убыток_пула_медиана_%": round(float(np.median(mv)) * 100, 2),
                    "убыток_пула_p95_%": round(float(np.quantile(mv, 0.95)) * 100, 2),
                    "убыток_пула_p99_%": round(float(np.quantile(mv, 0.99)) * 100, 2),
                    "входов_хуже_просадки_%": round(float((mv > max_dd + 1e-12).mean()) * 100, 1),
                    "худший_вход": time.strftime("%H:%M", time.gmtime(worst[1])),
                    "худший_убыток_usd": round(worst[0]), "худший_убыток_%_капитала_мест": round(worst[0] / total * 100, 1),
                    "мест_пробито_в_худшем": worst[2], "мест_ликвидировано_в_худшем": worst[3]})
    return out


def main() -> int:
    global DATA
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="", help="через запятую YYYY-MM-DD; пусто — худшие --top суток")
    ap.add_argument("--top", type=int, default=8, help="сколько худших суток взять с 2025-10-10")
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--symbols", default="", help="через запятую; пусто — все из klines-bybit-1m")
    ap.add_argument("--side", default="both", choices=("long", "short", "both"))
    ap.add_argument("--lev", type=float, default=5.0)
    ap.add_argument("--daily-loss", type=float, default=0.03)
    ap.add_argument("--max-dd", type=float, default=0.06)
    ap.add_argument("--lag", type=int, default=1, help="0 — закрытие по цене линии; N — через N минут")
    ap.add_argument("--taker-bps", type=float, default=4.5)
    ap.add_argument("--mm", type=float, default=0.02, help="поддерживающая маржа, доля номинала")
    ap.add_argument("--exec", dest="exec_mode", default="open", choices=("open", "worst"),
                    help="open — по открытию минуты k; worst — по худшей цене окна задержки (верхняя граница)")
    ap.add_argument("--entries", default="hour", choices=("hour", "minute"))
    ap.add_argument("--seats-report", action="store_true")
    ap.add_argument("--seat-coins", default="",
                    help="монеты пяти мест поимённо, через запятую (повторы разрешены); "
                         "пусто — первые пять монет по алфавиту")
    ap.add_argument("--json", default="")
    ap.add_argument("--data-dir", default=DATA,
                    help="каталог с минутками (CSV слепка или npz); по умолчанию — data/ рядом со скриптом")
    a = ap.parse_args()
    DATA = os.path.expanduser(a.data_dir)
    if not os.path.isdir(DATA):
        raise SystemExit(f"нет каталога с данными: {DATA} — скачать: python3 fetch_bybit_minutes.py")

    syms = [s.strip() for s in a.symbols.split(",") if s.strip()] or symbols()
    days = [d.strip() for d in a.days.split(",") if d.strip()] or worst_days(syms, a.since, a.top)
    sides = (1, -1) if a.side == "both" else ((1,) if a.side == "long" else (-1,))

    res: dict = {"правила": {"дневной_убыток": a.daily_loss, "просадка": a.max_dd, "плечо": a.lev,
                             "задержка_минут": a.lag, "исполнение": a.exec_mode,
                             "тейкер_bps": a.taker_bps, "поддерж_маржа": a.mm},
                 "сутки": days, "монет": len(syms), "входы": a.entries, "строки": [], "сводки": [], "пул": []}
    for side in sides:
        rows, eps = run(days, syms, side=side, lev=a.lev, daily_loss=a.daily_loss, max_dd=a.max_dd,
                        lag=a.lag, taker_bps=a.taker_bps, mm=a.mm, mode=a.entries, exec_mode=a.exec_mode)
        res["строки"] += rows
        if eps:
            res["сводки"].append(summarise(eps, a.max_dd, f"{'лонг' if side > 0 else 'шорт'}, задержка {a.lag} мин"))
        if a.seats_report:
            seat_coins = [c.strip() for c in a.seat_coins.split(",") if c.strip()] or None
            for r in seats_report(days, syms, side=side, lev=a.lev, daily_loss=a.daily_loss, max_dd=a.max_dd,
                                  lag=a.lag, taker_bps=a.taker_bps, mm=a.mm, mode=a.entries,
                                  exec_mode=a.exec_mode, seat_coins=seat_coins):
                res["пул"].append({**r, "сторона": "лонг" if side > 0 else "шорт"})

    for r in res["строки"]:
        print(json.dumps(r, ensure_ascii=False))
    for r in res["сводки"]:
        print(json.dumps(r, ensure_ascii=False))
    for r in res["пул"]:
        print(json.dumps(r, ensure_ascii=False))
    if a.json:
        open(a.json, "w", encoding="utf-8").write(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
