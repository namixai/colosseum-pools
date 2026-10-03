"""A stopped account when the head read is refused: settle still goes, the recut is skipped.

Written by the review at main `9a79ae69` and taken as it stood: the guard in `recut_if_uncut` was
held by nothing, since the existing head-refusal test only walks an ACTIVE account. Checked the
other way round before taking it -- with the guard removed this test fails.

    spike/.venv/bin/python -m unittest ops.tests.test_audit_head_none -v
"""
from unittest import mock

from ops import keeper
from ops.tests.test_keeper import CHALLENGE_A, POOL_A, KeeperTest, challenge_log


class HeadNone(KeeperTest):
    def test_a_stopped_account_is_still_settled_without_a_head_and_its_recut_waits(self):
        # `STOPPED` is a tuple (`ops/keeper.py:90`); the first of it is Breached, which is a
        # stopped account whose settlement still has steps to take.
        self.follow_a(status=keeper.STOPPED[0],
                      cutKey="0x00000000000000000000000000000000000000c7", cutBlock=1)
        self.chain.logs.append(challenge_log(10, POOL_A))
        k = self.make()
        k.one_pass()
        sent_before = len(self.chain.calls_to(CHALLENGE_A))
        real = self.chain.rpc
        self.chain.rpc = mock.Mock(side_effect=lambda m, p=(): (_ for _ in ()).throw(RuntimeError("rate limited"))
                                   if m == "eth_blockNumber" else real(m, p))
        k.one_pass()
        calls = [fn for fn, _ in self.chain.calls_to(CHALLENGE_A)][sent_before:]
        self.assertEqual(calls, ["settle"], "the settlement step goes without a head; the recut waits for one")
        self.assertNotIn("pool_failed", [c.args[0] for c in keeper.log.call_args_list])
