# Terms of Use

Upools at https://pools.usenami.io/

Last updated: [TO CONFIRM: effective date]

Read these Terms in full before you buy an exam or open a pool.

## 1. Who we are and what these Terms cover

These Terms apply to your use of Upools at https://pools.usenami.io/ and of its contracts on the Hyperliquid network (HyperEVM). Payments are made in USDC on Hyperliquid mainnet.

In these Terms, "Upools", "we", "us" and "the platform" mean the operator of Upools.

By connecting a wallet, buying an exam or opening a pool, you accept these Terms. If you do not accept them, do not use Upools.

## 2. What an exam is

Both stages on Upools are trading exams. No real money is traded in the exam. Nothing in an exam trades on the exchange.

The price of an exam buys the exam and nothing else. It is not a deposit and not a stake in any pool. Nothing on Upools promises you any gain.

The assets available in an exam are BTC, ETH and SOL.

## 3. The menu

Each line of the menu is an account size with its own loss limit and target, the same for both stages, and its price in USDC.

| Loss limit / target | 5,000 | 10,000 | 25,000 | 50,000 | 100,000 | 250,000 |
|---|---|---|---|---|---|---|
| 3% / 9% | 20 USDC | 40 USDC | 100 USDC | 200 USDC | 400 USDC | 1,000 USDC |
| 4% / 10% | 30 USDC | 60 USDC | 150 USDC | 300 USDC | 600 USDC | 1,500 USDC |
| 5% / 10% | 40 USDC | 79 USDC | 198 USDC | 395 USDC | 790 USDC | 1,975 USDC |
| 6% / 10% | 50 USDC | 95 USDC | 238 USDC | 475 USDC | 950 USDC | 2,375 USDC |

The pool opens with the lines of 5,000 and 10,000 switched on. The lines of 25,000, 50,000, 100,000 and 250,000 are listed and switched off.

Only the pool's creator can switch a line on, and only when the pool holds free money for two payouts of that line: 4,000 USDC for 25,000, 8,000 USDC for 50,000, 16,000 USDC for 100,000 and 40,000 USDC for 250,000. Until the pool holds that money, the line cannot be switched on.

Free money is what the pool holds, less the payouts it owes, sets aside or keeps for stops, less the code shares not yet sent on, and less the payouts of every trader who has passed an exam or is running a stage.

A line that is on sells only while the pool holds free money for two of its payouts. While it does not, the line does not sell; it sells again when the money is back. The creator may switch a line off at any time. An exam already bought keeps its rules.

## 4. Exam rules

These rules apply to both stages.

- **Loss limit.** Each stage has a loss limit equal to the line's percent of the starting balance. It is measured from the highest equity recorded at the start of a UTC day (in the day's first 15 minutes), and it never rises above the starting balance. Once the account's recorded equity is that percent above the start, the limit stays at the starting balance.
- **Daily loss.** Equity may fall no more than 3% below the day's first snapshot. The snapshot is equity including open positions, taken within the first 15 minutes of the UTC day.
- **Target.** The line's percent of the account size.
- **Leverage.** At most 10x on BTC and ETH, and at most 5x on SOL.
- **Time.** Thirty days at most per stage. There is no minimum number of trading days.

## 5. Orders and fills

An order is filled by the platform's filler no sooner than the fourth block after the block it is placed in, at the price of the block that fills it. If it is not filled within 3 blocks from then, it lapses at no cost.

A fill takes the order book's price, never better than the oracle's price and at most 1.5% worse. Each fill pays 0.045% of its notional, which is Hyperliquid's taker fee.

A trader may set a stop order and a target order on an open position. They take effect no sooner than 4 blocks after they are set. They trigger when the oracle's price reaches their level. A triggered order is filled at the fill price of that block (the order book's price, never better than the oracle's price and at most 1.5% worse, with the 0.045% fee), not at the price of its level. The platform's filler executes triggered orders. If the filler does not, anyone may record that the order has triggered and execute it 60 blocks later.

## 6. Passing, the second stage and the payout

If you pass the exam, you start the second stage within seven days.

If you pass the second stage, you are paid a fixed 8% of the account size, once, in USDC. This payout is a fixed amount set by the menu. It is not a share of any trading result.

- The platform's keeper claims the payout as soon as the stage has passed.
- The payout is paid no sooner than 72 hours after it is claimed, and then once the pool's approver has approved it, or 7 days after it is claimed without approval.
- A payout that may be paid is paid by a call anyone may make. The keeper makes that call for every pass.
- Each pool pays at most two payouts a day, counted over any 24 hours. Every payout counts, approved or not. Any more are paid in order on the following days, even if their 72 hours or 7 days have passed.
- Payouts are paid in the order they were claimed, out of the money the pool holds. A payout still waiting for approval keeps its place. A later one is paid past it only if the money also covers every payout passed over.
- A payout the pool cannot cover waits, and every payout after it waits too, until money comes in.
- The pool's creator may change its approver at any time. Whoever approves, no payout waits for approval longer than 7 days after it is claimed.

## 7. Stopped payouts

Until a payout is paid, the platform's checker may stop it, once, with one of four reasons written on chain:

1. Opposite positions with another account on the platform.
2. Reason 1, and the two wallets are linked.
3. A disputed result: a flaw of the exam, the oracle or the filler.
4. Prohibited play: a series of exams bought to trade each at the edge of its risk, or one account hedged by another, shown by transactions and a published analysis, with 14 days for the trader to object.

