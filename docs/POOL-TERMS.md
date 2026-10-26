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

The money and risk clauses are the CFO's: the footnote and six conditions approved by Alex on
14 October 2026. They go into the text word for word; until they do, the section says it is
pending, and a deployment records that its terms are pending.

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
