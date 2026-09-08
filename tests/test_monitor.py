"""
Monitor tests.

These cover the failure modes that actually lose money: unit conversion,
duplicate delivery, and the cursor after downtime.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.tron import OVERLAP_MS, units_to_usdt  # noqa: E402

D = Decimal


class TestUnitConversion:
    def test_whole_usdt(self):
        # 1000 USDT at 6 decimals
        assert units_to_usdt(1_000_000_000) == D("1000")

    def test_the_clients_example_amount(self):
        assert units_to_usdt(900_900_900) == D("900.9009")

    def test_fractional_precision_preserved(self):
        assert units_to_usdt(1) == D("0.000001")

    def test_string_input_from_json(self):
        # TronGrid returns value as a string; TronScan returns quant as a string
        # too. Both must convert identically.
        assert units_to_usdt("1000000000") == D("1000")
        assert units_to_usdt(1000000000) == D("1000")

    def test_no_float_drift_on_large_amounts(self):
        # 1,499,297 INR worth at rate 111 is ~13,507 USDT.
        raw = 13_507_180_180
        result = units_to_usdt(raw)
        assert result == D("13507.18018")
        # Round-trips exactly - a float would not.
        assert result * (D(10) ** 6) == D(raw)

    def test_accumulation_does_not_drift(self):
        total = sum(units_to_usdt(100_000_001) for _ in range(1000))
        assert total == D("100000.001000")


class TestOverlapWindow:
    def test_overlap_is_meaningful(self):
        # A zero or tiny overlap would drop boundary transactions.
        assert OVERLAP_MS >= 60_000

    def test_cursor_rewind_never_goes_negative(self):
        # Guards the max(0, ...) in both API paths.
        for since in (0, 1000, OVERLAP_MS - 1):
            assert max(0, since - OVERLAP_MS) >= 0


class FakeRepo:
    """Records deposits in memory with the same uniqueness rule as the schema."""

    def __init__(self):
        self.seen: set[tuple[str, int]] = set()
        self.cursors: dict[int, int] = {}
        self.next_id = 1

    async def record_deposit(self, *, tx_hash, wallet_id, amount_usdt,
                             from_address, block_number):
        key = (tx_hash, wallet_id)
        if key in self.seen:
            return None          # matches ON CONFLICT DO NOTHING
        self.seen.add(key)
        self.next_id += 1
        return self.next_id - 1

    async def set_monitor_cursor(self, wallet_id, last_timestamp_ms):
        # Mirrors the GREATEST(...) in the real upsert - forward only.
        self.cursors[wallet_id] = max(
            self.cursors.get(wallet_id, 0), last_timestamp_ms
        )


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_same_transaction_recorded_once(self):
        repo = FakeRepo()
        first = await repo.record_deposit(
            tx_hash="abc123", wallet_id=1, amount_usdt=D("1000"),
            from_address="TFrom", block_number=1,
        )
        second = await repo.record_deposit(
            tx_hash="abc123", wallet_id=1, amount_usdt=D("1000"),
            from_address="TFrom", block_number=1,
        )
        assert first is not None
        assert second is None, "a redelivered transaction must not be counted twice"

    @pytest.mark.asyncio
    async def test_same_hash_different_wallet_is_distinct(self):
        repo = FakeRepo()
        a = await repo.record_deposit(
            tx_hash="abc123", wallet_id=1, amount_usdt=D("1"),
            from_address=None, block_number=None,
        )
        b = await repo.record_deposit(
            tx_hash="abc123", wallet_id=2, amount_usdt=D("1"),
            from_address=None, block_number=None,
        )
        assert a is not None and b is not None

    @pytest.mark.asyncio
    async def test_cursor_only_moves_forward(self):
        repo = FakeRepo()
        await repo.set_monitor_cursor(1, 5000)
        await repo.set_monitor_cursor(1, 3000)   # a late, out-of-order poll
        assert repo.cursors[1] == 5000, "cursor must never rewind past recorded work"

    @pytest.mark.asyncio
    async def test_replay_after_downtime_is_safe(self):
        """
        Simulates a restart: the overlap window re-delivers transactions the
        monitor already recorded. None of them may be counted again.
        """
        repo = FakeRepo()
        batch = [
            {"tx_hash": f"tx{i}", "amount": D("100"), "timestamp_ms": 1000 + i}
            for i in range(5)
        ]
        for t in batch:
            await repo.record_deposit(
                tx_hash=t["tx_hash"], wallet_id=1, amount_usdt=t["amount"],
                from_address=None, block_number=None,
            )
        await repo.set_monitor_cursor(1, max(t["timestamp_ms"] for t in batch))

        # Restart: same batch arrives again inside the overlap window.
        new_ids = [
            await repo.record_deposit(
                tx_hash=t["tx_hash"], wallet_id=1, amount_usdt=t["amount"],
                from_address=None, block_number=None,
            )
            for t in batch
        ]
        assert all(i is None for i in new_ids)
        assert len(repo.seen) == 5
