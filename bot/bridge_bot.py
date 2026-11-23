"""
Bridge bot: /setrate, /wallet, /walletchange, /send, /summary.

These handlers only run for commands arriving in the registered Bridge chat
(see bot/auth.py). Set STRICT_BRIDGE_USER=true to additionally require that the
sender is BRIDGE_USER_ID.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from decimal import Decimal

from core.money import (
    MoneyError, fmt_inr, fmt_rate, fmt_usdt, fmt_usdt_plain, pct_collected,
    to_decimal,
)
from core.summary import render_mini_statement

log = logging.getLogger(__name__)
router = Router()


class SetRate(StatesGroup):
    supplier = State()
    client = State()
    supply_rate = State()
    # Only reached from /specialrate, for a vendor who has an introducer.
    # The same flow serves both commands rather than a second copy of it:
    # two rate flows would drift, and the one that drifted would be the one
    # used less often — which is the one handling commission.
    buy_rate = State()
    sell_rate = State()
    confirm = State()


class WalletChange(StatesGroup):
    which = State()
    address = State()
    confirm = State()


class WalletAdd(StatesGroup):
    kind = State()
    supplier = State()
    client = State()
    owner = State()
    address = State()


class WalletLink(StatesGroup):
    pairing = State()
    payout = State()


class AddIntroducer(StatesGroup):
    label = State()
    chat = State()
    vendor = State()


class AddVendor(StatesGroup):
    label = State()
    prefix = State()
    client = State()
    chat = State()
    address = State()
    confirm = State()


class BridgeSend(StatesGroup):
    supplier = State()
    client = State()
    hash_url = State()
    account = State()


def _party_keyboard(parties, prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=p["label"], callback_data=f"{prefix}:{p['id']}")]
        for p in parties
    ])


# ----------------------------------------------------------------- /setrate

@router.message(Command("setrate"))
async def cmd_setrate(message: Message, state: FSMContext, repo) -> None:
    # Only vendors this command can actually act on. One with an introducer
    # needs three rates and belongs to /specialrate.
    suppliers = await repo.list_suppliers_by_introducer(with_introducer=False)
    if not suppliers:
        await message.answer(
            "Every registered vendor has an introducer, so their rates are "
            "set with /specialrate."
        )
        return
    await state.set_state(SetRate.supplier)
    await state.update_data(special=False)
    await message.answer("Which supplier?", reply_markup=_party_keyboard(suppliers, "sr_sup"))


@router.message(Command("specialrate"))
async def cmd_specialrate(message: Message, state: FSMContext, repo) -> None:
    """
    Three rates instead of two, for a vendor who has an introducer.

        I have a command call /specialrate / For this one it asked for 3
        things / Supply / Buy / Sell rate
        — Bridge, 23 November 2026

    Supply is what the vendor is paid at, buy is supply plus the
    introducer's fee, sell is what the client buys at. The gap between
    supply and buy is the commission, and it comes out of the desk's spread
    rather than off the vendor or onto the client — see
    core.money.split_three_ways.
    """
    # Only vendors who actually have an introducer.
    #
    #     1 note, when i choose special rate, all accounts show, not just
    #     the one that has introducer on it — Bridge, 23 November 2026
    suppliers = await repo.list_suppliers_by_introducer(with_introducer=True)
    if not suppliers:
        await message.answer(
            "No vendor has an introducer yet. Attach one with "
            "/addintroducer first, or use /setrate."
        )
        return
    await state.set_state(SetRate.supplier)
    await state.update_data(special=True)
    await message.answer(
        "Which supplier? (this sets supply, buy and sell)",
        reply_markup=_party_keyboard(suppliers, "sr_sup"),
    )


@router.callback_query(SetRate.supplier, F.data.startswith("sr_sup:"))
async def setrate_supplier(call: CallbackQuery, state: FSMContext, repo) -> None:
    supplier_id = int(call.data.split(":", 1)[1])
    await state.update_data(supplier_id=supplier_id)

    # THE TWO COMMANDS REFUSE EACH OTHER'S VENDORS.
    #
    # Without this, setting a rate for a vendor who has an introducer with
    # plain /setrate would write buy = supply and the commission would stop
    # accruing — silently, because every figure the desk sees would still
    # look right. It was his own worry:
    #
    #     Yes I think that is a good idea, but need to think how to set a
    #     supplier/introducer on UI Control — Bridge, 23 November 2026
    #
    # And the other way round is just as wrong: /specialrate on a vendor
    # with nobody to pay would quietly carve a slice of the spread for an
    # introducer who does not exist.
    data = await state.get_data()
    introducer = await repo.introducer_for_supplier(supplier_id)

    if introducer is not None and not data.get("special"):
        await state.clear()
        await call.message.edit_text(
            f"That vendor has an introducer ({introducer['label']}), so its "
            "rates include a buy rate. Use /specialrate instead — /setrate "
            "would drop the buy rate and the commission would stop accruing "
            "without anything looking wrong."
        )
        await call.answer()
        return

    if introducer is None and data.get("special"):
        await state.clear()
        await call.message.edit_text(
            "That vendor has no introducer, so there is no buy rate to set. "
            "Use /setrate, or /addintroducer first if one should be "
            "attached."
        )
        await call.answer()
        return

    clients = await repo.list_parties("client")
    if not clients:
        await call.message.edit_text("No clients are registered yet.")
        await state.clear()
        await call.answer()
        return
    await state.set_state(SetRate.client)
    await call.message.edit_text("Which client?", reply_markup=_party_keyboard(clients, "sr_cli"))
    await call.answer()


@router.callback_query(SetRate.client, F.data.startswith("sr_cli:"))
async def setrate_client(call: CallbackQuery, state: FSMContext, repo) -> None:
    data = await state.get_data()
    client_id = int(call.data.split(":", 1)[1])
    await state.update_data(client_id=client_id)

    # Show the rate currently in force so the Bridge can confirm rather than
    # retype it, which is what the brief asks for.
    current = await repo.current_rate(data["supplier_id"], client_id)
    if current:
        age = float(current["age_hours"])
        stale = "  (STALE)" if age > 24 else ""
        buy_line = ""
        # Shown only when it is actually doing something. For every other
        # pairing buy equals supply and printing it would be a third
        # identical number to read past.
        if current["buy_rate"] != current["supply_rate"]:
            buy_line = f"Current buy rate:    {fmt_rate(current['buy_rate'])}\n"
        note = (
            f"Current supply rate: {fmt_rate(current['supply_rate'])}\n"
            f"{buy_line}"
            f"Current sell rate:   {fmt_rate(current['sell_rate'])}\n"
            f"Set {age:.1f} hours ago{stale}\n\n"
        )
    else:
        note = "No rate is set for this pairing yet.\n\n"

    await state.set_state(SetRate.supply_rate)
    await call.message.edit_text(f"{note}Enter the supply rate (INR per 1 USDT):")
    await call.answer()


@router.message(SetRate.supply_rate)
async def setrate_supply(message: Message, state: FSMContext) -> None:
    try:
        rate = to_decimal(message.text or "")
    except MoneyError:
        await message.answer("I could not read that as a rate. Send just the number.")
        return
    if rate <= 0:
        await message.answer("Rate must be greater than zero.")
        return
    await state.update_data(supply_rate=str(rate))

    data = await state.get_data()
    if data.get("special"):
        await state.set_state(SetRate.buy_rate)
        # "(INR per 1 USDT)" removed at his request, 23 November 2026. He
        # sets rates every day and does not need the unit spelled out.
        await message.answer(
            "Enter the buy rate — supply plus the introducer's fee:"
        )
        return

    await state.set_state(SetRate.sell_rate)
    await message.answer("Enter the sell rate (INR per 1 USDT):")


@router.message(SetRate.buy_rate)
async def setrate_buy(message: Message, state: FSMContext) -> None:
    try:
        rate = to_decimal(message.text or "")
    except MoneyError:
        await message.answer("I could not read that as a rate. Send just the number.")
        return

    data = await state.get_data()
    supply = to_decimal(data["supply_rate"])

    # Below supply would pay the introducer out of the vendor's money
    # instead of out of the spread, which is not the arrangement. Refused
    # here, where he can retype it, rather than at the database.
    if rate < supply:
        await message.answer(
            f"The buy rate cannot be below the supply rate "
            f"({fmt_rate(supply)}). The introducer's fee comes out of your "
            "spread, not out of the vendor's money. Send it again."
        )
        return

    await state.update_data(buy_rate=str(rate))
    await state.set_state(SetRate.sell_rate)
    await message.answer("Enter the sell rate (INR per 1 USDT):")


@router.message(SetRate.sell_rate)
async def setrate_sell(message: Message, state: FSMContext) -> None:
    try:
        rate = to_decimal(message.text or "")
    except MoneyError:
        await message.answer("I could not read that as a rate. Send just the number.")
        return
    if rate <= 0:
        await message.answer("Rate must be greater than zero.")
        return

    data = await state.get_data()
    supply = to_decimal(data["supply_rate"])
    buy = to_decimal(data["buy_rate"]) if data.get("buy_rate") else None

    # Above sell, the introducer would take more than the whole spread and
    # the desk would owe more than it earned.
    if buy is not None and rate < buy:
        await message.answer(
            f"The sell rate cannot be below the buy rate ({fmt_rate(buy)}). "
            "The introducer would be taking more than the whole spread. "
            "Send it again."
        )
        return

    await state.update_data(sell_rate=str(rate))
    await state.set_state(SetRate.confirm)

    # A sell rate at or below the supply rate means the trade loses money.
    # Worth stopping on, since it is almost always a typo.
    warning = ""
    if rate <= supply:
        warning = (
            "\n\nWARNING: the sell rate is not above the supply rate. "
            "This trade would run at a loss. Check before confirming."
        )
    elif buy is not None and rate == buy:
        warning = (
            "\n\nWARNING: the sell rate equals the buy rate, so the "
            "introducer takes the entire spread and you keep nothing. "
            "Check before confirming."
        )

    lines = [f"Supply rate: {fmt_rate(supply)}"]
    if buy is not None:
        lines.append(f"Buy rate:    {fmt_rate(buy)}")
    lines.append(f"Sell rate:   {fmt_rate(rate)}")

    # The "Introducer takes ₹X per 1,000 USDT" line was here and he asked
    # for it to go (23 November 2026). It was meant as a sense-check against
    # what he had agreed; he reads the three rates directly and did not want
    # a fourth figure to check them against.

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="sr_yes"),
        InlineKeyboardButton(text="Cancel", callback_data="sr_no"),
    ]])
    await message.answer(
        "\n".join(lines) + f"\n\nIs this correct?{warning}",
        reply_markup=kb,
    )


@router.callback_query(SetRate.confirm, F.data == "sr_yes")
async def setrate_confirm(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    data = await state.get_data()
    await repo.set_rate(
        supplier_id=data["supplier_id"],
        client_id=data["client_id"],
        supply_rate=to_decimal(data["supply_rate"]),
        sell_rate=to_decimal(data["sell_rate"]),
        # None for an ordinary pairing, which set_rate stores as the supply
        # rate — "no introducer" written as a figure rather than an absence.
        buy_rate=(to_decimal(data["buy_rate"])
                  if data.get("buy_rate") else None),
        set_by=party["id"],
    )
    await state.clear()
    await call.message.edit_text(
        "Rate is set in the system."
        + (" The introducer's share applies from the next deposit; trades "
           "already open keep the rates they were struck at."
           if data.get("buy_rate") else "")
    )
    await call.answer()


@router.callback_query(F.data == "sr_no")
async def setrate_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Cancelled. No rate was changed.")
    await call.answer()


# --------------------------------------------------------------- /progress

@router.message(Command("progress"))
async def cmd_progress(message: Message, repo) -> None:
    """
    The whole book on one screen.

    Client request, 19 September 2026: "can you add /progress to UI control,
    so it shows a summary of all trading progress for all groups?"

    /summary answers a narrower question — pick a supplier, see their open
    trades — which is two taps and tells him nothing about the groups he did
    not pick. This is the glance: every pairing, busy or not, no taps.

    The suppliers' own /progress shows them their collection and nothing
    else (decision D4). This is the Bridge's, and the Bridge sees all, so it
    carries the reference, the rate position and the money nobody has been
    billed for.
    """
    rows = await repo.book_progress()
    if not rows:
        await message.answer("No pairings are set up yet.")
        return

    lines, total_out, total_stranded = [], Decimal(0), Decimal(0)

    for r in rows:
        lines.append(f"{r['supplier_label']} → {r['client_label']}")

        if r["open_trades"]:
            outstanding = r["expected_inr"] - r["collected_inr"]
            total_out += outstanding
            deals = "1 trade" if r["open_trades"] == 1 else f"{r['open_trades']} trades"
            lines.append(
                f"  {deals}   ₹{fmt_inr(r['collected_inr'])}"
                f" of ₹{fmt_inr(r['expected_inr'])}"
                f"   {pct_collected(r['collected_inr'], r['expected_inr'])}"
            )
            if outstanding > 0:
                lines.append(f"  Outstanding  ₹{fmt_inr(outstanding)}")
            elif outstanding < 0:
                lines.append(f"  OVERPAID     ₹{fmt_inr(-outstanding)}")
            else:
                lines.append("  Paid in full — close it with /done or /issue")
            if r["uninstructed"]:
                # A trade the client has not been told about collects nothing
                # and looks identical to one that is merely slow.
                lines.append(
                    f"  {r['uninstructed']} not yet issued — use /issue"
                )
        else:
            lines.append("  Nothing open")

        # The line that would have caught ₹995,495, twice.
        if r["stranded_usdt"]:
            total_stranded += r["stranded_inr"]
            lines.append(
                f"  ⚠ {fmt_usdt_plain(r['stranded_usdt'])} USDT on a cancelled "
                f"trade, never invoiced (₹{fmt_inr(r['stranded_inr'])})"
            )

        lines.append("")

    # The rupee total stays. The PERCENTAGE does not.
    #
    # Client request, 22 September 2026: "I'd like percentage is per vendor
    # not total." Added the day before on my own reading of "add % to my look
    # up", which was wrong — a book-wide figure averages vendors who have
    # collected nothing against vendors who are finished and describes
    # neither. 61% across the book tells him to chase nobody in particular.
    #
    # A sum of rupees is not the same mistake: money owed genuinely adds up,
    # and it is the number he acts on.
    lines.append(f"Outstanding across the book  ₹{fmt_inr(total_out)}")
    if total_stranded:
        lines.append(f"Never invoiced               ₹{fmt_inr(total_stranded)}")

    # The next step he asked for on 23 September: "we have a next step,
    # choose trade ... then after choosing trade it gives a summary of utrs
    # so far and a total with percentage."
    #
    # NOT state-gated, deliberately. cancel_pick was, nothing set the state,
    # and the buttons were silently dead for days (22 September). A mini
    # statement is a read — a button pressed on yesterday's message should
    # just render today's figures, not match no handler.
    live = await repo.list_open_trades()
    kb = None
    if live:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{t['reference']} — {t['supplier_label']}",
                callback_data=f"mst:{t['id']}",
            )]
            for t in live
        ])
        lines += ["", "Pick a trade for its payments so far:"]

    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("mst:"))
async def mini_statement(call: CallbackQuery, repo) -> None:
    """
    Every UTR against one trade, for the Bridge.

    He gets the reference and the vendor on it; the supplier's copy of the
    same statement does not (decision D4).
    """
    trade_id = int(call.data.split(":", 1)[1])
    trade = await repo.trade_detail(trade_id)
    if trade is None:
        await call.message.answer("That trade no longer exists.")
        await call.answer()
        return

    payments = await repo.trade_payments(trade_id)
    await call.message.answer(render_mini_statement(
        payments=payments,
        expected_inr=trade["inr_expected"],
        paid_inr=trade["paid_inr"],
        reference=trade["reference"],
        vendor=trade["supplier_label"],
    ))
    await call.answer()


# --------------------------------------------------------------- /viewrate

@router.message(Command("viewrate"))
async def cmd_viewrate(message: Message, repo) -> None:
    """
    Every rate currently in force, without starting to change one.

    Client request, 10 September 2026. /setrate does show the current rate, but
    only after you have chosen a supplier and a client and are already two taps
    into changing it — which is no use when the question is simply "what are we
    on?". Reading and writing should not be the same command.
    """
    rates = await repo.current_rates()
    if not rates:
        await message.answer("No rates are set yet. Use /setrate.")
        return

    lines = ["Rates in force", ""]
    for r in rates:
        age = float(r["age_hours"])
        if age < 1:
            when = f"{age * 60:.0f} minutes ago"
        elif age < 48:
            when = f"{age:.0f} hours ago"
        else:
            when = f"{age / 24:.0f} days ago"

        # The staleness warning is the same threshold the deposit notice uses,
        # so the two never disagree about what counts as old.
        stale = "   ← CHECK THIS" if age > 24 else ""

        lines.append(f"{r['supplier_label']} → {r['client_label']}{stale}")
        lines.append(f"Buy  {fmt_rate(r['supply_rate'])}")
        lines.append(f"Sell {fmt_rate(r['sell_rate'])}")
        lines.append(f"Set {when}")
        lines.append("")

    await message.answer("\n".join(lines).rstrip())


# ------------------------------------------------------------------ /wallet

@router.message(Command("wallet"))
async def cmd_wallet(message: Message, repo) -> None:
    """
    Shows the internal-to-client linkage. The brief calls this "very important
    for routing and calculations" - the internal wallet IS the routing key
    (answer C3), so this view is how the Bridge verifies the wiring.
    """
    wallets = await repo.list_wallets()
    if not wallets:
        await message.answer("No wallets are configured.")
        return

    internal = [w for w in wallets if w["is_internal"]]
    external = [w for w in wallets if not w["is_internal"]]

    lines: list[str] = []
    if internal:
        lines.append("Supplier pairings")
        lines.append("")
        for w in internal:
            flag = "" if w["is_monitored"] else "   [not monitored]"
            lines.append(f"{w['supplier_label']} → {w['client_label']}{flag}")
            lines.append(f"  Deposits in   {w['address']}")

            # The destination, on the same block as the source.
            #
            # These used to be two separate lists — pairings above,
            # counterparty wallets below — with nothing joining them, so a
            # client with two addresses left the Bridge working out which one
            # a given pairing settles to (16 September 2026: "we need to be
            # able to associate the wallets to the client pairings, as not
            # able to see this at moment").
            if w["payout_address"]:
                lines.append(f"  Pay out to    {w['payout_address']}")
            else:
                lines.append("  Pay out to    not set — use /walletlink")
            lines.append("")

    if external:
        lines.append("Client and supplier addresses")
        lines.append("")
        for w in external:
            flag = "" if w["is_monitored"] else "   [not monitored]"
            lines.append(f"{w['owner_label']} = {w['address']}{flag}")

    await message.answer("\n".join(lines).rstrip())


# --------------------------------------------------------------- /walletadd
#
# There was no way to register a wallet from the Bridge bot at all. /wallet
# listed them and /walletchange changed an address, but every wallet in the
# system had been inserted by hand. The Bridge hit that edge at 03:13 on
# 16 September 2026 — "BRIDGE CONTROL is now not letting me set new wallets"
# — which was true, and had always been true.


def _is_tron_address(value: str) -> bool:
    """TRON base58: starts with T, 34 characters (B1, TRC20 only)."""
    return value.startswith("T") and len(value) == 34


@router.message(Command("walletadd"))
async def cmd_walletadd(message: Message, state: FSMContext) -> None:
    await state.set_state(WalletAdd.kind)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Supplier pairing (deposits land here)",
                              callback_data="wa_kind:internal")],
        [InlineKeyboardButton(text="A party's own address (you pay out to it)",
                              callback_data="wa_kind:external")],
    ])
    await message.answer("What kind of wallet?", reply_markup=kb)


@router.callback_query(WalletAdd.kind, F.data.startswith("wa_kind:"))
async def walletadd_kind(call: CallbackQuery, state: FSMContext, repo) -> None:
    kind = call.data.split(":", 1)[1]
    await state.update_data(kind=kind)

    if kind == "internal":
        suppliers = await repo.list_parties("supplier")
        if not suppliers:
            await call.message.edit_text("No suppliers are registered yet.")
            await state.clear()
            await call.answer()
            return
        await state.set_state(WalletAdd.supplier)
        await call.message.edit_text(
            "Which supplier sends to it?",
            reply_markup=_party_keyboard(suppliers, "wa_sup"),
        )
    else:
        parties = await repo.list_parties("client") + await repo.list_parties("supplier")
        await state.set_state(WalletAdd.owner)
        await call.message.edit_text(
            "Whose address is it?", reply_markup=_party_keyboard(parties, "wa_own")
        )
    await call.answer()


@router.callback_query(WalletAdd.supplier, F.data.startswith("wa_sup:"))
async def walletadd_supplier(call: CallbackQuery, state: FSMContext, repo) -> None:
    await state.update_data(supplier_id=int(call.data.split(":", 1)[1]))
    clients = await repo.list_parties("client")
    await state.set_state(WalletAdd.client)
    await call.message.edit_text(
        "Which client?", reply_markup=_party_keyboard(clients, "wa_cli")
    )
    await call.answer()


@router.callback_query(WalletAdd.client, F.data.startswith("wa_cli:"))
async def walletadd_client(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(client_id=int(call.data.split(":", 1)[1]))
    await state.set_state(WalletAdd.address)
    await call.message.edit_text("Enter the address:")
    await call.answer()


@router.callback_query(WalletAdd.owner, F.data.startswith("wa_own:"))
async def walletadd_owner(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(owner_party_id=int(call.data.split(":", 1)[1]))
    await state.set_state(WalletAdd.address)
    await call.message.edit_text("Enter the address:")
    await call.answer()


@router.message(WalletAdd.address)
async def walletadd_address(message: Message, state: FSMContext, party, repo) -> None:
    address = (message.text or "").strip()
    if not _is_tron_address(address):
        await message.answer(
            "That does not look like a TRON address. TRC20 addresses start "
            "with T and are 34 characters long."
        )
        return

    data = await state.get_data()
    await state.clear()

    ok, msg = await repo.add_wallet(
        address=address,
        is_internal=data["kind"] == "internal",
        supplier_id=data.get("supplier_id"),
        client_id=data.get("client_id"),
        owner_party_id=data.get("owner_party_id"),
        actor_party_id=party["id"],
    )
    await message.answer(msg + ("\n\nUse /walletlink to say where this pairing "
                                "settles to." if ok and data["kind"] == "internal"
                                else ""))


# -------------------------------------------------------------- /walletlink


@router.message(Command("walletlink"))
async def cmd_walletlink(message: Message, state: FSMContext, repo) -> None:
    """Say which of the client's addresses a pairing settles to."""
    wallets = [w for w in await repo.list_wallets() if w["is_internal"]]
    if not wallets:
        await message.answer("No supplier pairings are configured.")
        return

    rows = [
        [InlineKeyboardButton(
            text=f"{w['supplier_label']} → {w['client_label']}",
            callback_data=f"wl:{w['id']}",
        )]
        for w in wallets
    ]
    await state.set_state(WalletLink.pairing)
    await message.answer(
        "Which pairing?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(WalletLink.pairing, F.data.startswith("wl:"))
async def walletlink_pairing(call: CallbackQuery, state: FSMContext, repo) -> None:
    wallet_id = int(call.data.split(":", 1)[1])
    wallet = next(
        (w for w in await repo.list_wallets() if w["id"] == wallet_id), None
    )
    if wallet is None:
        await call.message.edit_text("That pairing no longer exists.")
        await state.clear()
        await call.answer()
        return

    candidates = await repo.wallets_owned_by(wallet["client_id"])
    if not candidates:
        # Only the client's own addresses are offered, so a pairing can never
        # be pointed at another client's wallet by a mis-tap.
        await call.message.edit_text(
            f"{wallet['client_label']} has no addresses registered yet. "
            "Add one with /walletadd first."
        )
        await state.clear()
        await call.answer()
        return

    await state.update_data(wallet_id=wallet_id)
    await state.set_state(WalletLink.payout)
    rows = [
        [InlineKeyboardButton(text=c["address"], callback_data=f"wlp:{c['id']}")]
        for c in candidates
    ]
    await call.message.edit_text(
        f"{wallet['supplier_label']} → {wallet['client_label']}\n\n"
        "Which address do you settle to?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await call.answer()


@router.callback_query(WalletLink.payout, F.data.startswith("wlp:"))
async def walletlink_payout(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    data = await state.get_data()
    await state.clear()
    ok, msg = await repo.set_payout_wallet(
        wallet_id=data["wallet_id"],
        payout_wallet_id=int(call.data.split(":", 1)[1]),
        actor_party_id=party["id"],
    )
    await call.message.edit_text(msg)
    await call.answer()


# ------------------------------------------------------------ /walletchange

@router.message(Command("walletchange"))
async def cmd_walletchange(message: Message, state: FSMContext, repo) -> None:
    wallets = await repo.list_wallets()
    if not wallets:
        await message.answer("No wallets are configured.")
        return

    rows = []
    for w in wallets:
        if w["is_internal"]:
            text = f"Internal: {w['supplier_label']} → {w['client_label']}"
        else:
            text = f"{w['owner_label']}"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"wc:{w['id']}")])

    await state.set_state(WalletChange.which)
    await message.answer("Which wallet?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(WalletChange.which, F.data.startswith("wc:"))
async def walletchange_which(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(wallet_id=int(call.data.split(":", 1)[1]))
    await state.set_state(WalletChange.address)
    await call.message.edit_text("Enter the new address:")
    await call.answer()


@router.message(WalletChange.address)
async def walletchange_address(message: Message, state: FSMContext) -> None:
    address = (message.text or "").strip()

    # TRON base58 addresses start with T and are 34 characters (B1: TRC20 only).
    if not (address.startswith("T") and len(address) == 34):
        await message.answer(
            "That does not look like a TRON address. "
            "TRC20 addresses start with T and are 34 characters long."
        )
        return

    await state.update_data(address=address)
    await state.set_state(WalletChange.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Confirm", callback_data="wc_yes"),
        InlineKeyboardButton(text="Cancel", callback_data="wc_no"),
    ]])
    await message.answer(f"Set the address to:\n{address}\n\nConfirm?", reply_markup=kb)


