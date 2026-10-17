# A pool's terms, on chain

Alex, 15 October 2026: every condition of a pool, written out clause by clause, lies on the chain,
open to anyone; the creator and every investor accept them with their own wallet.

**Draft. Nothing here is deployed, and the audit has not read it.**

## Where the text lies

`src/multi/TermsBook.sol` keeps a text once, under its keccak256, as the code of a contract of its
own (`src/lib/CodeStore.sol`). Anyone reads it with one call, `text(hash)`, however old it is, and
nobody can change it. A text is at most 24,575 bytes.

The pool names the hash of its terms and the book that holds them in its plan (`terms`,
`termsBook`), and the plan itself is kept the same way (`planData()`), so the pool's settings and
its terms can both be read back from the chain.

Not an event: the public node answers log queries for fifty blocks at a time, so a text left in an
event at creation is hard to find a week later. Code is read in one call.

## How a clause is tied to a setting

`ops/terms.py` writes the text from the plan file and the deployment's bounds. Its first line is
`Plan 0x<hash>`, the hash of the plan as the pool is given it with its two terms fields zero
(`deploy_multi.plan_name`), and its second the template version (4). The pool's constructor takes
no terms that do not begin with its own plan's hash, none from a book that is not the platform's
(`TermsBook`, by its code), and none that are not kept there; the book keeps no empty text (deep
audit F13). So a text cannot serve a plan it was not written for. Every number in a clause is a setting of the contract, and the clause
names it and its value: `[plan.pool.term_days = 180; bounds.min_term = 15552000; ...]`. Anyone can
render the text again from the plan the pool holds and the same template, and compare the hash with
the one the pool names. A test holds that every setting appears with its value, and that changing
any one changes the hash (`ops/tests/test_terms.py`).

A pool of several investors has one more line, the second: `Shares 0x<hash>`, the hash of its shares
contract's raise, the amount and the smallest deposit (`ops/terms.py shares_name`). Those two are
the shares contract's own and in no plan; `PoolShares.bind` takes no terms whose second line is not
its own (deep audit N5). No script deploys a shares contract yet; the one that does has to render
the terms from the same two numbers it passes the constructor.

The money and risk clauses, D1 to D11, are the CFO's (INBOX 2026-10-15T13-46-24, section 1),
approved by Alex on 16 October 2026, and are in the text word for word in English, since template 2.
Two changes, each on the CTO's word: D5's 14 and 90 days are the deployment's notice and renewal
(the product's are those numbers), and D8 says the free exams' second limit, a quarter of the
seats at once, as the code has it. A deployment records its template; nothing in it is pending. D12, on
referral payouts, is the CFO's too (`_hub/FINANCE/TERMS-EDITS-2026-10-16.md`), accepted on Alex's word of
16 October 2026, in the text since template 4, with no setting named until the pool pays referrers.
Since template 4 the funded account's clause names its assets as well (the deep audit's F27).

### D1 to D12 in Russian

The text on chain is the English. This is the CFO's Russian, the same clauses, with D8 completed
the same way. In D5 the numbers are the product's.

- **Д1. Оценка, не гарантия.** Процент доходности, показанный при создании пула, — оценка модели,
  а не обещание и не гарантия. Торгов на настоящие деньги в её основе пока нет. Фактический
  результат может быть ниже нижнего числа и может быть убытком; вернуться может меньше, чем внесено.
- **Д2. От чего зависит результат.** Сколько трейдеров купит экзамены; как они торгуют и сколько из
  них пройдёт; движение рынка — в резкий день стоп может исполниться хуже линии просадки; условия
  пула — на жёсткие условия может прийти меньше трейдеров, чем заложено в оценке; срок и размер
  пула; работа биржи и сети.
- **Д3. Деньги пула на боевых счетах.** На боевых счетах трейдеры торгуют деньгами пула: их прибыль
  делится с ними по доле, заданной в условиях пула; их убыток остаётся в пуле.
- **Д4. Плечо.** Увеличение плеча несёт риск увеличения убытков.
- **Д5. Срок и продление.** Вклад заперт до конца срока пула; досрочного вывода нет. Если не позднее
  чем за 14 дней до конца срока вы не заявили «выхожу», вклад остаётся в пуле ещё на 90 дней — и так
  в конце каждого следующего срока. Вносится только стейблкоин.
- **Д6. Конец срока.** Если вы заявили «выхожу», в конце срока ваша доля выплачивается по стоимости
  пула на этот момент за вычетом сбора с вывода; пул нескольких инвесторов при этом работает дальше.
  Пул одного инвестора заканчивается, только если владелец заявил «стоп»: тогда новые экзамены не
  продаются, позиции закрываются, прибыль делится. Если к концу срока денег на выплату уходящим не
  хватает, пул останавливается так же.
- **Д7. Сбор пула нескольких инвесторов.** Создатель задаёт сумму сбора; сбор длится 30 дней. Если
  сумма набрана, пул запускается и начинается срок. Если не набрана, каждый вклад возвращается за
  вычетом 0,5 % — сбора платформы с вывода.
