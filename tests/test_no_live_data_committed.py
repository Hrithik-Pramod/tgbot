"""
No live counterparty data in a public repository.

THE STANDING RULE

This repository is public. The decision was "keep it public, sanitise
everything first" — no real wallet addresses, bank details, UTRs, rates or
counterparty names in committed files. docs/, db/seed.sql and .env are
gitignored for the same reason.

WHAT ACTUALLY HAPPENED

The rule was kept for a fortnight and then broken three times in four days,
every time by me, every time while fixing something else:

  22 Sep  deploy/fix-alph1-rate.sql committed with live rates and amounts —
          I had said in as many words that it should stay out of the repo,
          then left it in deploy/ where `git add -A` swept it up
  23 Sep  three real UTRs in test_mini_statement.py
  24 Sep  two real bank account numbers and their holders' names in
          test_vendor_sees_own_accounts.py

Each was a test made concrete with data from the incident it described,
which is exactly the habit that makes these tests worth reading — and
exactly how real banking details end up on GitHub.

Judgement failed three times in a row, so it stops being a matter of
judgement.

WHAT THIS CATCHES, AND WHAT IT CANNOT

Crisp identifiers only: TRON addresses, Indian UTR references, long digit
runs that look like account numbers. A rate is just a number and no test can
tell 106.4 from any other; that one still needs a human reading the diff.

Known-public constants are listed by value — the USDT contract address is
not a secret and appears in config and in half the tests.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The TRC20 USDT contract. Public, immutable, and necessarily in the source.
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

TRON = re.compile(r"\bT[1-9A-HJ-NP-Za-km-z]{33}\b")
UTR = re.compile(r"\b(?:BKID|PUNB|MAHB|SIBL|ICIC|UTIB|SBIN)[A-Z]?\d{12,}\b")
LONG_DIGITS = re.compile(r"\b\d{12,18}\b")

SCANNED = {".py", ".sql", ".md", ".yml", ".yaml", ".sh", ".toml"}
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache",
             "docs", ".idea", ".vscode"}
# Gitignored, so never committed. seed.example.sql is the synthetic template
# and its placeholder addresses are meant to look like addresses.
SKIP_FILES = {"seed.sql", "seed.local.sql", "seed.example.sql"}


# Walked by name rather than rglob over the repository root: the root also
# holds .git and .venv, which are large enough that scanning them turns a
# fast test into a slow one for no benefit.
SOURCE_DIRS = ("bot", "core", "db", "deploy", "monitor", "tests")


def _files():
    for name in SOURCE_DIRS:
        d = ROOT / name
        if not d.is_dir():
            continue
        for p in d.rglob("*"):
            if not p.is_file() or p.suffix not in SCANNED:
                continue
            if SKIP_DIRS & set(p.relative_to(ROOT).parts):
                continue
            if p.name in SKIP_FILES:
                continue
            yield p
    for p in ROOT.glob("*"):
        if p.is_file() and p.suffix in SCANNED and p.name not in SKIP_FILES:
            yield p


# --------------------------------------------------------------------------
# The baseline.
#
# These files already carried live-looking identifiers before this guard
# existed — production seed data, migrations written against the real
# addresses, and tests made concrete with real incident data. Cleaning them
# is a job of its own: golive-seed.sql and the migrations have run against
# production, and rewriting a test's fixtures risks changing what it proves.
#
# So the rule here is narrow and absolute: THIS LIST MAY SHRINK, NEVER GROW.
# A new file with real data fails the suite; an old one is a debt recorded
# where it cannot be forgotten.
#
# Worst first, for whoever picks the cleanup up:
#   deploy/golive-seed.sql          real addresses AND real account numbers
#   deploy/fix-supb-attribution.sql a real UTR and two account numbers
#   core/parse.py                   real UTRs and a client account number
#   deploy/migrate-00{5,7,8}        real wallet addresses
# --------------------------------------------------------------------------
BASELINE = {
    "core/parse.py",
    "deploy/RUNBOOK.md",
    "deploy/check_monitor.py",
    "deploy/fix-supb-attribution.sql",
    "deploy/golive-seed.sql",
    "deploy/migrate-005-multi-wallet.sql",
    "deploy/migrate-007-client-wallet.sql",
    "deploy/migrate-008-payout-wallet.sql",
    "deploy/seed-supplier.sql",
    "README.md",
    "tests/test_account_matching.py",
    "tests/test_beneficiary_carry.py",
    "tests/test_client_requests_10sep.py",
    "tests/test_client_requests_10sep_late.py",
    "tests/test_client_sees_no_supplier_structure.py",
    "tests/test_command_escape.py",
    "tests/test_duplicate_account_names.py",
    "tests/test_duplicate_wording.py",
    "tests/test_handler_order.py",
    "tests/test_integration.py",
    "tests/test_live_fixes_10sep.py",
    "tests/test_money.py",
    "tests/test_parse.py",
    "tests/test_payment_with_no_trade.py",
    "tests/test_payout_mapping.py",
    "tests/test_poll_wallet.py",
    "tests/test_slots.py",
    "tests/test_tronscan_contract.py",
    "tests/test_trx_contract.py",
    "tests/test_unregistered_group_nudge.py",
    # This file quotes placeholder shapes in its own failure messages.
    "tests/test_no_live_data_committed.py",
}


def _scan(find) -> dict[str, list[str]]:
    """Every file OUTSIDE the baseline that matches."""
    hits: dict[str, list[str]] = {}
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in BASELINE:
            continue
        found = find(p.read_text())
        if found:
            hits[rel] = found
    return hits


def _report(hits: dict[str, list[str]]) -> str:
    return "\n".join(
        f"  {f}\n      " + "\n      ".join(sorted(set(v))[:5])
        for f, v in sorted(hits.items())
    )


class TestNoRealIdentifiersAreCommitted:
    def test_no_tron_addresses(self):
        """
        A real wallet address in a public repo hands anyone the full
        transaction history of that counterparty. Test fixtures should use a
        string that is obviously not an address.
        """
        hits = _scan(lambda t: [a for a in TRON.findall(t)
                                if a != USDT_CONTRACT])
        assert not hits, (
            "real-looking TRON addresses in a public repository:\n"
            + _report(hits)
            + "\n\nUse something like TExampleWalletAddress000000000000."
        )

    def test_no_bank_references(self):
        """
        A UTR identifies one real transfer between two real accounts.
        """
        # Same placeholder rule as below: a reference whose digits use
        # three or fewer distinct characters is plainly invented.
        hits = _scan(lambda t: [u for u in UTR.findall(t)
                                if len(set(re.sub(r'\D', '', u))) > 3])
        assert not hits, (
            "real-looking bank references in a public repository:\n"
            + _report(hits)
            + "\n\nUse a shaped placeholder: BKIDR10000000000000001."
        )

    def test_no_long_digit_runs_that_could_be_account_numbers(self):
        """
        Indian account numbers run 9 to 18 digits. Twelve is the floor here
        because shorter runs are too often a timestamp or an id to be worth
        the noise — this is a net, not a proof.
        """
        # A run with very few distinct digits is plainly a placeholder,
        # which is exactly what we want people to write instead.
        hits = _scan(lambda t: [d for d in LONG_DIGITS.findall(t)
                                if len(set(d)) > 3])
        assert not hits, (
            "digit runs that look like real account numbers:\n"
            + _report(hits)
            + "\n\nUse an obvious placeholder: 50200100000000."
        )


class TestTheIgnoreRulesStillHold:
    def test_the_confidential_paths_are_ignored(self):
        """
        The other half of the rule. These carry the material that must never
        be committed at all rather than merely sanitised.
        """
        ignored = (ROOT / ".gitignore").read_text()
        for path in (".env*", "docs/", "db/seed.sql"):
            assert path in ignored, f"{path} is no longer gitignored"
