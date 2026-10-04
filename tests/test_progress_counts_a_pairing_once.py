"""
One line per pairing in /progress, and one line in the total.

WHAT HAPPENED

4 October 2026, 10:57pm. The Bridge ran /progress and sent back:

    Uncle → Haze & ProperPay: Exchange
      1 trade   ₹1,515,000 of ₹3,012,885   50%
      Outstanding  ₹1,497,885

    Uncle → Haze & ProperPay: Exchange
      1 trade   ₹1,515,000 of ₹3,012,885   50%
      Outstanding  ₹1,497,885

    Outstanding across the book  ₹8,320,770

The two lines are the same line. His own figures give it away:

    5,325,000 + 1,497,885 + 1,497,885 = 8,320,770   what it printed
    5,325,000 + 1,497,885             = 6,822,885   the truth

So the book read ₹1,497,885 high, on the one screen he uses to decide what
still needs collecting.

WHY

book_progress drives off `FROM wallets w`, one row per wallet. Uncle had two
internal wallet rows for that client: wallet 9, retired 28 September, and
wallet 10, its live replacement. Retiring and re-adding is allowed — the
uniqueness index is partial, `WHERE is_internal AND retired_at IS NULL` — and
it was Uncle and BIG BOSS this happened to, on exactly that date.

Every figure on the line is a subquery keyed on supplier_id and client_id,
never on w.id. So the two rows came out byte-identical, which is why it reads
as a display quirk and is actually a wrong total.

WHAT IS NOT THE FIX

Hiding retired pairings. A retired wallet can still hold USDT on a cancelled
trade that was never invoiced, and hiding that line is how ₹995,495 went
unnoticed twice. test_wallet_retire.py lists book_progress as deliberately
historical. The rule is "show retired pairings", and it was never "count them
twice".
"""

import inspect
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.repo import Repo  # noqa: E402


def _sql() -> str:
    src = inspect.getsource(Repo.book_progress)
    body = re.findall(r'"""(.*?)"""', src, re.S)[1]
    return " ".join(body.split())


def _code(sql: str) -> str:
    """The statement without its SQL comments, which discuss all of this."""
    src = inspect.getsource(Repo.book_progress)
    body = re.findall(r'"""(.*?)"""', src, re.S)[1]
    return " ".join(
        " ".join(re.sub(r"--.*$", "", line) for line in body.splitlines()).split()
    )


class TestAPairingIsCountedOnce:
    def test_the_query_deduplicates_by_pairing(self):
        assert "DISTINCT ON (w.supplier_id, w.client_id)" in _code(_sql()), (
            "/progress counts a pairing once per wallet row, so a retired and "
            "replaced wallet is added to the book twice"
        )

    def test_it_deduplicates_on_the_key_not_the_whole_row(self):
        """
        A plain DISTINCT happens to work today only because no column in the
        select list differs between the duplicate rows. The day someone
        selects w.address or w.id it would silently stop, and silently is how
        this one got out.
        """
        code = _code(_sql())
        assert "DISTINCT ON" in code
        assert re.search(r"SELECT\s+DISTINCT(?!\s+ON)", code) is None

    def test_retired_pairings_are_still_shown(self):
        """
        The whole point. Deduplicating must not become filtering: stranded
        USDT on a retired pairing is exactly what this screen exists to
        surface. ₹995,495, twice.
        """
        code = _code(_sql())
        assert "retired_at IS NULL" not in code, (
            "retired pairings have been filtered out of the book — that is "
            "the blind spot this screen was built to close"
        )

    def test_the_book_is_still_listed_alphabetically(self):
        """
        He reads it top to bottom. De-duplication must not reorder it into
        wallet-id order.
        """
        code = _code(_sql())
        tail = code[code.rindex("ORDER BY"):]
        assert tail.startswith("ORDER BY supplier_label, client_label"), tail

    def test_every_figure_is_keyed_on_the_pairing_not_the_wallet(self):
        """
        Why the duplicate rows were identical rather than split between them,
        and why de-duplicating is safe: no figure is attributable to one
        wallet row, so dropping a row drops nothing.
        """
        sql = _sql()
        subqueries = re.findall(r"\(SELECT[^()]*(?:\([^()]*\)[^()]*)*\)", sql)
        counted = [q for q in subqueries if "FROM trades t" in q
                   or "FROM deposits" in q]
        assert counted, "found no figures to check; this test has gone blind"
        for q in counted:
            assert "w.id" not in q, (
                f"a figure is keyed on the wallet row rather than the "
                f"pairing, so de-duplication would change it: {q[:120]}"
            )


class TestTheShapeThatCausedIt:
    def test_a_pairing_may_legitimately_hold_a_retired_and_a_live_wallet(self):
        """
        Pinning the premise. If the schema ever made this impossible the
        de-duplication would be dead code, and someone would rightly remove
        it — so the reason it is needed is asserted here too.
        """
        schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
        idx = schema[schema.index("wallets_pairing_unique"):]
        assert "retired_at IS NULL" in idx[:300], (
            "the pairing index is no longer partial, so a retired wallet and "
            "its replacement can no longer coexist"
        )
