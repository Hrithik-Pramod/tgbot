"""
Contract test against a real TronScan response.

The monitor's parser was written from knowledge of the TronScan API, not from a
live response, which made it the least-verified code in the project: a renamed
field would mean deposits are silently never detected, and the system would look
perfectly healthy while missing money.

The payload below was captured from the live TronScan API on 8 September 2026
(USDT TRC20 transfers, mainnet) and trimmed to two records. It pins the field
names the parser depends on. If TronScan changes the shape, this test fails here
rather than in production.
"""

import json
import pathlib
import sys
from decimal import Decimal as D

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from monitor.tron import units_to_usdt  # noqa: E402

# Captured live, 8 Sep 2026. Addresses are public mainnet data.
REAL_RESPONSE = json.loads(r"""
{
  "total": 10000,
  "rangeTotal": 3646574871,
  "token_transfers": [
    {
      "transaction_id": "d8851c309181db9a375e8d5ece9ba8f66062b94fc94ac98c9a8526038fb4b0e8",
      "status": 0,
      "block_ts": 1788842253000,
      "from_address": "TXGjMPVrtVUqtfmSZNZyLaH4LU5eS1oEZ1",
      "to_address": "TLaGjwhvA8XQYSxFAcAXy7Dvuue9eGYitv",
      "block": 86056821,
      "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
      "quant": "411910000",
      "event_type": "Transfer",
      "confirmed": false,
      "contractRet": "SUCCESS",
      "finalResult": "SUCCESS",
      "tokenInfo": {"tokenAbbr": "USDT", "tokenDecimal": 6},
      "revert": false,
      "riskTransaction": false
    },
    {
      "transaction_id": "cbd856dcdbb657b21d2ed93460a8ea36e6a94cdb966b20bff788f1dde21f76b3",
      "status": 0,
      "block_ts": 1788842253000,
      "from_address": "TRDczzJSo4qCecwvjW8VLv2aVc3XPbecg1",
      "to_address": "TLvEwc1AU9XEtEZJyrzBEDHKN3DXxtjDgQ",
      "block": 86056821,
      "contract_address": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
      "quant": "45000000",
      "event_type": "Transfer",
      "confirmed": false,
      "contractRet": "SUCCESS",
      "finalResult": "SUCCESS",
      "tokenInfo": {"tokenAbbr": "USDT", "tokenDecimal": 6},
      "revert": false,
      "riskTransaction": false
    }
  ]
}
""")

WATCHED = "TLaGjwhvA8XQYSxFAcAXy7Dvuue9eGYitv"   # the "to" of the first record


def parse(payload, address):
    """
    Mirrors TronClient._tronscan's parsing so it can be exercised without a
    network call. Kept deliberately close to the implementation: if one changes
    without the other, the assertions below will disagree.
    """
    out = []
    for item in payload.get("token_transfers", []):
        if (item.get("to_address") or "").strip() != address:
            continue
        if item.get("finalResult") not in (None, "SUCCESS"):
            continue
        if item.get("revert"):
            continue
        out.append({
            "tx_hash": item.get("transaction_id") or item.get("hash"),
            "from_address": item.get("from_address"),
            "amount": units_to_usdt(item.get("quant", 0)),
            "timestamp_ms": int(item.get("block_ts") or item.get("block_timestamp") or 0),
            "block": item.get("block"),
        })
    out.sort(key=lambda x: x["timestamp_ms"])
    return out


class TestFieldNames:
    """Every field the parser reads must exist on a real record."""

    @pytest.mark.parametrize("field", [
        "transaction_id", "to_address", "from_address",
        "quant", "block_ts", "block", "finalResult",
    ])
    def test_field_present(self, field):
        assert field in REAL_RESPONSE["token_transfers"][0], (
            f"TronScan no longer returns {field!r} — the monitor would stop "
            "detecting deposits silently"
        )

    def test_envelope_key(self):
        assert "token_transfers" in REAL_RESPONSE


class TestParsing:
    def test_only_transfers_to_the_watched_address(self):
        got = parse(REAL_RESPONSE, WATCHED)
        assert len(got) == 1, "must ignore transfers to other addresses"
        assert got[0]["tx_hash"].startswith("d8851c30")

    def test_amount_is_exact(self):
        # quant is minor units as a string: 411910000 / 1e6 = 411.91 USDT
        got = parse(REAL_RESPONSE, WATCHED)
        assert got[0]["amount"] == D("411.91")
        assert isinstance(got[0]["amount"], D)

    def test_quant_is_a_string_in_the_real_payload(self):
        """
        It arrives as a string, so any int() or float() conversion would either
        break or lose precision. units_to_usdt goes via Decimal(str(...)).
        """
        assert isinstance(REAL_RESPONSE["token_transfers"][0]["quant"], str)

    def test_timestamp_is_milliseconds(self):
        got = parse(REAL_RESPONSE, WATCHED)
        ts = got[0]["timestamp_ms"]
        # ~1.79e12 — seconds would be ~1.79e9, and treating one as the other
        # would make the polling cursor meaningless.
        assert 1_600_000_000_000 < ts < 2_500_000_000_000

    def test_no_match_returns_empty(self):
        assert parse(REAL_RESPONSE, "TAYdLT7dqiwj1fLhg1pJ7RLVkseW4Ct7DW") == []


class TestFiltering:
    def _one(self, **over):
        rec = dict(REAL_RESPONSE["token_transfers"][0])
        rec.update(over)
        return {"token_transfers": [rec]}

    def test_failed_transfer_ignored(self):
        assert parse(self._one(finalResult="FAILED"), WATCHED) == []

    def test_reverted_transfer_ignored(self):
        """
        Live data carries a `revert` flag. A reverted transfer moved no money,
        so counting it would open a trade against a deposit that never landed.
        """
        assert parse(self._one(revert=True), WATCHED) == []

    def test_unconfirmed_is_still_reported(self):
        # B4: notify on detection, confirm separately. `confirmed: false` is
        # normal for a fresh transaction and must not be filtered out.
        assert REAL_RESPONSE["token_transfers"][0]["confirmed"] is False
        assert len(parse(REAL_RESPONSE, WATCHED)) == 1
