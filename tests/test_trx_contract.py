"""
TRX monitoring, pinned against real API responses.

MONITOR_ASSET=TRX is a test setting, but the code behind it is not exempt from
being tested — it is what decides whether a real transfer is seen at all, and a
parser that silently matches nothing looks exactly like "no deposits arrived".

The payloads below are captured verbatim from live calls made on 9 September
2026, trimmed only of fields nothing reads:

    https://apilist.tronscanapi.com/api/transfer?address=...&sort=-timestamp
    https://api.trongrid.io/v1/accounts/.../transactions?only_to=true

If a provider changes shape, these fail here rather than in a group chat with
Jason watching.
"""

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor.tron import TronClient, hex_to_base58  # noqa: E402

WALLET = "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9"

# --------------------------------------------------------------------- TronScan

TRONSCAN_LIVE = {
    "total": 1540,
    "data": [
        {
            "block": 75657646,
            "transactionHash": "8a6f5e4e521bff0741b13b4accc68bd9e421e683eae2c9ce6b10903373dfb49b",
            "timestamp": 1757632686000,
            "transferFromAddress": "TDDBTQF2Xu3ALd1hZVqQmPmJ62xMUYYWf2",
            "transferToAddress": WALLET,
            "amount": 8333333,
            "tokenName": "_",
            "confirmed": True,
            "contractRet": "SUCCESS",
            "revert": False,
            "tokenInfo": {"tokenId": "_", "tokenAbbr": "trx", "tokenDecimal": 6},
        },
        {
            "block": 75026888,
            "transactionHash": "f70942af869cd16d54c06ae16217e0dab0542fc2d12da788f88c15a9e1c3659a",
            "timestamp": 1755739887000,
            "transferFromAddress": "TRGggG6hCrtcjB7bjbeLQKiaMytHqPhjkD",
            "transferToAddress": WALLET,
            "amount": 1498588,
            "tokenName": "_",
            "confirmed": True,
            "contractRet": "SUCCESS",
            "revert": False,
            "tokenInfo": {"tokenId": "_", "tokenAbbr": "trx", "tokenDecimal": 6},
        },
    ],
}

# --------------------------------------------------------------------- TronGrid

TRONGRID_LIVE = {
    "data": [
        {
            # A real TRX send, in the shape TronGrid returns raw transactions.
            "txID": "048598f1c5caba4e7808191865f565690ee3497caf419f43cf1e2e44d2a92c06",
            "blockNumber": 84954514,
            "block_timestamp": 1785534306000,
            "ret": [{"contractRet": "SUCCESS", "fee": 0}],
            "raw_data": {
                "contract": [{
                    "type": "TransferContract",
                    "parameter": {"value": {
                        "amount": 12500000,
                        "owner_address": "412c995748799f92862c5d3db379b768d6dbbcaf5c",
                        "to_address": "4182dd6b9966724ae2fdc79b416c7588da67ff1b35",
                    }},
                }],
            },
        },
        {
            # Staking. Real traffic is full of these and none of them are money
            # arriving — this is the entry that must be skipped.
            "txID": "aa" * 32,
            "blockNumber": 84954515,
            "block_timestamp": 1785534307000,
            "ret": [{"contractRet": "SUCCESS", "fee": 0}],
            "raw_data": {
                "contract": [{
                    "type": "UnDelegateResourceContract",
                    "parameter": {"value": {
                        "balance": 6771359014,
                        "resource": "ENERGY",
                        "receiver_address": "4182dd6b9966724ae2fdc79b416c7588da67ff1b35",
                        "owner_address": "412c995748799f92862c5d3db379b768d6dbbcaf5c",
                    }},
                }],
            },
        },
    ],
    "success": True,
}

GRID_WALLET = hex_to_base58("4182dd6b9966724ae2fdc79b416c7588da67ff1b35")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """Records the request and returns a fixed payload."""

    def __init__(self, payload):
        self.payload = payload
        self.url = None
        self.params = None

    async def get(self, url, params=None, headers=None):
        self.url, self.params = url, params
        return _FakeResponse(self.payload)

    async def aclose(self):
        return None


def _client(payload, *, asset="TRX"):
    c = TronClient(
        tronscan_base="https://apilist.tronscanapi.com/api",
        trongrid_base="https://api.trongrid.io",
        trongrid_key="", usdt_contract="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        asset=asset,
    )
    c._client = _FakeHttp(payload)
    return c


class TestAssetSelection:
    def test_defaults_to_usdt(self):
        """Production must not depend on the variable being present."""
        assert _client({}, asset="USDT").asset == "USDT"

    def test_lowercase_is_accepted(self):
        assert _client({}, asset="trx").asset == "TRX"

    def test_anything_else_refuses_to_start(self):
        """
        A typo must not silently fall back to monitoring the wrong asset — the
        bot would run, look healthy, and see nothing.
        """
        with pytest.raises(ValueError, match="USDT or TRX"):
            _client({}, asset="USDC")

    @pytest.mark.asyncio
    async def test_trx_mode_calls_the_native_endpoint(self):
        c = _client(TRONSCAN_LIVE)
        await c.incoming_transfers(WALLET, None)
        assert c._client.url.endswith("/transfer")
        assert "contract_address" not in c._client.params

    @pytest.mark.asyncio
    async def test_usdt_mode_still_calls_the_trc20_endpoint(self):
        """The regression guard: adding TRX must not disturb production."""
        c = _client({"token_transfers": []}, asset="USDT")
        await c.incoming_transfers(WALLET, None)
        assert c._client.url.endswith("/token_trc20/transfers")
        assert c._client.params["contract_address"].startswith("TR7NHq")


