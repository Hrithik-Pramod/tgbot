"""
A wallet is adopted when it has no adoption baseline — not when it has no
cursor. And a quiet wallet is not a dead one.

TWO FAULTS, FOUND TOGETHER (15 September 2026)

Supplier D's wallet showed adopted = false and last polled four hours ago.
Two unrelated causes wearing the same clothes.

1. THE ADOPTION GATE WAS ASKING THE WRONG QUESTION

   adopt_wallet writes last_timestamp_ms and adopted_at_ms in one statement,
   with a comment saying they can never disagree — "that combination would
   look exactly like an established wallet and let its history back in".

   /walletchange deletes the whole monitor_state row so the new address
   re-adopts. But the poll cycle reads every wallet once at the top and then
   works through them, so a cycle already in flight still held the OLD
   cursor. It skipped adoption, found transfers, and set_monitor_cursor
   recreated the row — with a cursor and no baseline. Exactly the
   combination that was supposed to be impossible.

   From then on the wallet had a cursor, so the adoption branch never ran
   again and adopted_at_ms stayed null forever. That null switches OFF the
   history guard. The guard is what stops the two-minute cursor rewind
   walking back into old transfers and reporting them as new deposits — 38
   of them on 9 September, and a trade for ₹19,851,619.

   The fix is to ask the question the branch is actually about.

2. A WALLET WITH NOTHING NEW LOOKED UNPOLLED

   last_polled_at was written only by set_monitor_cursor, which runs only
   when transfers come back, and by adopt_wallet. A wallet polling perfectly
   but sitting quiet never updated it, so the health check said "deposits
   are being missed" about wallets that were fine.

   It said that on the same run as the real fault above. A check that cries
   wolf is one nobody reads on the day it matters.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.repo import Repo  # noqa: E402
from monitor import tron  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


class TestAdoptionAsksAboutAdoption:
    def test_the_gate_is_the_adoption_baseline(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        assert 'if wallet["adopted_at_ms"] is None:' in src

    def test_it_is_no_longer_the_cursor(self):
        """
        A cursor can exist without a baseline — that is the whole bug. Gating
        on it means a wallet in that state never adopts and never can.
        """
        src = _code(tron.DepositMonitor._poll_wallet)
        assert 'if wallet["last_timestamp_ms"] is None:' not in src

    def test_the_history_guard_still_reads_the_baseline(self):
        """
        The guard is the thing the gate protects. If adopted_at_ms is null it
        does nothing, which is why leaving a wallet unadopted is dangerous
        rather than untidy.
        """
        src = _code(tron.DepositMonitor._poll_wallet)
        assert 'wallet["adopted_at_ms"]' in src

    def test_adoption_still_writes_both_together(self):
        sql = _code(Repo.adopt_wallet)
        assert "last_timestamp_ms" in sql and "adopted_at_ms" in sql


class TestAQuietWalletReportsAsPolled:
    def test_every_successful_poll_is_recorded(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        assert "mark_polled" in src

    def test_it_happens_before_anything_conditional(self):
        """
        After the failure branch, before adoption and before the transfer
        loop — otherwise a wallet with nothing new falls straight past it,
        which is the bug.
        """
        src = _code(tron.DepositMonitor._poll_wallet)
        marked = src.index("mark_polled")
        adopted = src.index('wallet["adopted_at_ms"] is None')
        assert marked < adopted

    def test_a_failed_poll_is_not_recorded_as_polled(self):
        """
        The check exists to catch a wallet that cannot be reached. Stamping
        it on failure would hide precisely what it is for.
        """
        src = _code(tron.DepositMonitor._poll_wallet)
        before_fetch = src.split("except Exception as exc:")[0]
        assert "mark_polled" not in before_fetch
        failure_branch = src.split("except Exception as exc:")[1].split("return")[0]
        assert "mark_polled" not in failure_branch


class TestMarkPolledCannotHalfAdoptAWallet:
    def test_it_touches_neither_the_cursor_nor_the_baseline(self):
        """
        A row created by mark_polled must leave the wallet looking
        un-adopted. Writing a cursor here would recreate the exact state
        that caused the incident.
        """
        sql = _code(Repo.mark_polled)
        assert "last_timestamp_ms" not in sql
        assert "adopted_at_ms" not in sql

    def test_it_is_an_upsert(self):
        sql = _code(Repo.mark_polled)
        assert "ON CONFLICT (wallet_id) DO UPDATE" in sql

    def test_it_clears_the_error_count(self):
        sql = _code(Repo.mark_polled)
        assert "consecutive_errors = 0" in sql
