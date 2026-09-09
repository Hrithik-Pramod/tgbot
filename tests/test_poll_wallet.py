"""
The polling loop itself.

Every other test covers a function the poller calls. Nothing covered the poller,
which is the piece that decides what becomes a deposit and what does not — and
it turned out to have a real gap: it accepted anything above zero.

A live probe on 9 September 2026 returned, among fifty transfers to one real
wallet, amounts of 0.00001 and 0.000101. That is dust: unsolicited
micro-transfers, usually address-poisoning attempts. Each one would have opened
a trade, consumed a reference number, and notified the Bridge about a fraction
of a rupee.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.tron import MAX_BACKOFF_CYCLES, OVERLAP_MS, DepositMonitor  # noqa: E402

D = Decimal


class _Config:
    def __init__(self, minimum="1"):
        self.min_deposit_amount = D(minimum)
        self.poll_interval_seconds = 20


class _Repo:
    def __init__(self):
        self.cursors = {}
        self.adopted = {}

    async def set_monitor_cursor(self, wallet_id, ts):
        self.cursors[wallet_id] = ts

    async def adopt_wallet(self, wallet_id, *, cursor_ms, adopted_at_ms):
        self.cursors[wallet_id] = cursor_ms
        self.adopted[wallet_id] = adopted_at_ms


class _Client:
    def __init__(self, transfers, fail=False):
        self.transfers = transfers
        self.fail = fail
        self.calls = 0

    async def incoming_transfers(self, address, since_ms):
        self.calls += 1
        if self.fail:
            raise RuntimeError("both providers down")
        return self.transfers


class _Notifier:
    def __init__(self):
        self.messages = []

    async def to_bridge(self, text, reply_markup=None):
        self.messages.append(text)


# An established wallet: already adopted, carrying a cursor. That is the normal
# state, so it is the default here. A wallet with last_timestamp_ms None is
# being seen for the first time and takes the adoption path instead — see
# TestAdoption.
WALLET = {"id": 1, "address": "TFLEpkCtXFSCYCvzqgtUENDaSUKcFUX2zb",
          "is_internal": True, "supplier_id": 1, "client_id": 2,
          "last_timestamp_ms": 1_699_000_000_000, "adopted_at_ms": None}


def transfer(amount, *, tx="a" * 64, ts=1_700_000_000_000):
    return {"tx_hash": tx, "amount": D(amount), "from_address": "TFrom",
            "timestamp_ms": ts, "block": 1, "confirmed": True}


def build(transfers, *, minimum="1", fail=False):
    handled = []
    monitor = DepositMonitor(repo=_Repo(), client=_Client(transfers, fail=fail),
                             notifier=_Notifier(), config=_Config(minimum))

    async def record(wallet, tr):
        handled.append(tr)

    monitor._handle_deposit = record
    return monitor, handled


class TestDustIsIgnored:
    @pytest.mark.asyncio
    async def test_the_amounts_seen_on_a_real_wallet(self):
        """The two observed live. Neither is a deposit."""
        monitor, handled = build([transfer("0.00001", tx="a" * 64),
                                  transfer("0.000101", tx="b" * 64)])
        await monitor._poll_wallet(WALLET)
        assert handled == []

    @pytest.mark.asyncio
    async def test_a_real_deposit_still_gets_through(self):
        monitor, handled = build([transfer("5000")])
        await monitor._poll_wallet(WALLET)
        assert len(handled) == 1
        assert handled[0]["amount"] == D("5000")

    @pytest.mark.asyncio
    async def test_dust_and_a_real_deposit_in_one_poll(self):
        """The realistic case — the floor must not discard the batch."""
        monitor, handled = build([transfer("0.000001", tx="c" * 64),
                                  transfer("5000", tx="d" * 64),
                                  transfer("0.00002", tx="e" * 64)])
        await monitor._poll_wallet(WALLET)
        assert [t["amount"] for t in handled] == [D("5000")]

    @pytest.mark.asyncio
    async def test_exactly_the_threshold_is_accepted(self):
        """`<` not `<=` — a deposit of exactly the minimum is a deposit."""
        monitor, handled = build([transfer("1")], minimum="1")
        await monitor._poll_wallet(WALLET)
        assert len(handled) == 1

    @pytest.mark.asyncio
    async def test_the_threshold_is_configurable_for_trx_testing(self):
        """
        TRX test runs use small amounts, so the floor has to be lowerable
        without a code change.
        """
        monitor, handled = build([transfer("0.5")], minimum="0.1")
        await monitor._poll_wallet(WALLET)
        assert len(handled) == 1

    @pytest.mark.asyncio
    async def test_the_cursor_advances_past_dust(self):
        """
        Otherwise every poll re-reads the same dust forever, and a wallet that
        attracts it never moves forward.
        """
        monitor, _ = build([transfer("0.00001", ts=1_700_000_000_000),
                            transfer("0.00002", tx="f" * 64, ts=1_700_000_500_000)])
        await monitor._poll_wallet(WALLET)
        assert monitor.repo.cursors == {1: 1_700_000_500_000}


class TestAdoption:
    """
    What happens the first time a wallet is watched.

    This is not a hypothetical. On 9 September 2026 the client supplied two of
    his live wallets for a test. /walletchange cleared the cursor, the next poll
    ran with no lower bound, and the provider returned the most recent existing
    transactions — which the monitor recorded as deposits that had just landed.
    The result was 38 deposits and one open trade for 188,167 USDT against
    ₹19,851,619, built from transfers dating back to June.

    A wallet with no cursor has just been handed to us. It is not a wallet we
    are behind on.
    """

    ADOPTED = dict(WALLET, last_timestamp_ms=None)

    @pytest.mark.asyncio
    async def test_existing_history_is_not_recorded(self):
        monitor, handled = build([
            transfer("9417", tx="a" * 64, ts=1_786_985_172_000),
            transfer("9417", tx="b" * 64, ts=1_786_985_173_000),
            transfer("9417", tx="c" * 64, ts=1_786_985_174_000),
        ])
        await monitor._poll_wallet(self.ADOPTED)
        assert handled == [], "history was ingested as new deposits"

    @pytest.mark.asyncio
    async def test_the_cursor_is_set_from_the_newest_existing_transaction(self):
        """
        Chain time, not the server's clock — so adoption does not depend on the
        server clock being correct — and offset past the overlap rewind.
        """
        monitor, _ = build([
            transfer("9417", tx="a" * 64, ts=1_786_985_172_000),
            transfer("9417", tx="b" * 64, ts=1_786_985_174_000),
        ])
        await monitor._poll_wallet(self.ADOPTED)
        assert monitor.repo.cursors == {1: 1_786_985_174_000 + OVERLAP_MS + 1}

    @pytest.mark.asyncio
    async def test_the_overlap_rewind_cannot_reach_back_into_ignored_history(self):
        """
        The regression guard for the second half of this bug.

        Adoption ignores history but records nothing, so there is no database
        row for the UNIQUE constraint to absorb a re-read. Every poll rewinds
        OVERLAP_MS for boundary safety — which, with the cursor set to `newest`,
        pulled the newest ignored transaction straight back in. In production
        that reported a 30 TRX transfer from 25 August as arriving on 10
        September.

        The test asserts the property that matters — after adoption, the next
        query starts strictly after the last ignored transaction — rather than
        the arithmetic that currently achieves it.
        """
        newest_ignored = 1_786_985_174_000
        monitor, handled = build([
            transfer("9417", tx="a" * 64, ts=1_786_985_172_000),
            transfer("9417", tx="b" * 64, ts=newest_ignored),
        ])
        await monitor._poll_wallet(self.ADOPTED)

        cursor = monitor.repo.cursors[1]
        next_query_starts_at = cursor - OVERLAP_MS
        assert next_query_starts_at > newest_ignored, (
            f"the next poll asks from {next_query_starts_at}, which still "
            f"includes the ignored transaction at {newest_ignored}"
        )

    @pytest.mark.asyncio
    async def test_an_empty_wallet_still_gets_a_cursor(self):
        """
        Otherwise it is adopted again on every single poll, and its real first
        deposit is skipped as 'history'.
        """
        monitor, _ = build([])
        await monitor._poll_wallet(self.ADOPTED)
        assert monitor.repo.cursors.get(1, 0) > 1_700_000_000_000

    @pytest.mark.asyncio
    async def test_the_bridge_is_told_what_was_ignored(self):
        """Silently discarding transactions is not acceptable, even correctly."""
        monitor, _ = build([transfer("9417", tx="a" * 64)])
        await monitor._poll_wallet(self.ADOPTED)
        assert len(monitor.notifier.messages) == 1
        msg = monitor.notifier.messages[0]
        assert "Now monitoring" in msg and "1 existing transaction" in msg

    @pytest.mark.asyncio
    async def test_adoption_happens_once_not_every_poll(self):
        """
        The second poll must behave normally — a real deposit arriving right
        after adoption has to be picked up.
        """
        monitor, handled = build([transfer("9417", tx="a" * 64,
                                           ts=1_786_985_172_000)])
        await monitor._poll_wallet(self.ADOPTED)
        assert handled == []

        # Same wallet, now carrying the cursor adoption gave it.
        settled = dict(WALLET, last_timestamp_ms=monitor.repo.cursors[1])
        monitor.client.transfers = [transfer("5000", tx="d" * 64,
                                             ts=1_786_985_999_000)]
        await monitor._poll_wallet(settled)
        assert [t["amount"] for t in handled] == [D("5000")]

    @pytest.mark.asyncio
    async def test_a_failing_api_does_not_adopt(self):
        """
        Adopting on a failed call would set a cursor from no information and
        skip everything between now and the next success.
        """
        monitor, _ = build([], fail=True)
        await monitor._poll_wallet(self.ADOPTED)
        assert monitor.repo.cursors == {}


class TestTheAdoptionBoundaryHolds:
    """
    The third and final attempt at this, and the reason the boundary moved out
    of the provider's query and into our own code.

    Attempt one set the cursor to the newest existing transaction. The overlap
    rewind walked back past it.

    Attempt two offset the cursor by OVERLAP_MS + 1, so the query would start
    one millisecond after that transaction. It still came back — TronScan
    truncates start_timestamp to whole seconds, verified live: asking from
    1787667471001 returns the transaction at 1787667471000.

    So the boundary cannot live in the request. These tests simulate a provider
    that ignores the filter entirely, which is the honest worst case, and
    require that nothing pre-adoption is ever treated as a deposit.
    """

    HISTORY_TS = 1_787_667_471_000

    def _settled_after_adoption(self, monitor):
        """The wallet as the next poll will load it from the database."""
        return dict(
            WALLET,
            last_timestamp_ms=monitor.repo.cursors[1],
            adopted_at_ms=monitor.repo.adopted[1],
        )

    @pytest.mark.asyncio
    async def test_history_stays_out_even_if_the_provider_ignores_the_filter(self):
        history = transfer("30", tx="2f948c99" + "0" * 56, ts=self.HISTORY_TS)
        monitor, handled = build([history])

        await monitor._poll_wallet(dict(WALLET, last_timestamp_ms=None))
        assert handled == []

        # The provider returns it again regardless of what we asked for.
        for _ in range(5):
            await monitor._poll_wallet(self._settled_after_adoption(monitor))

        assert handled == [], (
            "a pre-adoption transaction was recorded as a deposit — this is "
            "the 30 TRX from 25 August reappearing on 10 September"
        )

    @pytest.mark.asyncio
    async def test_a_genuine_deposit_after_adoption_is_still_seen(self):
        """The boundary must exclude history without excluding the test send."""
        history = transfer("30", tx="a" * 64, ts=self.HISTORY_TS)
        monitor, handled = build([history])
        await monitor._poll_wallet(dict(WALLET, last_timestamp_ms=None))

        real = transfer("10", tx="b" * 64, ts=self.HISTORY_TS + 60_000)
        monitor.client.transfers = [history, real]
        await monitor._poll_wallet(self._settled_after_adoption(monitor))

        assert [t["tx_hash"] for t in handled] == ["b" * 64]

    @pytest.mark.asyncio
    async def test_a_deposit_in_the_same_second_as_the_boundary_is_excluded(self):
        """
        Deliberate, and worth stating. A transaction sharing the adoption
        timestamp is indistinguishable from history, so it is treated as
        history. Losing it is acceptable; letting history through is not.
        """
        history = transfer("30", tx="a" * 64, ts=self.HISTORY_TS)
        monitor, handled = build([history])
        await monitor._poll_wallet(dict(WALLET, last_timestamp_ms=None))

        monitor.client.transfers = [transfer("10", tx="b" * 64, ts=self.HISTORY_TS)]
        await monitor._poll_wallet(self._settled_after_adoption(monitor))
        assert handled == []

    @pytest.mark.asyncio
    async def test_the_baseline_survives_a_restart(self):
        """
        It is stored, not held in memory. A restart re-reads it from the
        database, so history cannot creep back in overnight.
        """
        monitor, _ = build([transfer("30", tx="a" * 64, ts=self.HISTORY_TS)])
        await monitor._poll_wallet(dict(WALLET, last_timestamp_ms=None))
        assert monitor.repo.adopted[1] == self.HISTORY_TS

    @pytest.mark.asyncio
    async def test_an_established_wallet_is_unaffected(self):
        """
        A wallet adopted long ago, with no baseline recorded, must keep working
        — the column is nullable and old rows predate it.
        """
        monitor, handled = build([transfer("5000")])
        await monitor._poll_wallet(dict(WALLET, adopted_at_ms=None))
        assert len(handled) == 1


class TestOtherRejections:
    @pytest.mark.asyncio
    async def test_zero_and_negative_are_ignored(self):
        monitor, handled = build([transfer("0", tx="g" * 64),
                                  transfer("-5", tx="h" * 64)])
        await monitor._poll_wallet(WALLET)
        assert handled == []

    @pytest.mark.asyncio
    async def test_a_missing_hash_is_ignored(self):
        """Without a hash there is no idempotency key, so it cannot be stored."""
        bad = transfer("5000")
        bad["tx_hash"] = None
        monitor, handled = build([bad])
        await monitor._poll_wallet(WALLET)
        assert handled == []


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_the_bridge_is_told_after_five_failures(self):
        """
        Not on the first — a single blip is normal. Not never, either: a wallet
        that has stopped being polled is the worst failure this system has.
        """
        monitor, _ = build([], fail=True)
        for _ in range(20):
            await monitor._poll_wallet(WALLET)
        assert len(monitor.notifier.messages) == 1
        assert "may not be detected" in monitor.notifier.messages[0]

    @pytest.mark.asyncio
    async def test_a_failing_wallet_backs_off(self):
        """One bad address must not exhaust the API budget for the others."""
        monitor, _ = build([], fail=True)
        for _ in range(12):
            await monitor._poll_wallet(WALLET)
        assert monitor.client.calls < 12, "no backoff — every cycle hit the API"

    @pytest.mark.asyncio
    async def test_the_alert_is_reachable_at_all(self):
        """
        The regression guard for a bug this file found.

        One counter used to serve as both the failure count and the skip
        count, so the escalation threshold was only ever crossed on a cycle
        that skipped the API call instead of making it. The condition could
        not be true during a failure, and the Bridge was never told that
        deposit detection had stopped.

        Stated as "it fires within a bounded number of cycles" rather than
        "on cycle N", so a change to the backoff curve does not fail this
        test — only a change that makes it unreachable again.
        """
        monitor, _ = build([], fail=True)
        for cycle in range(60):
            await monitor._poll_wallet(WALLET)
            if monitor.notifier.messages:
                assert monitor._failures[1] == 5
                return
        pytest.fail("the Bridge was never told, after 60 cycles of failure")

    @pytest.mark.asyncio
    async def test_it_keeps_reminding_rather_than_going_quiet(self):
        """One message hours ago is easy to miss."""
        monitor, _ = build([], fail=True)
        for _ in range(400):
            await monitor._poll_wallet(WALLET)
        assert len(monitor.notifier.messages) > 1
        assert monitor.notifier.messages[-1] != monitor.notifier.messages[0], \
            "later alerts should carry the current failure count"

    @pytest.mark.asyncio
    async def test_recovery_clears_the_backoff(self):
        """
        After a recovery the wallet must be polled every cycle again, not stay
        throttled because of an outage that is over.
        """
        monitor, handled = build([transfer("5000")], fail=True)
        for _ in range(12):
            await monitor._poll_wallet(WALLET)

        monitor.client.fail = False
        # Enough cycles to get past the longest backoff gap and land on an
        # attempt, which is what clears the counters.
        for _ in range(MAX_BACKOFF_CYCLES + 2):
            await monitor._poll_wallet(WALLET)

        assert monitor._failures == {} and monitor._skips == {}
        before = monitor.client.calls
        await monitor._poll_wallet(WALLET)
        assert monitor.client.calls == before + 1, "still throttled after recovery"

    @pytest.mark.asyncio
    async def test_the_cursor_is_untouched_when_the_api_fails(self):
        """
        Moving it on a failure would skip the window that failed, and those
        deposits would never be seen again.
        """
        monitor, _ = build([], fail=True)
        await monitor._poll_wallet(WALLET)
        assert monitor.repo.cursors == {}
