"""
A split conserves every figure, and can be drawn on a round rupee number.

WHAT HAPPENED — 16 November 2026

    1 dollar over on 1 trade, another 1 dollar under ... it was the first
    split trade i have done
    — Bridge

66,843.569 USDT halved into BRAV81 and BRAV82:

    BRAV81   expected 3,549,394   paid 3,549,393   -1
    BRAV82   expected 3,549,393   paid 3,549,394   +1

Nothing was lost — the pair summed to 7,098,787 on both sides. The two
halves were a RUPEE APART, they collect into the same bank account, and
nothing on a payment slip says which half it was meant for. The client paid
them the other way round. One order then sat open a rupee short while its
twin sat a rupee over.

No care at the desk prevents that. Two figures one rupee apart are a
transposition waiting to happen.

TWO CHANGES

  Split on rupees.   He can give the first part as a round figure —
                     ₹3,550,000 and ₹3,548,787 instead of ₹3,549,394 and
                     ₹3,549,393 — and that number is used verbatim.

  The second part is the remainder.
                     It was priced independently from its own USDT, with
                     the docstring arguing that "a difference of rounded
                     figures is not the rounding of the difference". True,
                     and the wrong invariant: three separate roundings mean
                     the children need not add back to the parent. On
                     16 November they happened to, which is luck — the
                     second part's USDT is itself rounded to 2dp from a
                     deposit carried to 6, so half a rupee of drift is
                     available on every split.

WHAT THE RUPEE HAD TO DO WITH SPLITTING AT ALL

    also check why this happened first time using the splitting a deal, is
    it related? — Bridge

Yes, and not because splitting is broken. A split is the only thing that
produces two orders with nearly equal figures collecting into one account.
Two ordinary trades differ by lakhs, so a transposed rupee has nowhere to
hide.
"""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.money import inr_to_usdt, round_usdt, usdt_to_inr  # noqa: E402

# The fakes that the original split tests use.
from tests.test_split_into_two_orders import _Conn, _repo  # noqa: E402

SUPPLY, SELL = D("106.2"), D("107.7")
TOTAL_USDT = D("10000")
TOTAL_INR = usdt_to_inr(TOTAL_USDT, SUPPLY)        # 1,062,000
TOTAL_OWED = inr_to_usdt(TOTAL_INR, SELL)          # 9,860.72


async def _split(**kw):
    conn = _Conn()
    ok, msg, detail = await _repo(conn).split_trade(
        trade_id=7, actor_party_id=1, **kw
    )
    return ok, msg, detail


class TestItAlwaysAddsUp:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("first", ["1", "3333.33", "4000", "9999.99",
                                       "0.01", "5000"])
    async def test_every_column_sums_back_to_the_parent(self, first):
        """
        The property the 16 November split got right by luck.
        """
        ok, _, d = await _split(first_usdt=D(first))
        assert ok
        assert d["first"]["usdt"] + d["second"]["usdt"] == TOTAL_USDT
        assert (d["first"]["inr_expected"] + d["second"]["inr_expected"]
                == TOTAL_INR)
        assert d["first"]["usdt_owed"] + d["second"]["usdt_owed"] == TOTAL_OWED

    @pytest.mark.asyncio
    async def test_the_first_part_is_priced_properly_in_its_own_right(self):
        """
        Only the SECOND part absorbs the residue. The first is what he
        asked for, priced from its own USDT at the deposit's own rates.
        """
        _, _, d = await _split(first_usdt=D("4000"))
        assert d["first"]["inr_expected"] == usdt_to_inr(D("4000"), SUPPLY)
        assert d["first"]["usdt_owed"] == inr_to_usdt(
            d["first"]["inr_expected"], SELL)


class TestSplittingOnRupees:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("asked", ["400000", "400006", "400023", "400040"])
    async def test_the_figure_he_types_is_the_figure_the_client_gets(self, asked):
        """
        The whole point of the option: used verbatim, not converted to USDT
        and back.

        The round trip loses a rupee only on some values — ₹400,000 survives
        it, ₹400,006 does not (it comes back as ₹400,005, because one step
        of USDT is ₹1.062 and the rupee does not divide evenly into it). A
        mutation that re-derived the figure passed on ₹400,000 alone, so the
        awkward values are named here. Those are exactly the ones he would
        never notice going wrong.
        """
        _, _, d = await _split(first_inr=D(asked))
        assert d["first"]["inr_expected"] == D(asked)

    @pytest.mark.asyncio
    async def test_the_other_half_is_the_rest_to_the_rupee(self):
        _, _, d = await _split(first_inr=D("400000"))
        assert d["second"]["inr_expected"] == TOTAL_INR - D("400000")

    @pytest.mark.asyncio
    async def test_the_two_halves_are_not_adjacent_figures(self):
        """
        The 16 November shape, reproduced and avoided. Halving by USDT
        gives two figures a rupee apart; naming a round rupee figure does
        not.
        """
        _, _, usd = await _split(first_usdt=D("5000"))
        gap_usd = abs(usd["first"]["inr_expected"]
                      - usd["second"]["inr_expected"])
        _, _, inr = await _split(first_inr=D("500000"))
        gap_inr = abs(inr["first"]["inr_expected"]
                      - inr["second"]["inr_expected"])
        assert gap_usd < 10, "premise changed: an even USDT split is adjacent"
        assert gap_inr > 10000

    @pytest.mark.asyncio
    async def test_the_usdt_still_splits_and_still_sums(self):
        _, _, d = await _split(first_inr=D("400000"))
        assert d["first"]["usdt"] + d["second"]["usdt"] == TOTAL_USDT
        assert d["first"]["usdt"] == round_usdt(D("400000") / SUPPLY)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["0", "-1", "1062000", "2000000"])
    async def test_a_figure_that_leaves_no_second_part_is_refused(self, bad):
        ok, msg, _ = await _split(first_inr=D(bad))
        assert not ok
        assert "second part" in msg or "more than zero" in msg


class TestOneUnitOrTheOther:
    @pytest.mark.asyncio
    async def test_both_at_once_is_refused(self):
        """
        Two answers to one question. Picking either silently would make the
        other one a lie on screen.
        """
        ok, msg, _ = await _split(first_usdt=D("4000"), first_inr=D("400000"))
        assert not ok and "not both" in msg

    @pytest.mark.asyncio
    async def test_neither_is_refused(self):
        ok, msg, _ = await _split()
        assert not ok and "not neither" in msg


class TestTheScreenOffersIt:
    def test_the_rupee_button_is_on_the_split_screen(self):
        src = (ROOT / "bot" / "bridge_trade.py").read_text(encoding="utf-8")
        assert 'callback_data="spinr"' in src
        assert "Enter it in ₹ instead" in src

    def test_the_handler_passes_the_right_unit_through(self):
        import inspect
        from bot import bridge_trade
        src = inspect.getsource(bridge_trade.split_amount_typed)
        assert '"first_inr": first' in src
        assert '"first_usdt": first' in src
        assert 'data.get("split_unit") == "inr"' in src

    def test_usdt_remains_the_default(self):
        """
        He has split in USDT since October. The rupee entry is an offer,
        not a change of the thing he already knows.
        """
        import inspect
        from bot import bridge_trade
        src = inspect.getsource(bridge_trade.split_start)
        assert 'split_unit="usdt"' in src