@router.callback_query(WalletChange.confirm, F.data == "wc_yes")
async def walletchange_confirm(call: CallbackQuery, state: FSMContext, party, repo) -> None:
    data = await state.get_data()
    await repo.update_wallet_address(
        wallet_id=data["wallet_id"],
        new_address=data["address"],
        actor_party_id=party["id"],
    )
    await state.clear()
    await call.message.edit_text(
        "Updated. Monitoring has moved to the new address."
    )
    await call.answer()


@router.callback_query(F.data == "wc_no")
async def walletchange_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Cancelled. No wallet was changed.")
    await call.answer()


# ------------------------------------------------------------ /walletremove

@router.message(Command("walletremove"))
async def cmd_walletremove(message: Message, repo) -> None:
    """
    Take a wallet out of service, keeping everything that went through it.

    Client request, 25 September 2026: "if i needed to remove an account
    wallet for a provider, and not have an associated wallet in place, can
    you add this".

    No FSM state. cancel_pick was gated on a state nothing set and the
    buttons were dead for days (22 September); a picker that re-reads the
    wallet on press has no reason to be gated, and the confirmation step is
    where the safety lives.
    """
    wallets = await repo.list_wallets()
    if not wallets:
        await message.answer("No wallets are configured.")
        return

    rows = []
    for w in wallets:
        if w["is_internal"]:
            text = f"Internal: {w['supplier_label']} → {w['client_label']}"
        else:
            text = f"{w['owner_label']}"
        rows.append([InlineKeyboardButton(text=text, callback_data=f"wr:{w['id']}")])

    await message.answer(
        "Which wallet do you want to retire?\n\n"
        "Nothing is deleted — its trades and deposits stay exactly as they "
        "are. The bot stops watching the address, and the address and the "
        "vendor slot become free to use again.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("wr:"))
async def walletremove_which(call: CallbackQuery, repo) -> None:
    """
    Say what will happen before it happens, in figures.

    A wallet with five settled trades behind it should not be retired on a
    two-word button. The count is read live rather than carried in the
    callback, so a stale button cannot understate what is at stake.
    """
    wallet_id = int(call.data.split(":", 1)[1])
    w = await repo.wallet_detail(wallet_id)
    if w is None:
        await call.message.answer("That wallet no longer exists.")
        await call.answer()
        return

    who = (f"{w['supplier_label']} → {w['client_label']}"
           if w["is_internal"] else w["owner_label"])
    lines = [
        f"Retire this wallet?", "",
        who,
        w["address"], "",
        f"Trades settled through it   {w['trades']}",
        f"Deposits received           {w['deposits']}",
        "",
        "Those stay. The address stops being watched.",
    ]
    if w["live_trades"]:
        lines += [
            "",
            f"⚠ {w['live_trades']} LIVE trade on this wallet. Retiring is "
            "blocked until it closes — the counterparty may be about to "
            "send to this address.",
        ]
    elif w["deposits"]:
        # Worth saying every time there is history, not only when something
        # is open. The address does not stop existing on TRON.
        lines += [
            "",
            "⚠ Anything sent to this address afterwards will arrive and the "
            "bot will not see it. Tell them the address is dead.",
        ]

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Retire it", callback_data=f"wr_yes:{wallet_id}"),
        InlineKeyboardButton(text="Cancel", callback_data="wr_no"),
    ]])
    await call.message.answer("\n".join(lines), reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("wr_yes:"))