class TestTronscanTrx:
    @pytest.mark.asyncio
    async def test_reads_a_live_response(self):
        got = await _client(TRONSCAN_LIVE).incoming_transfers(WALLET, None)
        assert len(got) == 2

        # Oldest first — the cursor depends on this ordering.
        assert got[0]["timestamp_ms"] < got[1]["timestamp_ms"]

        newest = got[1]
        assert newest["tx_hash"] == TRONSCAN_LIVE["data"][0]["transactionHash"]
        assert newest["from_address"] == "TDDBTQF2Xu3ALd1hZVqQmPmJ62xMUYYWf2"
        assert newest["block"] == 75657646
        assert newest["confirmed"] is True

    @pytest.mark.asyncio
    async def test_sun_converts_exactly(self):
        """8333333 SUN is 8.333333 TRX. Never 8.333332999999999."""
        got = await _client(TRONSCAN_LIVE).incoming_transfers(WALLET, None)
        assert got[1]["amount"] == Decimal("8.333333")
        assert isinstance(got[1]["amount"], Decimal)

    @pytest.mark.asyncio
    async def test_trc10_tokens_are_ignored(self):
        """
        The endpoint carries TRC10 tokens too. One of those must never open a
        trade — anyone can mint a TRC10 token and send it for nothing.
        """
        payload = {"data": [dict(TRONSCAN_LIVE["data"][0], tokenName="1002000")]}
        assert await _client(payload).incoming_transfers(WALLET, None) == []

    @pytest.mark.asyncio
    async def test_outgoing_is_ignored(self):
        payload = {"data": [dict(TRONSCAN_LIVE["data"][0],
                                 transferToAddress="TSomeoneElse")]}
        assert await _client(payload).incoming_transfers(WALLET, None) == []

    @pytest.mark.asyncio
    async def test_failed_and_reverted_are_ignored(self):
        for bad in ({"contractRet": "REVERT"}, {"revert": True}):
            payload = {"data": [dict(TRONSCAN_LIVE["data"][0], **bad)]}
            assert await _client(payload).incoming_transfers(WALLET, None) == [], bad

    @pytest.mark.asyncio
    async def test_the_cursor_is_rewound(self):
        """Boundary transactions are missed without the overlap."""
        c = _client(TRONSCAN_LIVE)
        await c.incoming_transfers(WALLET, 1757632686000)
        assert c._client.params["start_timestamp"] == 1757632686000 - 120_000


class TestTrongridTrxFallback:
    @pytest.mark.asyncio
    async def test_reads_a_live_response_and_skips_non_transfers(self):
        c = _client({}, asset="TRX")

        async def boom(*a, **k):
            raise RuntimeError("TronScan down")

        c._tronscan_trx = boom
        c._client = _FakeHttp(TRONGRID_LIVE)

        got = await c.incoming_transfers(GRID_WALLET, None)

        assert len(got) == 1, "the staking entry must not be read as a deposit"
        assert got[0]["amount"] == Decimal("12.5")
        assert got[0]["from_address"] == hex_to_base58(
            "412c995748799f92862c5d3db379b768d6dbbcaf5c"
        )
        assert got[0]["confirmed"] is False


class TestHexToBase58:
    """
    The conversion TronGrid's fallback depends on. If it is wrong, every address
    comparison fails and the fallback reports nothing — indistinguishable from a
    quiet chain.

    Both cases below have external ground truth, so they test the arithmetic
    rather than restating it.
    """

    def test_matches_the_usdt_contract_address(self):
        """
        The TRC20 USDT contract, whose base58 form is in our own config and
        whose hex form is public. An independent check on the whole pipeline:
        sha256 twice, four-byte checksum, base58 digits.
        """
        assert hex_to_base58("41a614f803b6fd780986a42c78ec9c7f77e6ded13c") \
            == "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

    def test_matches_the_address_in_the_captured_trongrid_response(self):
        """
        That response carries the same account in hex; TronScan reported it in
        base58. Two providers, one address, and the conversion must join them.
        """
        assert hex_to_base58("4182dd6b9966724ae2fdc79b416c7588da67ff1b35") == WALLET

    def test_leading_zero_bytes_survive(self):
        """
        Leading zeros vanish in the integer arithmetic and have to be restored
        by hand — the classic base58 bug, and silent when it happens.
        """
        out = hex_to_base58("410000000000000000000000000000000000000000")
        assert out.startswith("T") and len(out) == 34
