"""
A name the matcher could not place survives the next deploy.

WHAT HAPPENED (live, 19 September 2026)

The client pasted a payment "to SUPER TRA" — the bank's own truncation of
SUPER TRADING COMPANY (STC), which is registered twice, once under
IndoLondon and once under New SUPER Group. The bot could not place it and
asked, which is correct behaviour for an ambiguous name.

The interesting question was whether the tiebreak added the day before had
run at all. Only IndoLondon had a live trade that evening, so it had exactly
one candidate to pick and should have resolved it without asking.

That question could not be answered. The handler logs the typed name and the
whole candidate list, but four redeploys had recreated the container and
taken every line with them. The evidence existed for a few hours and then did
not.

THE RULE THIS APPLIES

The same one the ledger already follows: if it matters after the fact, it
goes in the database. A container log is fine for watching a system run and
useless for asking what happened yesterday on something deployed several
times a day.

So the audit log now carries the name the client typed, the candidates it was
weighed against, and — the part that would have settled it — whether each of
those candidates had an open trade at that moment.
"""

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


def _sql(fn) -> str:
    return re.sub(r"\s+", " ", _code(fn))


class TestTheFailureIsWrittenDown:
    def test_an_unmatched_name_reaches_the_audit_log(self):
        src = _code(client_bot.on_pasted_payment)
        assert "audit_standalone" in src
        assert '"payment.unmatched_beneficiary"' in src

    def test_it_keeps_what_the_client_actually_typed(self):
        """
        "SUPER TRA" is the whole clue. Without it the record says only that
        something did not match, which is what the client can already see.
        """
        src = _code(client_bot.on_pasted_payment)
        branch = src.split("audit_standalone")[1]
        assert '"typed": p.beneficiary' in branch

    def test_it_keeps_the_payment_it_was_attached_to(self):
        src = _code(client_bot.on_pasted_payment).split("audit_standalone")[1]
        assert '"utr": p.utr' in src

    def test_it_keeps_every_candidate_and_its_name(self):
        src = _code(client_bot.on_pasted_payment).split("audit_standalone")[1]
        assert '"candidates"' in src
        assert 'a["account_name"]' in src

    def test_it_records_which_candidates_had_an_open_trade(self):
        """
        The field that would have answered the 19 September question on its
        own: the tiebreak picks the candidate with a live trade, so knowing
        how many had one says immediately whether it could have fired.
        """
        src = _code(client_bot.on_pasted_payment).split("audit_standalone")[1]
        assert '"has_open_trade": a["trade_id"] is not None' in src

    def test_the_container_log_line_is_kept_as_well(self):
        """Useful while watching it run. Just no longer the only copy."""
        src = _code(client_bot.on_pasted_payment)
        assert "log.warning" in src
        assert "unmatched beneficiary" in src


class TestItCannotBreakThePaymentItObserves:
    def test_the_write_is_wrapped(self):
        """A diagnostic that can fail a payment is worse than no diagnostic."""
        src = _code(Repo.audit_standalone)
        assert "except Exception:" in src
        assert "log.exception" in src

    def test_it_takes_its_own_connection(self):
        """
        audit() belongs inside the transaction it describes. This is the
        other kind — an observation worth keeping whether or not anything
        else happened — so it must not need a caller's transaction.
        """
        src = _code(Repo.audit_standalone)
        assert "async with self.pool.acquire() as conn:" in src
        assert "conn" not in inspect.signature(Repo.audit_standalone).parameters

    def test_the_transactional_audit_is_untouched(self):
        """
        Every entry that must stand or fall with a change still goes through
        audit(), which takes a connection and does NOT swallow failures.
        """
        src = _code(Repo.audit)
        assert "except" not in src
        assert "conn" in inspect.signature(Repo.audit).parameters