async def walletremove_confirm(call: CallbackQuery, party, repo) -> None:
    wallet_id = int(call.data.split(":", 1)[1])
    ok, msg, detail = await repo.retire_wallet(
        wallet_id=wallet_id, actor_party_id=party["id"]
    )
    if not ok:
        await call.message.answer(msg)
        await call.answer()
        return

    await call.message.answer(
        f"{msg}\n\n"
        f"{detail['trades_kept']} trade(s) and {detail['deposits_kept']} "
        f"deposit(s) kept.\n"
        f"{detail['address']} is free to register again with /walletadd."
    )
    await call.answer()


@router.callback_query(F.data == "wr_no")
async def walletremove_cancel(call: CallbackQuery) -> None:
    await call.message.edit_text("Cancelled. No wallet was retired.")
    await call.answer()


# -------------------------------------------------------------- /addvendor
#
# Client request, 18 September 2026: "I'm going to create some more FX groups
# now so they are ready for later use add bots".
#
# He can make the groups and add the bots himself. Registering them was the
# part that still needed me at a terminal running deploy/seed-supplier.sql,
# which is a poor answer to wanting several ready in advance.
#
# The chat id used to be the hard part — there was no way to read it from
# inside the product. Adding the supplier bot to the new group now posts it
# to the Bridge channel (see bot/auth.py), so the two steps join up.

