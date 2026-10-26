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

`ops/terms.py` writes the text from the plan file and the deployment's bounds, with a template
version on its first line. Every number in a clause is a setting of the contract, and the clause
names it and its value: `[plan.pool.term_days = 180; bounds.min_term = 15552000; ...]`. Anyone can
render the text again from the plan the pool holds and the same template, and compare the hash with
the one the pool names. A test holds that every setting appears with its value, and that changing
any one changes the hash (`ops/tests/test_terms.py`).

The money and risk clauses, D1 to D11, are the CFO's (INBOX 2026-10-15T13-46-24, section 1),
approved by Alex on 16 October 2026, and are in the text word for word in English, template 2.
Two changes, each on the CTO's word: D5's 14 and 90 days are the deployment's notice and renewal
(the product's are those numbers), and D8 says the free exams' second limit, a quarter of the
seats at once, as the code has it. A deployment records its template; nothing in it is pending.

### D1 to D11 in Russian

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

### Where the code does not say this yet

D10, and the fee in D6, are wider than the code on 16 October 2026. The code takes the platform's
0.5 % only on what a raise that fell short hands back: refunds, deposits returned from their
tickets, and the pool's own money (`RAISE_FEE_BPS`). A holder leaving at the end of a term, and the
owner of a single-investor pool taking money out, pay no fee; and an exam's platform fee is the
factory's `challengeFee()`, a fixed amount the operator sets, not 0.5 % of the price. Which one
changes, the code or the clause, is not decided here.

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
