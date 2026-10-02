"""
A payment that covers the total closes the trade — on every path, not most.

WHAT HAPPENED

SUPA43, 2 October 2026. ₹5,644,500 expected, ₹5,644,500 paid, status still
`awaiting_payment`. The closing claim had never been attempted, because the
final payment arrived through the shared-account branch added the day before,
and that branch wrote the payment and moved on.

    also Myfx, i change the rate midway through a send, it didnt take it?
    — Bridge, 2 October 2026

He was asking about something else entirely. The stuck trade was found while
looking.

WHY IT MATTERS MORE THAN A MISSING MESSAGE

The summaries not going out is the visible half: no closing summary to the
Bridge, and the supplier never released to their next batch.

The expensive half is silent. A trade in `awaiting_payment` still holds the
"one order at a time" slot in the allocation tiebreak, so the NEXT payment
into that account is attributed to an order that finished hours ago. That is
the SUPA38/BRAV9 misattribution — ₹983,000 across three payments on
30 September, two days and a hand-written reconciliation to unpick — and it
would have been reintroduced by the very branch written to prevent it.

THE RULE, AND THE NET UNDER IT

  1. EVERY PATH THAT RECORDS A PAYMENT CLAIMS COMPLETION.
     There is no quiet payment. A payment the client never sees acknowledged
     is still money against a trade.

  2. AND A SWEEP ASKS ANYWAY.
     Three payment paths existed and all three claimed; a fourth was added
     and did not. Relying on the next author remembering has now failed once,
     which is once more than the rule is worth. claim_completion is atomic —
     it closes a trade exactly once — so the sweep cannot double-announce
     alongside the immediate claim, and the immediate claim stays because a
     summary a cycle late is worse than one that arrives at once.
"""

import ast
import inspect
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import bridge_trade, client_bot  # noqa: E402
from db.repo import Repo  # noqa: E402
from monitor import tron  # noqa: E402


# ----------------------------------------------------------------------
# The rule: no payment is recorded without the claim
# ----------------------------------------------------------------------

def _functions_that_add_payments(module):
    """
    Every function in a bot module that calls repo.add_payment or
    repo.resolve_held_payment — i.e. every place a payments row is born.

    Found by walking the AST rather than by name, so a path added next month
    is covered by this test the day it is written.
    """
    src = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    writers = {"add_payment", "resolve_held_payment"}
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in writers):
                found[node.name] = node
    return found


def _calls_in(node) -> set[str]:
    return {c.func.attr for c in ast.walk(node)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}


class TestEveryPaymentPathClaimsCompletion:
    @pytest.mark.parametrize("module", [client_bot, bridge_trade])
    def test_the_modules_have_payment_paths_to_check_at_all(self, module):
        """
        A guard on the guard. If the AST walk stops finding anything — a
        rename, a refactor — every assertion below would pass vacuously.
        """
        assert _functions_that_add_payments(module), (
            f"found no payment-writing function in {module.__name__}; this "
            "test has stopped checking anything"
        )

    @pytest.mark.parametrize("module", [client_bot, bridge_trade])
    def test_every_one_of_them_claims_completion(self, module):
        for name, node in _functions_that_add_payments(module).items():
            assert "check_completion" in _calls_in(node), (
                f"{module.__name__}.{name} records a payment but never asks "
                "whether the trade is now finished. SUPA43, 2 October 2026."
            )

    @pytest.mark.parametrize("module", [client_bot, bridge_trade])
    def test_every_one_of_them_also_checks_near_completion(self, module):
        """
        The supplier is told to prepare the next batch when the outstanding
        balance falls below the threshold. A path that skips this leaves them
        waiting on a nudge that never comes.
        """
        for name, node in _functions_that_add_payments(module).items():
            assert "check_near_completion" in _calls_in(node), (
                f"{module.__name__}.{name} records a payment but never checks "
                "near-completion, so the supplier is not told to prepare."
            )

    def test_the_quiet_branch_is_the_one_that_was_missed(self):
        """
        Named explicitly, because a generic sweep over functions is exactly
        the kind of test that keeps passing while the one case that matters
        is renamed out of its scope.
        """
        src = inspect.getsource(client_bot._place_on_shared_account)
        assert "check_completion" in src
        assert "check_near_completion" in src


# ----------------------------------------------------------------------
# The behaviour, not just the source
# ----------------------------------------------------------------------

class _Notifier:
    def __init__(self, completes=True):
        self.completes = completes
        self.completed: list[int] = []
        self.neared: list[int] = []
        self.asked: list[dict] = []

    async def check_completion(self, trade_id):
        self.completed.append(trade_id)
        return self.completes

    async def check_near_completion(self, trade_id):
        self.neared.append(trade_id)

    async def ask_which_order(self, **kw):
        self.asked.append(kw)


class _Repo:
    def __init__(self, trades, *, add_ok=True):
        self.trades = trades
        self.add_ok = add_ok
        self.added: list[dict] = []

    async def live_trades_on_account(self, **kw):
        return list(self.trades)

    async def add_payment(self, **kw):
        self.added.append(kw)
        return (self.add_ok, "recorded" if self.add_ok else "duplicate")

    async def hold_payment(self, **kw):
        return True, 1


class _P:
    def __init__(self):
        self.utr = "UTR0000000001"
        self.amount_inr = D("295000")
        self.beneficiary = "A SHARED HOLDER"


class _Message:
    class _Chat:
        id = 1
    chat = _Chat()
    message_id = 2


ACCOUNTS = [
    {"account_name": "A SHARED HOLDER", "account_number": "000000000000",
     "ifsc": "XXXX0000000"},
    {"account_name": "A SHARED HOLDER", "account_number": "000000000000",
     "ifsc": "XXXX0000000"},
]