@router.message(Command("comms"))
async def cmd_comms(message: Message, repo) -> None:
    """
    What is owed to each introducer, and a button to settle it.

        we have a report similar to the utr statement, 1. XXXX Comms,
        2 XXXX Comms, and a total
        — Bridge, 23 November 2026

    Counted when the deal is SENT, which is his answer and not the obvious
    one — so an unpaid deal is already in here. Cancelled deals drop out,
    which means a day can change after he has looked at it. That is the
    trade-off he chose, and the reason a settlement names the trades it
    covers rather than a date.
    """
    rows = await repo.commission_due()
    if not rows:
        await message.answer("No commission outstanding.")
        return

    by_introducer: dict = {}
    for r in rows:
        by_introducer.setdefault(
            (r["introducer_id"], r["introducer"]), []).append(r)

    lines, buttons = [], []
    grand = Decimal(0)
    for (introducer_id, label), deals in by_introducer.items():
        subtotal = sum(d["introducer_usdt"] for d in deals)
        grand += subtotal
        lines.append(f"{label}")
        for n, d in enumerate(deals, 1):
            when = d["instructed_at"].strftime("%d %b")
            flag = "  (CANCELLED)" if d["status"] == "cancelled" else ""
            lines.append(
                f"  {n}. {when}  {d['reference']}  "
                f"{fmt_usdt_plain(d['introducer_usdt'])} USDT{flag}"
            )
        lines += [f"  Subtotal  {fmt_usdt_plain(subtotal)} USDT", ""]
        buttons.append([InlineKeyboardButton(
            text=f"Settle {label} — {fmt_usdt_plain(subtotal)} USDT",
            callback_data=f"comm:{introducer_id}",
        )])

    lines.append(f"Total owed   {fmt_usdt_plain(grand)} USDT")

    # A cancelled deal still listed means commission was counted when it
    # was sent and has since reversed. Said out loud rather than netted
    # off, so a settled day that later changed can be pointed at.
    if any(d["status"] == "cancelled" for deals in by_introducer.values()
           for d in deals):
        lines += [
            "",
            "Deals marked CANCELLED were counted when sent and have since "
            "been cancelled. Settle only what you mean to pay.",
        ]

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("comm:"))
async def comms_settle(call: CallbackQuery, party, repo) -> None:
    """
    Not state-gated. The total can sit on screen while he goes and sends
    the USDT, and a button that died in the meantime would leave him
    reconciling by memory — which is what the settlement record exists to
    prevent.

    Idempotent by construction: commission_settlements has one row per
    trade, so a second tap records nothing twice.
    """
    introducer_id = int(call.data.split(":", 1)[1])
    ok, msg, _ = await repo.settle_commission(
        introducer_id=introducer_id, actor_party_id=party["id"]
    )
    await call.message.answer(msg)
    await call.answer()


