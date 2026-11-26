"""
A rate-limited API is a failure, not a quiet wallet.

WHAT IT COST — 26 NOVEMBER 2026, 23:04

37,843 USDT landed on a monitored wallet and no trade opened.

    a trade was sent but the bot was set to monitor wrong wallet
    i need to issue
    — Bridge, 23:24

The wallet was not wrong. TSs6Bjj... was wallet 11, Raj's, is_monitored
true, polled at 23:20:16 with consecutive_errors = 0 — and a cursor still
sitting at 25 November 23:06:48, a full day behind a transfer that had been
confirmed on chain for sixteen minutes.

A wallet watched perfectly, seeing nothing.

TronScan rate-limits with HTTP 200 and a body of:

    {"Error": "request rate exceeded the allowed_rps(3), and the query
               server is suspended for 54 s..."}

raise_for_status() passes on a 200. payload.get("token_transfers", []) on a
body with no such key returns []. So:

  - the throttle read as "no transfers"
  - the poll was recorded as a success
  - the cursor advanced past the window containing the deposit
  - consecutive_errors stayed at 0, so no backoff and no escalation
  - the TronGrid fallback, whose entire purpose is this, never engaged

The monitor reported success for not looking. We were also the cause: four
wallets polled back to back consume the whole 3 rps allowance, so the
throttle is the NORMAL failure, not a rare one — and it was the one failure
mode the fallback could not see.

WHAT WAS ALREADY WRITTEN DOWN

incoming_transfers has said this since it was written:

    If both fail the exception propagates so the caller can back off -
    returning an empty list here would look identical to "no deposits",
    which would silently hide a broken monitor.

The intent was right. Only the HTTP status was being checked, and a 200 is
not an answer — a parsed body carrying the expected key is.

THE DISTINCTION THIS FILE EXISTS TO PROTECT

An empty list under the expected key means nothing arrived, and must pass
through silently. A missing key means the body is not a transfer list, and
must raise. Collapsing those two is the bug, and it is a one-character
change away at all times: payload.get(key, []) brings it straight back.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor.tron import UpstreamError, _rows  # noqa: E402

# Verbatim, as TronScan sent it on 26 November 2026.
THROTTLED = {
    "Error": "request rate exceeded the allowed_rps(3), and the query server "
             "is suspended for 54 s. To obtain higher request quotas and a "
             "more stable service, it is recommended to authenticate with an "
             "API Key."
}


class TestTheThrottleRaises:
    def test_the_exact_body_that_hid_the_deposit(self):
        with pytest.raises(UpstreamError) as e:
            _rows(THROTTLED, "token_transfers", source="TronScan")
        assert "rate exceeded" in str(e.value)

    def test_the_source_is_named(self):
        """
        So the log line says which API gave up, not merely that one did.
        """
        with pytest.raises(UpstreamError, match="TronScan"):
            _rows(THROTTLED, "token_transfers", source="TronScan")

    def test_lowercase_error_too(self):
        with pytest.raises(UpstreamError):
            _rows({"error": "whatever"}, "data", source="TronGrid")

    def test_a_missing_key_is_an_error_not_an_empty_result(self):
        """
        The heart of it. A body with neither the key nor an error field is
        still not a transfer list, and used to read as a quiet wallet.
        """
        with pytest.raises(UpstreamError, match="token_transfers"):
            _rows({"total": 0}, "token_transfers", source="TronScan")

    def test_the_keys_are_reported(self):
        """
        A body of an unexpected shape is a five-minute diagnosis with its
        keys in the message and an hour without them.
        """
        with pytest.raises(UpstreamError, match="total"):
            _rows({"total": 0}, "token_transfers", source="TronScan")

    def test_a_non_object_raises(self):
        for body in ([], "suspended", None, 7):
            with pytest.raises(UpstreamError):
                _rows(body, "data", source="TronGrid")

    def test_the_key_present_but_not_a_list_raises(self):
        with pytest.raises(UpstreamError):
            _rows({"data": {"oops": 1}}, "data", source="TronGrid")


class TestAQuietWalletIsStillQuiet:
    def test_an_empty_list_passes_through(self):
        """
        The case that must NOT raise. Most polls find nothing, and a
        monitor that treated that as a failure would back off permanently
        and shout at the Bridge about healthy wallets.
        """
        assert _rows({"token_transfers": []}, "token_transfers",
                     source="TronScan") == []

    def test_null_is_treated_as_empty(self):
        """
        Seen from TronGrid: the key present with a null value. It carries
        the key, so it is an answer, and the answer is "none".
        """
        assert _rows({"data": None}, "data", source="TronGrid") == []

    def test_real_rows_come_back_untouched(self):
        rows = [{"to_address": "TSs6Bjj", "quant": "37843000000"}]
        assert _rows({"token_transfers": rows}, "token_transfers",
                     source="TronScan") is rows

    def test_an_error_field_that_is_empty_is_not_an_error(self):
        """
        Some responses carry error: "" or error: null alongside real data.
        Raising on the presence of the key rather than its truth would
        reject good responses.
        """
        assert _rows({"error": "", "data": []}, "data",
                     source="TronGrid") == []
        assert _rows({"error": None, "data": []}, "data",
                     source="TronGrid") == []


class TestEveryFetcherUsesIt:
    """
    Four fetchers, all with the same shape. One left on the old form is the
    whole bug, still live, on whichever API that one is.
    """

    SRC = (ROOT / "monitor" / "tron.py").read_text(encoding="utf-8")

    def test_no_fetcher_defaults_a_missing_key_to_empty(self):
        """
        Walked as an AST, not grepped.

        The first version searched the file text for payload.get("...", [])
        and failed — because the docstring explaining the bug quotes it, and
        so does this one. A correct fix marked as a regression.

        Sixth time prose has satisfied a source scan in this project. The
        parser cannot read a docstring as a call, which is the only reliable
        way to assert about code in a file that is mostly explanation.
        See tests/sqltext.py.
        """
        import ast

        tree = ast.parse(self.SRC)

        # _rows is the one place allowed to read a payload directly — that
        # is what it is for. Everything else must go through it.
        guard = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_rows")
        inside_guard = range(guard.lineno, (guard.end_lineno or guard.lineno) + 1)

        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "payload"
            and node.lineno not in inside_guard
        ]
        assert not offenders, (
            "a fetcher still reads the payload directly, so a missing key "
            f"is an empty result again — lines {offenders}"
        )

    def test_all_four_go_through_the_guard(self):
        assert self.SRC.count("_rows(payload,") == 4

    def test_the_fallback_can_now_see_a_throttle(self):
        """
        Why raising matters rather than merely logging: incoming_transfers
        catches an exception from the primary and tries TronGrid. An empty
        list is not an exception, so until now the one failure that
        actually happens in production was the one the fallback never saw.
        """
        import inspect

        from monitor.tron import TronClient
        src = inspect.getsource(TronClient.incoming_transfers)
        assert "except Exception" in src
        assert "fallback(address, since_ms)" in src
