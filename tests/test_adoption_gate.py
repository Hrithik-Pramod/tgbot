"""
Only adoption may create a monitor_state row.

THE FAULT (live, 15 September 2026)

Supplier D's wallet showed adopted = false and last polled four hours ago.
It had a cursor and no adoption baseline — the one combination adopt_wallet
writes both columns together to prevent, because a null baseline switches
OFF the history guard. That guard is what stops the two-minute overlap
rewind pulling old transfers back in as fresh deposits: 38 of them on
9 September, and a trade for ₹19,851,619.

HOW IT GOT THERE

/walletchange deletes the whole monitor_state row so the new address is
adopted from scratch. But the poll cycle reads every wallet once at the top
and then works through them, so a cycle already in flight still held the OLD
cursor. It skipped adoption, found transfers, and set_monitor_cursor — an
upsert — recreated the row with that stale cursor and no baseline.

THE FIX I TRIED FIRST, AND WHY IT WAS WRONG

Gating adoption on adopted_at_ms instead of the cursor. It reads better and
it breaks production: wallets adopted before that column existed carry a
cursor and a null baseline perfectly legitimately, and re-adopting one drags
its cursor past deposits it has not seen. test_poll_wallet has asserted that
since the column was added, and it caught this.

Which also settles the deeper point: a cursor with no baseline cannot be
told apart from a legacy wallet. It is not repairable in code. It has to be
made unreachable.

THE FIX

Only adopt_wallet creates the row, and it always writes both columns.
set_monitor_cursor and mark_polled are UPDATE-only, so a row deleted by
/walletchange stays deleted until the next cycle reads a fresh snapshot,
sees no cursor, and adopts properly.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.repo import Repo  # noqa: E402
from monitor import tron  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    src = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    return re.sub(r"\s+", " ", src)


class TestOnlyAdoptionCreatesTheRow:
    def test_setting_the_cursor_cannot_create_it(self):
        sql = _code(Repo.set_monitor_cursor)
        assert "INSERT INTO monitor_state" not in sql, (
            "an upsert here recreates a row /walletchange just deleted, with "
            "a stale cursor and no adoption baseline"
        )
        assert "UPDATE monitor_state" in sql
        assert "WHERE wallet_id = $1" in sql

    def test_marking_a_poll_cannot_create_it(self):
        sql = _code(Repo.mark_polled)
        assert "INSERT INTO monitor_state" not in sql
        assert "UPDATE monitor_state" in sql

    def test_adoption_still_creates_it_with_both_columns(self):
        sql = _code(Repo.adopt_wallet)
        assert "INSERT INTO monitor_state" in sql
        assert "last_timestamp_ms" in sql
        assert "adopted_at_ms" in sql

    def test_the_cursor_still_only_moves_forward(self):
        """GREATEST, so an out-of-order poll cannot rewind recorded work."""
        sql = _code(Repo.set_monitor_cursor)
        assert "GREATEST(last_timestamp_ms, $2)" in sql


class TestTheAdoptionGateIsUnchanged:
    """
    Deliberately still the cursor. Gating on the baseline would re-adopt
    every wallet that predates that column — see the module docstring.
    """

    def test_it_gates_on_the_cursor(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        assert 'if wallet["last_timestamp_ms"] is None:' in src

    def test_it_does_not_gate_on_the_baseline(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        assert 'if wallet["adopted_at_ms"] is None:' not in src

    def test_the_history_guard_still_reads_the_baseline(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        assert 'wallet["adopted_at_ms"]' in src


class TestAQuietWalletReportsAsPolled:
    """
    last_polled_at was written only by set_monitor_cursor, which runs only
    when transfers come back. A wallet polling fine but sitting quiet never
    updated it, so the health check said "deposits are being missed" about
    wallets that were fine — on the same run as a real fault, which is the
    cost: a check that cries wolf is not read on the day it matters.
    """

    def test_every_successful_poll_is_recorded(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        assert "mark_polled" in src

    def test_a_failed_poll_is_not(self):
        src = _code(tron.DepositMonitor._poll_wallet)
        failure_branch = src.split("except Exception as exc:")[1].split("return")[0]
        assert "mark_polled" not in failure_branch

    def test_it_touches_neither_the_cursor_nor_the_baseline(self):
        sql = _code(Repo.mark_polled)
        assert "last_timestamp_ms" not in sql
        assert "adopted_at_ms" not in sql