@router.message(Command("addintroducer"))
async def cmd_addintroducer(message: Message, state: FSMContext, repo) -> None:
    """
    Register an introducer's group and attach it to the vendor they brought.

        For the link I'd add /addintroducer on UI Control, same as
        /addvendor — register their group, pick the vendor.
        Ok, I think that is a good idea
        — 23 November 2026

    No wallet and no prefix: an introducer receives nothing through the bot
    and opens no deals. They are a reader of one vendor's book.
    """
    suppliers = await repo.list_parties("supplier")
    if not suppliers:
        await message.answer(
            "There are no vendors registered, so there is nobody for an "
            "introducer to have introduced."
        )
        return

    await state.set_state(AddIntroducer.label)
    await message.answer(
        "Registering an introducer.\n\n"
        "What are they called? This is for your own screens — the vendor "
        "and the client never see it."
    )


@router.message(AddIntroducer.label)
async def addintroducer_label(message: Message, state: FSMContext) -> None:
    label = (message.text or "").strip()
    if len(label) < 2:
        await message.answer("Give it a name of at least two characters.")
        return
    await state.update_data(label=label)
    await state.set_state(AddIntroducer.chat)
    await message.answer(
        "What is the introducer group's chat id?\n\n"
        "Create the group, add @pt_introducer_desk_bot to it, and it will "
        "post the id here. It starts with a minus sign."
    )


