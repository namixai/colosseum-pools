"""Стенд поломок для pool_stress.py: каждая поломка правила обязана краснить test_pool_stress.py.

После ревью 25.09 добавлены поломки края суток, вилки ликвидации и сведения мест по времени.

Копия без `__pycache__` и запуск с `-B`: иначе поломка той же длины, записанная в ту же секунду,
читается из старого `.pyc` и «зеленеет» ложно. Сначала контроль без поломки — он обязан быть
зелёным; красное без строки «•» считается падением не на проверке, а на ошибке стенда.

    python3 mut_pool_stress.py [часть имени поломки ...]
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent
PY = sys.executable
F, T = "pool_stress.py", "test_pool_stress.py"

M = [
 ("линия от плеча не делится", "return px_in * (1.0 - side * loss_frac / lev)", "return px_in * (1.0 - side * loss_frac)"),
 ("линия шорта в ту же сторону", "return px_in * (1.0 - side * loss_frac / lev)", "return px_in * (1.0 - loss_frac / lev)"),
 ("дневная линия от капитала, а не от снимка",
  "line_eq = max(snapshot * (1.0 - daily_loss), start_eq * (1.0 - max_dd))",
  "line_eq = max(start_eq * (1.0 - daily_loss), start_eq * (1.0 - max_dd))"),
 ("рубит поздняя линия, а не ранняя",
  "line_eq = max(snapshot * (1.0 - daily_loss), start_eq * (1.0 - max_dd))",
  "line_eq = min(snapshot * (1.0 - daily_loss), start_eq * (1.0 - max_dd))"),
 ("вход при уже пробитой линии", "    if line_eq >= eq_in:", "    if False:"),
 ("пробитие по закрытию, а не по минимуму минуты", "    adverse = l if side > 0 else h", "    adverse = bar['c']"),
 ("пробитие шорта ищется снизу", "    adverse = l if side > 0 else h", "    adverse = l"),
 ("задержка не применяется", "        k, p_out, path_end = j + lag, float(o[j + lag]), j + lag",
  "        k, p_out, path_end = j, float(o[j]), j"),
 ("выход по закрытию минуты, а не по открытию", "        k, p_out, path_end = j + lag, float(o[j + lag]), j + lag",
  "        k, p_out, path_end = j + lag, float(bar['c'][j + lag]), j + lag"),
 ("гэп через линию исполнен по линии",
  "            p_out = float(o[j])                             # минута открылась за линией: раньше не исполнить",
  "            pass"),
 ("путь до ликвидации включает минуту выхода целиком",
  "        deep = min(float(l[i:path_end].min()), tail) if side > 0 else max(float(h[i:path_end].max()), tail)",
  "        deep = min(float(l[i:path_end + 1].min()), tail) if side > 0 else max(float(h[i:path_end + 1].max()), tail)"),
 ("ликвидация биржей не обрезает убыток", "    if liquidated:", "    if False:"),
 ("🔴 край суток: выход по ОТКРЫТИЮ последней минуты, до пробития (замечание ревью 25.09)",
  '        k, p_out, path_end = n - 1, float(bar["c"][n - 1]), n', "        k, p_out, path_end = n - 1, float(o[n - 1]), n"),
 ("край суток: путь не включает низ последней минуты",
  '        k, p_out, path_end = n - 1, float(bar["c"][n - 1]), n', '        k, p_out, path_end = n - 1, float(bar["c"][n - 1]), n - 1'),
 ("🔴 верхняя граница оставляет ликвидированному месту маржу",
  '        eq_out = 0.0 if exec_mode == "worst" else max(equity(p_liq, px_in, side, lev, eq_in) - fee, 0.0)',
  "        eq_out = max(equity(p_liq, px_in, side, lev, eq_in) - fee, 0.0)"),
 ("нижняя граница отнимает у ликвидированного места маржу",
  '        eq_out = 0.0 if exec_mode == "worst" else max(equity(p_liq, px_in, side, lev, eq_in) - fee, 0.0)',
  "        eq_out = 0.0"),
 ("🔴 места сводятся по номеру бара, а не по отметке времени (замечание ревью 25.09)",
  "    return [(t, {s: pos[s][t] for s in pos}) for t in sorted(common) if (t - t0) % step == 0]",
  "    return [(t, {s: n for s in pos}) for n, t in enumerate(sorted(common)) if (t - t0) % step == 0]"),
 ("час-режим пула берёт каждую минуту", '    step = 60 if mode == "minute" else 3600', "    step = 60"),
 ("уровень ликвидации без поддерживающей маржи", "    r = (mm * lev - 1.0) / (lev - mm * lev)", "    r = -1.0 / lev"),
 ("комиссия не на номинал, а на эквити", "    fee = taker_bps / 1e4 * lev * eq_in", "    fee = taker_bps / 1e4 * eq_in"),
 ("место теряет больше своего капитала", "        eq_out = max(equity(p_out, px_in, side, lev, eq_in) - fee, 0.0)",
  "        eq_out = equity(p_out, px_in, side, lev, eq_in) - fee"),
 ("эквити без плеча", "    return eq_in * (1.0 + side * lev * (px / px_in - 1.0))", "    return eq_in * (1.0 + side * (px / px_in - 1.0))"),
 ("перелёт считается от капитала, а не от линии",
  '"перелёт_доля": max(line_eq - eq_out, 0.0) / start_eq,', '"перелёт_доля": max(start_eq - eq_out, 0.0) / start_eq,'),
 ("перелёт берётся и по непробитым", 'over = np.array([e["перелёт_доля"] for e in eps if e["пробито"]])',
  'over = np.array([e["перелёт_доля"] for e in eps])'),
 ("доля хуже просадки считается по пробитым", 'loss = np.array([-e["итог_доля"] for e in eps])\n    over',
  'loss = np.array([-e["итог_доля"] for e in eps if e["пробито"]])\n    over'),
 ("сутки грузятся без верхней границы", "    k = (ts >= t0) & (ts < t0 + DAY)", "    k = ts >= t0"),
 ("худшие сутки сортируются по возрастанию убытка", "    rank = sorted(acc, key=lambda dd: float(np.mean(acc[dd])))",
  "    rank = sorted(acc, key=lambda dd: -float(np.mean(acc[dd])))"),
 ("убыток пула не взвешен капиталом места", 'money = sum(-e["итог_доля"] * F for e, F in zip(eps, POOL_SEATS))',
  'money = sum(-e["итог_доля"] for e in eps)'),
 ("режим worst не берёт худшую цену окна",
  "        bad = float(l[j:k + 1].min()) if side > 0 else float(h[j:k + 1].max())",
  "        bad = float(l[k]) if side > 0 else float(h[k])"),
 ("режим worst применяется всегда", '    if exec_mode == "worst" and lag:', "    if lag:"),
 ("путь до ликвидации кончается не на выходе", "    tail = p_out ", "    tail = float(o[k]) "),
 ("пробитые места не считаются", 'k = sum(e["пробито"] for e in eps)', "k = 0"),
]

only = sys.argv[1:]
bad = 0
ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "HOME": str(Path.home())}


def run_copy(mutate=None):
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        for f in (F, T):
            shutil.copy(SRC / f, d)
        if mutate and not mutate(d):
            return None
        return subprocess.run([PY, "-B", T], cwd=d, capture_output=True, text=True, env=ENV)


ctl = run_copy()
print("КОНТРОЛЬ без поломки:", "зелёный" if ctl.returncode == 0
      else "🔴 КРАСНЫЙ — стенд сломан: " + (ctl.stdout + ctl.stderr).strip()[-300:])
if ctl.returncode != 0:
    sys.exit(1)
for name, old, new in M:
    if only and not any(o in name for o in only):
        continue

    def mut(d, old=old, new=new):
        p = d / F
        src = p.read_text(encoding="utf-8")
        if src.count(old) != 1:
            return False
        p.write_text(src.replace(old, new), encoding="utf-8")
        return True

    r = run_copy(mut)
    if r is None:
        print(f"🔴 МУТАЦИЯ НЕ ЛЕГЛА: {name}")
        bad += 1
        continue
    why = next((x.strip() for x in r.stdout.splitlines() if x.strip().startswith("•")), None)
    if r.returncode == 0:
        bad += 1
        print(f"🔴 ЗЕЛЁНЫЙ  {name}")
    elif why is None:
        bad += 1
        print(f"🔴 УПАЛ НЕ НА ПРОВЕРКЕ  {name}: {(r.stderr or r.stdout).strip()[-160:]}")
    else:
        print(f"КРАСНЕЕТ  {name}: {why[2:80]}")
print(f"поломок {len(M)} · не пойманы/не легли: {bad}")
sys.exit(1 if bad else 0)
