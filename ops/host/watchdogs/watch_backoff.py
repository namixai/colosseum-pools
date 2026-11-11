"""One retry ladder for both host watchdogs (`keys_watch.py`, `keeper_watch.py`).

Why it is longer than a minute. HyperEVM nodes count requests per MINUTE. A ladder of three tries
with pauses of 1 and 2 s fits inside one rate window: when the first request met a 429, both
retries met the same one, and the watchdog gave up with "could not check" (exit 3). Measured in
September and October 2026: one watchdog lost 7 runs of 24 in a day that way, and on the host two
runs in a row left a flag up after the chain had already cleared it.

The ladder is the keeper's: seven tries, pauses of 1 to 32 s, 63 s in all. The point of 63 is that
it is more than 60: the last try always goes out in the next minute's window, whichever second of
its window the first refusal fell on.

One ladder per call, one budget per run. Without a budget a hung node (every try waits out the
request timeout) would stretch a run past the unit's TimeoutStartSec, and the journal would show a
killed process where it should show an honest exit 3. `RUN_BUDGET_S` is the ceiling after which no
new pause begins; the units' TimeoutStartSec has to exceed it by one request
(ops/tests/test_host_watchdogs.py checks that).

Changed ONLY here: the watchdogs have no pauses or try counts of their own.
"""

from __future__ import annotations

import time

RATE_WINDOW_S = 60                    # the node's rate window: requests are counted per minute
PAUSES_S = (1, 2, 4, 8, 16, 32)       # the pause after the 1st, 2nd, ... 6th failed try
ATTEMPTS = len(PAUSES_S) + 1          # seven tries
LADDER_S = sum(PAUSES_S)              # 63 s: the longest a call can spend on pauses
RUN_BUDGET_S = 240                    # ceiling for a whole run; the units allow 300 s


class Ladder:
    """The retries of one run. `real=False` is for a test: pauses are counted, not slept."""

    def __init__(self, real: bool = True, sleep=time.sleep, clock=time.monotonic):
        self.real, self._sleep, self._clock = real, sleep, clock
        self.started = clock()
        self.retries = 0              # how many times it waited and tried again
        self.waited_s = 0             # seconds of pauses so far (counted, in a test)
        self.out_of_budget = False

    def again(self, attempt: int) -> bool:
        """After failed try number `attempt` (from 0): wait, and say whether to try again.

        False: the tries are used up, or the run would go past its budget. The caller then gives
        up with "could not check" rather than judging the chain.
        """
        if attempt >= len(PAUSES_S):
            return False
        pause = PAUSES_S[attempt]
        if self._clock() - self.started + pause > RUN_BUDGET_S:
            self.out_of_budget = True
            return False
        self.retries += 1
        self.waited_s += pause
        if self.real:
            self._sleep(pause)
        return True

    def gave_up(self, attempts_made: int) -> str:
        """The tail of a failure message: how many tries were made and why it stopped."""
        why = (f"the run's budget of {RUN_BUDGET_S} s is used up" if self.out_of_budget
               else f"the ladder of {LADDER_S} s has been climbed")
        return f"{attempts_made} failures in a row, {self.waited_s} s of pauses, {why}"