@router.message(AddIntroducer.chat)
async def addintroducer_chat(
    message: Message, state: FSMContext, repo
) -> None:
    raw = (message.text or "").strip()
    try:
        chat_id = int(raw)
    except ValueError:
        await message.answer(
            "That is not a chat id. It is a number, usually beginning with "
            "-100. Add the introducer bot to the group and I will post it "
            "here."
        )
        return

    # Same rule as a vendor group: access is granted by membership of a
    # group the Bridge controls, never by a private chat with one person.
    if chat_id >= 0:
        await message.answer(
            "That is a personal chat, not a group. Access is granted by "
            "group membership, so an introducer has to be a group you "
            "administer."
        )
        return

    await state.update_data(chat_id=chat_id)
    suppliers = await repo.list_parties("supplier")
    await state.set_state(AddIntroducer.vendor)
    await message.answer(
        "Which vendor did they introduce?",
        reply_markup=_party_keyboard(suppliers, "ai_sup"),
    )


@router.callback_query(AddIntroducer.vendor, F.data.startswith("ai_sup:"))
async def addintroducer_vendor(
    call: CallbackQuery, state: FSMContext, party, repo
) -> None:
    supplier_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()

    # Refused before the party is created, so a rejected attempt does not
    # leave an orphan introducer sitting in the book.
    existing = await repo.introducer_for_supplier(supplier_id)
    if existing is not None:
        await state.clear()
        await call.message.edit_text(
            f"That vendor already has an introducer ({existing['label']}). "
            "One per vendor — remove or change that one first."
        )
        await call.answer()
        return

    try:
        introducer_id = await repo.add_party(
            role="introducer", label=data["label"], display_name=None,
            telegram_chat_id=data["chat_id"], telegram_user_id=None,
            actor_party_id=party["id"],
        )
    except Exception as exc:
        await state.clear()
        await call.message.edit_text(
            f"Could not register that group: {exc}\n\nNothing was changed."
        )
        await call.answer()
        return

    ok, msg = await repo.set_introducer(
        supplier_id=supplier_id, introducer_id=introducer_id,
        actor_party_id=party["id"],
    )
    await state.clear()
    await call.message.edit_text(
        msg if ok else f"{msg}\n\nThe group was registered but not attached."
    )
    await call.answer()