A stopped payout stays in the pool for at least 14 days, while the trader can object. If the platform's operator lifts the stop within those 14 days, the payout is paid like any other. After them, anyone may make the stop final, and only from then does the money belong to the pool.

**How to object.** Write to business@usenami.io within 14 days after the stop. Include your wallet address, the address of the exam, the reason given for the stop, why you object, and your evidence, such as transaction hashes. The platform's operator reviews the objection. If the operator lifts the stop within those 14 days, the payout is paid like any other, as set out above.

## 8. Trading group codes

Every exam is bought with a trading group's code. The pool's creator sets each code with the address of the group's owner and the code's share: none, or 40% to 60%. A code for two also sets an author's address and the author's part of that share. A pool has as many codes as its creator sets.

Once a trader has used a code, its owner, its author and their split never change. Its share can be raised, never lowered.

## 9. Pool creators

This section applies if you open and fund a pool.

- **Opening.** Opening a pool costs 1 HYPE, paid to the platform.
- **Minimum term.** A pool runs at least 6 months (184 days) from its opening. Before then, its creator can neither close it nor take back what it put in, only the surplus above it. Only the platform can close a pool sooner, and the money then goes where it would anyway.
- **Withdrawals.** The creator withdraws by a request. The money leaves no sooner than 7 days after the request and within 3 days after that, only what the pool can spare then, and never more than was asked. The platform may cancel a withdrawal request. The platform never moves the money.
- **What stays in the pool.** While the pool is open, the payout of a trader who has passed the exam stays in the pool from the pass and for as long as the stage runs. The creator can withdraw surplus only above every such payout, and above three payouts of the largest line that has an exam or a stage running, or is switched on and selling.
- **Surplus** means what the pool holds above the payouts owed, held and kept, above the code shares not yet sent on, and above what the creator put in.
- **Code shares.** When the creator takes money out of the pool, each code is credited its share of the surplus taken (less the platform's part), in proportion to the part of all exam prices the pool received that came from that code's sales. What a code is credited stays in the pool, out of the creator's reach and out of payouts, until anyone sends it on, at any time: to the code's owner, and the author's part to the author. A share credited to a code is never taken back, even if later payouts bring the pool's money below what the creator put in.
- **Other USDC sent to the pool.** USDC sent to the pool other than through the creator's deposit function is not counted as put in by the creator. It covers payouts like any money of the pool. Any part of it that is surplus when the creator takes money out is shared with the codes' owners like any other surplus.

## 10. Platform fees

- 0.5% of each exam price.
- 0.5% of each withdrawal from a pool.
- 1 HYPE to open a pool.

## 11. Risks you accept

- **Smart contracts.** Upools runs on smart contracts. They may contain errors.
- You can lose the full price of an exam. If you fund a pool, you can lose what you put in, because the pool pays traders who pass.
- Transactions on Hyperliquid (HyperEVM) are final once confirmed. We cannot reverse them.
- Upools depends on the Hyperliquid network, its oracle, USDC and your own wallet. Any of them can fail, be delayed or change.
- You are responsible for your wallet, your keys and any transaction you sign.

## 12. No warranties

Upools, its site and its contracts are provided "as is" and "as available", with no warranty of any kind, whether express, implied or statutory. This includes any warranty of fitness for a particular purpose, accuracy, completeness, reliability, availability or timeliness. We do not promise that Upools will work without interruption or errors.

We accept no responsibility for:

- errors, bugs or weaknesses in the code of the Upools contracts;
- errors or failures of the oracle, the order book data or the platform's filler and keeper;
- errors, failures, delays or changes of the Hyperliquid network, HyperEVM, cross-chain bridges or USDC;
- your wallet, its provider, or any loss of access to your keys;
- interruption or end of service of the site or the contracts;
- unauthorized access to, or attacks on, the site, the contracts or your wallet;
- errors or gaps in any content on the site.

## 13. Limitation of liability

To the fullest extent the law allows, Upools and the people who work on it are not liable for any indirect, incidental, special, consequential or punitive damages, or for any loss of data, use or goodwill, arising from your use of Upools or from your inability to use it.

Our total liability to you for all claims, of any kind and on any legal basis, will not exceed the greater of (a) the total you paid for exams on Upools and (b) 100 US dollars.

Some jurisdictions do not allow certain warranties to be excluded or liability to be limited. In those jurisdictions, the exclusions and limits above apply to the fullest extent the law allows.

## 14. Who may use Upools

Upools is not available to Restricted Persons. Restricted Persons are:

- people and entities that live in, are located in, are incorporated in or have a registered office in the United States of America or Ontario, Canada;
- people and entities that live in, are located in, are incorporated in or have a registered office in a territory subject to economic or trade sanctions or export control laws (Restricted Territories);
- citizens of Restricted Territories, wherever they are;
- people and entities named on a sanctions list.

Upools does not check identity. By using Upools you confirm that:

- you are not a Restricted Person;
- you have reached the age of majority under the law of the country where you live;
- you do not use a VPN, proxy or any other method to hide your location or get around these limits;
- your use of Upools is lawful where you live. You alone are responsible for checking this.

We may block access from any place or wallet that we reasonably believe is restricted.

## 15. Changes to these Terms

We may change these Terms. When we do, we update the "Last updated" date at the top of this page. Changes take effect when they are published. If you keep using Upools after that, you accept the changed Terms. If you do not accept them, stop using Upools.

Rules of an exam already bought do not change.

## 16. Contact

business@usenami.io

https://pools.usenami.io/