- **Д8. Бесплатные экзамены.** Создатель пула может раздавать бесплатные экзамены — не больше 10 в
  месяц на каждые $100 000 пула и одновременно не больше четверти мест пула. За бесплатный экзамен
  пул ничего не получает, а результат прошедшего на настоящем счёте ложится на пул так же, как у
  платного; место в пуле занято с начала экзамена.
- **Д9. Условия пула.** Владелец пула одного инвестора и создатель пула нескольких инвесторов
  задают условия сами. Вкладчик пула нескольких инвесторов принимает условия, заданные создателем,
  подписью при вкладе.
- **Д10. Сборы платформы.** Платформа берёт 0,5 % цены каждого экзамена и 0,5 % с каждого вывода, в
  том числе с возврата вклада при несобранном пуле; в оценке они уже учтены.
- **Д11. Ответственность.** Ответственность за свои деньги несёте только вы.
- **Д12. Реферальные выплаты.** Владелец пула одного инвестора или создатель пула нескольких
  инвесторов может платить рефералам из денег пула. Размер выплаты (долю цены экзамена) и потолок
  выплат за год он задаёт при создании пула. Они записаны в условиях пула, вы видите их до вклада, и
  в течение срока они не меняются. Эти выплаты уменьшают то, что пул получает с экзаменов.

Д12 принят словом Alex 16.10.2026 (DECISIONS п. 100 и 103), текст CFO
(`_hub/FINANCE/TERMS-EDITS-2026-10-16.md`), в шаблоне с версии 4. Выплат рефералам в коде пока нет,
поэтому у Д12 нет строки параметра; она появится вместе с выплатой.

### The fees in the code

D6 and D10 are what the code takes since 16 October 2026 (the CTO's decision: the code follows the
terms). An exam pays the platform 0.5 % of its price, out of the price: the buyer pays the price,
the pool keeps 99.5 % of it (item 28, DECISIONS item 101), and a free exam pays nothing; the
factory's flat `challengeFee()` is not used by this pool (`MultiPool.PLATFORM_FEE_BPS`). Every payment out of
the pool to its owner pays 0.5 %, kept as it is paid: money taken with `extend`, `withdraw` and
`withdrawEvm`. A leaver of a pool of several investors is paid what their shares are worth less
0.5 %; the pool keeps that part of what it pays the shares contract, which keeps the rest (on
money it already held) for `payFee`, so nothing is charged twice. A raise that falls short hands
every deposit back less 0.5 % (`PoolShares.RAISE_FEE_BPS`). Without a fee recipient on the
factory, nothing is taken. Until 16 October the code took the 0.5 % only on a failed raise; the
deep audit's F8.

### When a leaver is paid

D5 and D6 say a deposit is locked until the end of the term and a leaver is paid at its end, at the
pool's value then. Until template 4 the code and clause 4 let the money leave in the term's last
days with an extension, at the value of that day (the deep audit's F38). Alex chose the code to
follow the terms (17 October 2026, "делаем А", DECISIONS 103): `MultiPool.extend` lets money
reserved to leave go only once the end it was reserved for has passed, and `PoolShares` closes the
round of those who asked to leave only then, at the value of that moment, by anyone (`renew`, the
keeper as soon as the end has passed). In the last days the creator's extension moves the end and
nothing else. Clause 4 says so since template 4. D5 and D6 are as approved.

### What the app has to check

The chain ties the text to the plan by its first line and keeps it unchangeable; it does not read
the clauses after that line (the audit's F13 residue, test `test_deep_F13_theClausesAreCheckedOffChain`).
So before an investor signs, by `openTicket`, or a creator by `bind`, or an owner by `begin`, the app
(КОЛИЗЕЙ-2's) has to:

1. read the pool's plan from the chain (`planData()`), the deployment's bounds (`notice()`,
   `renewal()`, `minTerm()`, `maxTerm()`), and the text kept under `plan.terms` (`TermsBook.text`);
2. render the text again from that plan with the template its second line names, as
   `ops/terms.py` does, and compare: the same bytes, the same keccak256 as `plan.terms`;
3. show the investor the rendered text, the one the hash names, and offer the signature only when
   they match. If they do not, say so and offer nothing.

`ops/terms.py` renders from the plan file; the app needs the same from the plan as the chain holds
it, so the template has to be ported (or `terms.py` given a renderer from the plan tuple) first.

## Who accepts, and how

- **A pool of one investor.** The owner starts the term with `begin(terms)`; the call names the
  hash, and the pool refuses any other.
- **A pool of several investors.** The creator binds the shares contract to the pool with
  `bind(pool, terms)`, and every investor opens their deposit with `openTicket(terms)`; each
  refuses a hash that is not the pool's, and each acceptance is an event (`TermsAccepted`).

What the wallet signs is the transaction, and the hash is in it: whoever signs it has named, on
chain, the exact text they accept.

## Tests

`test/multi/TermsBook.t.sol` (the book and the code store), the terms tests in
`test/multi/MultiPool.t.sol` (`begin` refuses other terms) and `test/multi/PoolShares.t.sol`
(`bind` and `openTicket` do), and `ops/tests/test_terms.py`.