@router.message(Command("addvendor"))
async def cmd_addvendor(message: Message, state: FSMContext, repo) -> None:
    clients = await repo.list_parties("client")
    if not clients:
        await message.answer(
            "There are no clients registered, so there is nothing to pair a "
            "vendor with yet."
        )
        return

    await state.set_state(AddVendor.label)
    await message.answer(
        "Registering a new vendor.\n\n"
        "What is it called? This is the name that appears on your own "
        "notifications — the client never sees it.\n\n"
        "It will follow the group's own title from then on, so an exact "
        "match now is not important."
    )


@router.message(AddVendor.label)
async def addvendor_label(message: Message, state: FSMContext) -> None:
    from db.repo import derive_prefix

    label = (message.text or "").strip()
    if len(label) < 2:
        await message.answer("Give it a name of at least two characters.")
        return

    try:
        suggested = derive_prefix(label)
    except ValueError:
        await message.answer(
            "I cannot make a deal prefix out of that name. Use one with some "
            "letters or numbers in it."
        )
        return

    await state.update_data(label=label, prefix=suggested)
    await state.set_state(AddVendor.prefix)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Use {suggested}", callback_data="av_prefix")
    ]])
    await message.answer(
        f"Deal numbers for {label} will read {suggested}1, {suggested}2, and "
        "so on.\n\nUse that, or type a different prefix.",
        reply_markup=kb,
    )


async def _addvendor_ask_client(target, state: FSMContext, repo) -> None:
    clients = await repo.list_parties("client")
    await state.set_state(AddVendor.client)
    await target.answer(
        "Which client does this vendor settle with?",
        reply_markup=_party_keyboard(clients, "av_cli"),
    )


@router.callback_query(AddVendor.prefix, F.data == "av_prefix")
async def addvendor_prefix_keep(call: CallbackQuery, state: FSMContext, repo) -> None:
    await _addvendor_ask_client(call.message, state, repo)
    await call.answer()


@router.message(AddVendor.prefix)
async def addvendor_prefix_typed(message: Message, state: FSMContext, repo) -> None:
    prefix = (message.text or "").strip().upper()
    if not prefix.isalnum() or not 2 <= len(prefix) <= 6:
        await message.answer(
            "A prefix is 2 to 6 letters or digits, nothing else. "
            "It is the front of every deal number this vendor ever gets."
        )
        return
    await state.update_data(prefix=prefix)
    await _addvendor_ask_client(message, state, repo)


@router.callback_query(AddVendor.client, F.data.startswith("av_cli:"))
async def addvendor_client(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(client_id=int(call.data.split(":", 1)[1]))
    await state.set_state(AddVendor.chat)
    await call.message.edit_text(
        "What is the vendor group's chat id?\n\n"
        "Add the supplier bot to the group and I will post the id here. "
        "It starts with a minus sign."
    )
    await call.answer()


@router.message(AddVendor.chat)
async def addvendor_chat(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        chat_id = int(raw)
    except ValueError:
        await message.answer(
            "That is not a chat id. It is a number, usually beginning with "
            "-100. Add the supplier bot to the group and I will post it here."
        )
        return

    # A group id is negative. A positive one is a private chat with a person,
    # and registering that as a vendor would hand one individual the whole
    # supplier role rather than a group the Bridge controls the membership of.
    if chat_id >= 0:
        await message.answer(
            "That is a personal chat, not a group. Access is granted by group "
            "membership, so a vendor has to be a group you administer."
        )
        return

    await state.update_data(chat_id=chat_id)
    await state.set_state(AddVendor.address)
    await message.answer(
        "Which internal wallet address receives this vendor's USDT?\n\n"
        "It must be one nobody else uses — a deposit is attributed by the "
        "wallet it lands in, so a shared address makes two vendors "
        "indistinguishable."
    )


@router.message(AddVendor.address)
async def addvendor_address(message: Message, state: FSMContext, repo) -> None:
    address = (message.text or "").strip()
    if not _is_tron_address(address):
        await message.answer(
            "That does not look like a TRON address. TRC20 addresses start "
            "with T and are 34 characters long."
        )
        return

    data = await state.update_data(address=address)
    client = await repo.party_label(data["client_id"])

    await state.set_state(AddVendor.confirm)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Register", callback_data="av_yes"),
        InlineKeyboardButton(text="Cancel", callback_data="av_no"),
    ]])
    await message.answer(
        f"Vendor      {data['label']}\n"
        f"Deals       {data['prefix']}1, {data['prefix']}2, ...\n"
        f"Settles     {client}\n"
        f"Group       {data['chat_id']}\n"
        f"Wallet      {address}\n\n"
        "Register this?",
        reply_markup=kb,
    )


