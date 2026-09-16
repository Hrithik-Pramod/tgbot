"""
A pairing records — and shows — which client address it settles to.

THE REQUEST (Bridge, 16 September 2026, 03:12)

He pasted what /wallet gave him: four supplier pairings, then two
counterparty wallets, with nothing joining the two lists.

    we need to be able to associate the wallets to the client pairings,
    as not able to see this at moment

Client A is paid at two addresses. Nothing in the system said which pairing
used which, so he was holding it in his head while acting on a deposit
notification — at the exact moment a mistake sends real money to the wrong
place.

AND THE OTHER HALF, ten minutes later:

    And BRIDGE CONTROL is now not letting me set new wallets

True, and always had been. /wallet listed them, /walletchange changed an
address, and there was no way to add one at all — every wallet in the
system had been inserted by hand. Not a regression; a gap nobody had walked
into until three in the morning.

WHAT THIS IS NOT

Routing. The bot does not move money and this does not make it. It records
the intended destination so the notification he is already reading can
print it, instead of sending him to /wallet mid-trade.
"""

import inspect
import re
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import bridge_bot  # noqa: E402
from bot.notifier import Notifier  # noqa: E402
from core.summary import render_deposit_notification  # noqa: E402
from db.repo import Repo  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
MIGRATION = (ROOT / "deploy" / "migrate-008-payout-wallet.sql").read_text(encoding="utf-8")

PAYOUT = "TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq"


def _code(fn) -> str:
    src = inspect.getsource(fn)
    if fn.__doc__:
        src = src.replace(fn.__doc__, "")
    src = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    return re.sub(r"\s+", " ", src)


def _notice(**kw):
    base = dict(
        reference="SUPD1", supplier_label="Girish - Sam", client_label="Client A",
        usdt_in=D("1000"), inr_out=D("106000"), tx_hash="abc",
        supply_rate=D("106"), sell_rate=D("107.5"), usdt_out=D("985.58"),
    )
    base.update(kw)
    return render_deposit_notification(**base)


class TestTheDestinationIsOnTheNotification:
    def test_it_prints_where_to_send(self):
        out = _notice(payout_address=PAYOUT)
        assert f"Send to {PAYOUT}" in out

    def test_the_address_is_never_abbreviated(self):
        """
        Same rule as the internal wallet. Two TRON addresses share their
        first and last characters far too readily to trust a shortened one
        with money.
        """
        out = _notice(payout_address=PAYOUT)
        assert PAYOUT in out
        assert "…" not in out and "..." not in out

    def test_it_sits_with_the_amount_being_sent(self):
        """Destination next to figure — both are needed for the one action."""
        out = _notice(payout_address=PAYOUT)
        lines = out.splitlines()
        send_on = next(i for i, l in enumerate(lines) if "Send on to" in l)
        send_to = next(i for i, l in enumerate(lines) if "Send to " in l)
        assert send_to == send_on + 1

    def test_an_unmapped_pairing_says_nothing_rather_than_guessing(self):
        out = _notice()
        assert "Send to " not in out

    def test_the_rest_of_the_notification_is_unchanged(self):
        out = _notice(payout_address=PAYOUT)
        assert "GIRISH - SAM  ·  Transaction SUPD1" in out
        assert "Bought at 106" in out
        assert "Send on to Client A" in out


class TestBothNotificationPathsCarryIt:
    """
    A deposit is announced from two places — immediately when the account is
    already known, and later when a held announcement is released. Wiring one
    and not the other is how the confirm button went missing in August.
    """

    def test_the_immediate_path_passes_it(self):
        src = _code(Notifier.on_supplier_deposit)
        assert "payout_address=payout_address" in src

    def test_the_held_release_passes_it(self):
        src = _code(Notifier.announce_trade)
        assert 'payout_address=trade["payout_address"]' in src

    def test_the_claim_query_supplies_it(self):
        sql = _code(Repo.claim_trade_announcement)
        assert "pw.address AS payout_address" in sql
        assert "LEFT JOIN wallets pw ON pw.id = w.payout_wallet_id" in sql


class TestWalletShowsTheAssociation:
    def test_each_pairing_names_both_ends(self):
        src = _code(bridge_bot.cmd_wallet)
        assert "Deposits in" in src
        assert "Pay out to" in src

    def test_an_unset_pairing_says_how_to_set_it(self):
        src = _code(bridge_bot.cmd_wallet)
        assert "/walletlink" in src


class TestAWalletCanBeAddedFromTheBot:
    def test_the_command_exists(self):
        assert hasattr(bridge_bot, "cmd_walletadd")
        assert 'Command("walletadd")' in inspect.getsource(bridge_bot)

    def test_it_validates_the_address(self):
        assert bridge_bot._is_tron_address("T" + "x" * 33)
        assert not bridge_bot._is_tron_address("x" * 34)
        assert not bridge_bot._is_tron_address("T" + "x" * 10)

    def test_a_duplicate_address_is_refused(self):
        src = _code(Repo.add_wallet)
        assert "already registered" in src

    def test_a_second_wallet_for_one_pairing_is_refused(self):
        """
        One internal wallet per pairing — the wallet is the routing key. The
        message points at /walletchange rather than just saying no.
        """
        src = _code(Repo.add_wallet)
        assert "/walletchange" in src

    def test_it_does_not_adopt_the_wallet_itself(self):
        """
        Adoption belongs to the monitor's first poll. Seeding a baseline by
        hand is how 38 historic transfers were ingested as live deposits.
        """
        src = _code(Repo.add_wallet)
        assert "adopted_at_ms" not in src
        assert "monitor_state" not in src


class TestLinkingRefusesWhatWouldNotMakeSense:
    def test_the_target_must_belong_to_this_pairings_client(self):
        src = _code(Repo.set_payout_wallet)
        assert 'dst["owner_party_id"] != src["client_id"]' in src

    def test_an_internal_wallet_cannot_be_a_destination(self):
        src = _code(Repo.set_payout_wallet)
        assert 'dst["is_internal"]' in src

    def test_only_a_pairing_can_have_a_payout_address(self):
        src = _code(Repo.set_payout_wallet)
        assert 'not src["is_internal"]' in src

    def test_the_picker_only_offers_that_clients_addresses(self):
        """Belt and braces — the refusal above is the check, this is so a
        mis-tap is not possible in the first place."""
        src = _code(bridge_bot.walletlink_pairing)
        assert 'wallets_owned_by(wallet["client_id"])' in src

    def test_it_is_audited(self):
        src = _code(Repo.set_payout_wallet)
        assert "wallet.payout_set" in src


class TestTheMigration:
    def test_it_adds_the_column(self):
        assert "ADD COLUMN IF NOT EXISTS payout_wallet_id" in MIGRATION

    def test_the_schema_matches(self):
        assert "payout_wallet_id BIGINT     REFERENCES wallets(id)" in SCHEMA

    def test_girish_is_pointed_at_the_address_the_bridge_named(self):
        assert "TV7EdxczfZ3Gz6LFuDSEUaicib3xqgyT2J" in MIGRATION
        assert "TTNbTqxUpr5uXbRRNQcQteYonfv9N77kzq" in MIGRATION

    def test_the_others_keep_settling_where_they_always_have(self):
        assert "TXtfrak7La6RTVDmEFvtR4td7N2tp6tYvG" in MIGRATION

    def test_it_is_rerunnable(self):
        assert MIGRATION.count("IF NOT EXISTS") >= 2