def _trade(tid, collected, expected=D("5644500")):
    return {"id": tid, "account_id": 12, "collected": collected,
            "inr_expected": expected}


class TestTheEstablishedPathClosesTheTrade:
    @pytest.mark.asyncio
    async def test_a_payment_that_covers_the_total_closes_it(self):
        """SUPA43, exactly: the last payment on the established path."""
        trades = [_trade(43, D("5349500")), _trade(44, D(0))]
        repo, notifier = _Repo(trades), _Notifier(completes=True)

        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, repo, notifier,
        )
        assert out is not None, "the payment should have been placed"
        assert repo.added, "no payment was recorded"
        assert notifier.completed == [43]

    @pytest.mark.asyncio
    async def test_a_payment_that_does_not_finish_it_nudges_the_supplier(self):
        trades = [_trade(43, D("1000000")), _trade(44, D(0))]
        repo, notifier = _Repo(trades), _Notifier(completes=False)

        await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, repo, notifier,
        )
        assert notifier.completed == [43]
        assert notifier.neared == [43], (
            "not finished, so the supplier should have been considered for "
            "the prepare-the-next-batch notice"
        )

    @pytest.mark.asyncio
    async def test_a_closed_trade_is_not_also_nudged(self):
        trades = [_trade(43, D("5349500")), _trade(44, D(0))]
        notifier = _Notifier(completes=True)
        await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, _Repo(trades), notifier,
        )
        assert notifier.neared == []

    @pytest.mark.asyncio
    async def test_a_duplicate_claims_nothing(self):
        """
        add_payment refused, so no money moved and there is nothing to close.
        Claiming here would announce a completion on the strength of a
        payment that was never recorded.
        """
        trades = [_trade(43, D("5349500")), _trade(44, D(0))]
        notifier = _Notifier()
        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9},
            _Repo(trades, add_ok=False), notifier,
        )
        assert out is None
        assert notifier.completed == []

    @pytest.mark.asyncio
    async def test_a_held_payment_claims_nothing_either(self):
        """
        No established path, so the money is held and the Bridge asked.
        Nothing is on the ledger yet, so nothing can be finished by it.
        """
        trades = [_trade(43, D("1000000")), _trade(44, D("2000000"))]
        repo, notifier = _Repo(trades), _Notifier()
        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, repo, notifier,
        )
        assert "held" in out
        assert repo.added == []
        assert notifier.completed == []
        assert notifier.asked

    @pytest.mark.asyncio
    async def test_it_survives_having_no_notifier(self):
        """Tests and the edited-message path both call it without one."""
        trades = [_trade(43, D("5349500")), _trade(44, D(0))]
        out = await client_bot._place_on_shared_account(
            _Message(), _P(), ACCOUNTS, {"id": 9}, _Repo(trades), None,
        )
        assert out is not None


# ----------------------------------------------------------------------
# The net underneath
# ----------------------------------------------------------------------

class _SweepRepo:
    def __init__(self, ids):
        self.ids = list(ids)

    async def covered_but_open_trades(self):
        return list(self.ids)


def _monitor(repo, notifier):
    m = tron.DepositMonitor.__new__(tron.DepositMonitor)
    m.repo = repo
    m.notifier = notifier
    return m


class TestTheSweepCatchesWhatAPathForgets:
    @pytest.mark.asyncio
    async def test_a_fully_paid_open_trade_is_closed(self):
        notifier = _Notifier(completes=True)
        await _monitor(_SweepRepo([43]), notifier)._close_covered_trades()
        assert notifier.completed == [43]

    @pytest.mark.asyncio
    async def test_a_healthy_system_sweeps_nothing(self):
        notifier = _Notifier()
        await _monitor(_SweepRepo([]), notifier)._close_covered_trades()
        assert notifier.completed == []

    @pytest.mark.asyncio
    async def test_a_failure_in_the_sweep_does_not_stop_the_monitor(self):
        """
        Every sweep step is swallowed. Detecting deposits is the one job that
        must not be interrupted by a notice failing to send.
        """
        class _Boom:
            async def covered_but_open_trades(self):
                raise RuntimeError("database went away")

        await _monitor(_Boom(), _Notifier())._close_covered_trades()

    def test_the_sweep_runs_every_cycle(self):
        """
        Asserted against the body of poll_once itself, not the module. The
        first version of this searched the whole class and passed with the
        call deleted, because the method's own definition matched — a test
        that watched its subject exist rather than run.
        """
        cycle = inspect.getsource(tron.DepositMonitor.poll_once)
        assert "await self._close_covered_trades()" in cycle, (
            "the sweep is defined but never reached from the poll cycle"
        )

    def test_the_sweep_asks_exactly_what_the_claim_would_answer(self):
        """
        The query must mirror claim_completion's WHERE clause, or the sweep
        offers trades the claim refuses and the log fills with noise that
        means nothing.
        """
        sweep = inspect.getsource(Repo.covered_but_open_trades)
        claim = inspect.getsource(Repo.claim_completion)
        for condition in ("status = 'awaiting_payment'",
                          "inr_expected IS NOT NULL",
                          "inr_expected > 0",
                          ">= t.inr_expected"):
            assert condition in sweep, condition
            assert condition in claim, condition

    def test_the_sweep_does_not_close_trades_itself(self):
        """
        One place decides a trade is finished. The sweep asks; it does not
        answer, or there would be two definitions of done.
        """
        src = inspect.getsource(Repo.covered_but_open_trades)
        assert "UPDATE" not in src.upper().replace("UPDATED", "")
        assert "SELECT t.id" in src