@router.callback_query(AddVendor.confirm, F.data == "av_yes")
async def addvendor_confirm(
    call: CallbackQuery, state: FSMContext, party, repo
) -> None:
    data = await state.get_data()
    await state.clear()

    ok, msg, detail = await repo.onboard_supplier(
        label=data["label"], telegram_chat_id=data["chat_id"],
        prefix=data["prefix"], client_id=data["client_id"],
        wallet_address=data["address"], actor_party_id=party["id"],
    )
    if not ok:
        await call.message.edit_text(f"{msg}\n\nNothing was created.")
        await call.answer()
        return

    # Two things are deliberately missing, and saying so here is the point:
    # a vendor that looks finished but cannot trade is worse than one that
    # plainly is not finished yet.
    await call.message.edit_text(
        f"{detail['label']} is registered and its wallet is being watched.\n\n"
        "Two things left before it can trade:\n\n"
        f"  /setrate — no rate exists for {detail['label']} → "
        f"{detail['client_label']} yet, so a deposit would open no trade\n"
        "  /walletlink — say which address this pairing pays the client on\n\n"
        "Anything already on that wallet counts as history. Only transfers "
        "from now on are deposits."
    )
    await call.answer()


@router.callback_query(F.data == "av_no")
async def addvendor_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Cancelled. No vendor was registered.")
    await call.answer()


# -------------------------------------------------------------------- /send

@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext, repo) -> None:
    """
    Forward a send instruction, per the brief: supplier, client, hash, account.

    This is the Bridge's own manual path. It exists alongside the automatic
    deposit detection so a trade can still be pushed through when the monitor
    has not seen the transaction — B8's fallback, from your side rather than
    the supplier's.
    """
    suppliers = await repo.list_parties("supplier")
    if not suppliers:
        await message.answer("No suppliers are registered.")
        return
    await state.set_state(BridgeSend.supplier)
    await message.answer("Which supplier?", reply_markup=_party_keyboard(suppliers, "bs_sup"))


@router.callback_query(BridgeSend.supplier, F.data.startswith("bs_sup:"))
async def send_supplier(call: CallbackQuery, state: FSMContext, repo) -> None:
    await state.update_data(supplier_id=int(call.data.split(":", 1)[1]))
    clients = await repo.list_parties("client")
    if not clients:
        await call.message.edit_text("No clients are registered.")
        await state.clear()
        await call.answer()
        return
    await state.set_state(BridgeSend.client)
    await call.message.edit_text("Which client?", reply_markup=_party_keyboard(clients, "bs_cli"))
    await call.answer()


@router.callback_query(BridgeSend.client, F.data.startswith("bs_cli:"))
async def send_client(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(client_id=int(call.data.split(":", 1)[1]))
    await state.set_state(BridgeSend.hash_url)
    await call.message.edit_text("Hash URL?")
    await call.answer()


@router.message(BridgeSend.hash_url)
async def send_hash(message: Message, state: FSMContext, repo) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Send the transaction hash or its explorer URL.")
        return

    data = await state.get_data()
    accounts = await repo.list_bank_accounts(data["supplier_id"])
    if not accounts:
        await message.answer("That supplier has no registered accounts.")
        await state.clear()
        return

    await state.update_data(hash_url=text)
    await state.set_state(BridgeSend.account)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a["account_name"], callback_data=f"bs_acct:{a['id']}")]
        for a in accounts
    ])
    await message.answer("Which account should the funds go to?", reply_markup=kb)


@router.callback_query(BridgeSend.account, F.data.startswith("bs_acct:"))
async def send_account(call: CallbackQuery, state: FSMContext, repo, notifier) -> None:
    account_id = int(call.data.split(":", 1)[1])
    data = await state.get_data()
    await state.clear()

    accounts = {a["id"]: a for a in await repo.list_bank_accounts(data["supplier_id"])}
    account = accounts.get(account_id)
    if account is None:
        await call.message.edit_text("That account is no longer available.")
        await call.answer()
        return

    async with repo.pool.acquire() as conn:
        labels = await conn.fetchrow(
            "SELECT s.label AS s, c.label AS c FROM parties s, parties c "
            "WHERE s.id = $1 AND c.id = $2",
            data["supplier_id"], data["client_id"],
        )

    summary = (
        f"Send instruction\n"
        f"{labels['s']} → {labels['c']}\n"
        f"Hash {data['hash_url']}\n"
        f"Send to Account name {account['account_name']} "
        f"Account Number {account['account_number']} "
        f"IFSC {account['ifsc']}"
    )
    await call.message.edit_text(summary)
    await notifier.to_bridge(summary)
    await call.answer()


# ----------------------------------------------------------------- /summary

@router.message(Command("summary"))
async def cmd_summary(message: Message, repo) -> None:
    """F2: the current open trade only."""
    suppliers = await repo.list_parties("supplier")
    if not suppliers:
        await message.answer("No suppliers are registered.")
        return
    await message.answer(
        "Which supplier?", reply_markup=_party_keyboard(suppliers, "sum")
    )


@router.callback_query(F.data.startswith("sum:"))
async def summary_supplier(call: CallbackQuery, repo) -> None:
    supplier_id = int(call.data.split(":", 1)[1])

    async with repo.pool.acquire() as conn:
        trades = await conn.fetch(
            """
            SELECT t.reference, t.usdt_received, t.inr_expected, t.status,
                   c.label AS client_label,
                   COALESCE(SUM(p.amount_inr), 0) AS paid
            FROM trades t
            JOIN parties c ON c.id = t.client_id
            LEFT JOIN payments p ON p.trade_id = t.id
            WHERE t.supplier_id = $1 AND t.status IN ('open', 'awaiting_payment')
            GROUP BY t.id, c.label
            ORDER BY t.opened_at
            """,
            supplier_id,
        )

    if not trades:
        await call.message.edit_text("No open trades for this supplier.")
        await call.answer()
        return

    lines = []
    for t in trades:
        outstanding = t["inr_expected"] - t["paid"]
        lines.append(f"Transaction {t['reference']} → {t['client_label']}")
        lines.append(f"USDT received: {fmt_usdt(t['usdt_received'])}")
        lines.append(f"INR expected:  ₹{fmt_inr(t['inr_expected'])}")
        lines.append(f"Collected:     ₹{fmt_inr(t['paid'])}")
        lines.append(f"Outstanding:   ₹{fmt_inr(outstanding)}")
        lines.append("")

    await call.message.edit_text("\n".join(lines).rstrip())
    await call.answer()
