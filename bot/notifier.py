"""
Outbound messaging and the deposit → trade pipeline.

The Notifier owns every message the system sends unprompted, and the logic that
turns a detected deposit into an open trade with a payment instruction. It sits
here rather than in the monitor so the monitor stays a dumb, reliable poller.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from core.money import fmt_inr, inr_to_usdt, margin_usdt, usdt_to_inr
from core.summary import (
    Payment, render_completion_notice, render_deposit_notification,
    render_supplier_summary, render_trade_summary,
)

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, *, bridge_bot: Bot, supplier_bot: Bot, client_bot: Bot,
                 repo, config):
        self.bridge_bot = bridge_bot
        self.supplier_bot = supplier_bot
        self.client_bot = client_bot
        self.repo = repo
        self.config = config

    # ------------------------------------------------------------- delivery

    async def to_bridge(self, text: str, reply_markup=None, *,
                        html: bool = False) -> None:
        try:
            await self.bridge_bot.send_message(
                self.config.bridge_channel_id, text, reply_markup=reply_markup,
                parse_mode="HTML" if html else None,
                # Telegram would otherwise render a card for the explorer link
                # and push the figures off the screen.
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            # A failed notification must never take down the caller - the
            # deposit is already recorded, and losing the message is recoverable
            # where losing the deposit is not.
            log.exception("failed to notify bridge channel")

    async def to_party(self, party_id: int, text: str, *, html: bool = False) -> None:
        async with self.repo.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT role, telegram_chat_id, telegram_user_id
                FROM parties WHERE id = $1 AND is_active
                """,
                party_id,
            )
        if row is None:
            log.warning("cannot notify unknown or inactive party %s", party_id)
            return

        bot = self.supplier_bot if row["role"] == "supplier" else self.client_bot
        chat_id = row["telegram_chat_id"]
        try:
            await bot.send_message(
                chat_id, text, parse_mode="HTML" if html else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except Exception:
            log.exception("failed to notify party %s", party_id)

    # ------------------------------------------------ near-completion notice

    async def check_near_completion(self, trade_id: int) -> None:
        """
        Tell the supplier to prepare the next batch when a trade is nearly paid.

        Client request, 8 September 2026: "when we are 300,000 or less remaining
        on trade, can we notify in bot to Supplier — Prepare next batch, close to
        completion". The point is lead time: the supplier can have the next
        deposit ready rather than starting from cold once this trade closes.

        Called after every payment. The claim is atomic, so it fires once per
        trade however many payments land together.
        """
        trade = await self.repo.claim_near_completion(
            trade_id, self.config.near_completion_inr
        )
        if trade is None:
            return

        paid = await self.repo.trade_paid_total(trade_id)
        outstanding = trade["inr_expected"] - paid

        await self.to_party(
            trade["supplier_id"],
            "Prepare next batch — close to completion\n\n"
            f"Transaction {trade['reference']}\n"
            f"Outstanding: ₹{fmt_inr(outstanding)} of ₹{fmt_inr(trade['inr_expected'])}",
        )
        await self.to_bridge(
            f"{trade['reference']} is close to completion "
            f"(₹{fmt_inr(outstanding)} outstanding). Supplier has been asked to "
            "prepare the next batch."
        )
        log.info("near-completion notice sent for trade %s", trade["reference"])

    # --------------------------------------------------- automatic completion

    async def check_completion(self, trade_id: int) -> bool:
        """
        Close and distribute the moment the payments cover the expected total.

        Client request, 10 September 2026: "yes, remove /done, have it
        calculate". Until now a trade stayed open until someone typed the
        command, so a fully paid trade could sit there with the supplier and the
        Bridge none the wiser.

        /done still exists as the way to close a trade that will never be paid
        in full — a shortfall the Bridge has decided to accept. This handles the
        ordinary case, which is every trade that is actually paid.

        Returns True if this call is the one that closed it.
        """
        trade = await self.repo.claim_completion(trade_id)
        if trade is None:
            return False

        rows = await self.repo.trade_payments(trade_id)
        if not rows:
            # Cannot happen — the claim requires payments covering the total —
            # but a summary with no payments would raise, and this must never
            # take down the paste that triggered it.
            log.error("trade %s claimed as complete with no payments",
                      trade["reference"])
            return False

        payments = [
            Payment(utr=r["utr"], amount_inr=r["amount_inr"],
                    beneficiary_name=r["account_name"])
            for r in rows
        ]
        total = sum(p.amount_inr for p in payments)

        summary = render_trade_summary(
            payments,
            reference=trade["reference"],
            expected_inr=trade["inr_expected"] or None,
        )

        await self.to_party(trade["client_id"], summary)
        await self.to_party(trade["client_id"], render_completion_notice(total))
        await self.to_bridge(summary)
        await self.to_party(
            trade["supplier_id"],
            render_supplier_summary(payments, expected_inr=trade["inr_expected"] or None),
        )

        log.info("trade %s completed automatically, total %s",
                 trade["reference"], total)
        return True

    # ------------------------------------------------- deposit → trade flow

    async def on_supplier_deposit(
        self, *, wallet, deposit_id: int, amount_usdt: Decimal, tx_hash: str,
    ) -> None:
        """
        A supplier deposit landed on an internal wallet.

        C3: the pairing is inferred from the wallet, so no manual selection is
        needed. B6: if a trade is already open for this wallet, the deposit
        accumulates against it rather than opening a second one.
        """
        supplier_id = wallet["supplier_id"]
        client_id = wallet["client_id"]

        rate = await self.repo.current_rate(supplier_id, client_id)
        if rate is None:
            await self.to_bridge(
                f"Deposit of {amount_usdt} USDT received, but NO RATE is set for "
                f"this pairing. No trade was opened.\nHash {tx_hash}"
            )
            return

        # C5: warn on a stale rate rather than silently applying it.
        stale_note = ""
        if float(rate["age_hours"]) > self.config.rate_staleness_hours:
            stale_note = (
                f"\n\nWARNING: this rate was set {float(rate['age_hours']):.0f} "
                "hours ago. Check it is still correct."
            )

        async with self.repo.pool.acquire() as conn:
            async with conn.transaction():
                trade = await conn.fetchrow(
                    """
                    SELECT * FROM trades
                    WHERE wallet_id = $1 AND status IN ('open', 'awaiting_payment')
                    FOR UPDATE
                    """,
                    wallet["id"],
                )

                if trade is None:
                    reference = await self.repo.next_reference(conn, supplier_id)
                    # If the supplier ran /send before the deposit was detected,
                    # carry their nominated account onto the new trade so the
                    # order of those two events does not matter.
                    nominated = await self.repo.latest_nomination(supplier_id)
                    trade_id = await conn.fetchval(
                        """
                        INSERT INTO trades (reference, supplier_id, client_id, wallet_id,
                                            rate_id, supply_rate, sell_rate, status,
                                            nominated_account_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, 'open', $8)
                        RETURNING id
                        """,
                        reference, supplier_id, client_id, wallet["id"],
                        rate["id"], rate["supply_rate"], rate["sell_rate"], nominated,
                    )
                    running_usdt = amount_usdt
                else:
                    trade_id = trade["id"]
                    reference = trade["reference"]
                    running_usdt = trade["usdt_received"] + amount_usdt

                # Recompute from the running total, never incrementally - a
                # sum of rounded parts is not the rounding of the sum.
                inr_expected = usdt_to_inr(running_usdt, trade["supply_rate"] if trade else rate["supply_rate"])
                sell = trade["sell_rate"] if trade else rate["sell_rate"]
                usdt_owed = inr_to_usdt(inr_expected, sell)
                margin = margin_usdt(
                    running_usdt,
                    trade["supply_rate"] if trade else rate["supply_rate"],
                    sell,
                )

                await conn.execute(
                    """
                    UPDATE trades
                    SET usdt_received = $2, inr_expected = $3,
                        usdt_owed_client = $4, margin_usdt = $5,
                        status = 'awaiting_payment'
                    WHERE id = $1
                    """,
                    trade_id, running_usdt, inr_expected, usdt_owed, margin,
                )
                await conn.execute(
                    "UPDATE deposits SET trade_id = $2 WHERE id = $1",
                    deposit_id, trade_id,
                )
                await self.repo.audit(
                    conn, actor_party_id=None, action="deposit.allocated",
                    entity_type="trade", entity_id=trade_id,
                    detail={
                        "tx_hash": tx_hash,
                        "amount_usdt": amount_usdt,
                        "running_usdt": running_usdt,
                    },
                )

        async with self.repo.pool.acquire() as conn:
            labels = await conn.fetchrow(
                """
                SELECT s.label AS supplier_label, c.label AS client_label
                FROM parties s, parties c WHERE s.id = $1 AND c.id = $2
                """,
                supplier_id, client_id,
            )

        # The confirm button is attached here so the Bridge can go straight from
        # "a deposit landed" to issuing the client's payment instruction without
        # hunting for a command.
        from bot.bridge_trade import confirm_keyboard

        await self.to_bridge(
            render_deposit_notification(
                reference=reference,
                supplier_label=labels["supplier_label"],
                client_label=labels["client_label"],
                usdt_in=amount_usdt,
                inr_out=inr_expected,
                tx_hash=tx_hash,
                wallet_address=wallet["address"],
                # Both rates and the onward amount, so the Bridge can act on
                # this message without looking anything up (client request,
                # 10 September 2026).
                supply_rate=trade["supply_rate"] if trade else rate["supply_rate"],
                sell_rate=sell,
                usdt_out=usdt_owed,
            )
            + stale_note,
            reply_markup=confirm_keyboard(trade_id),
        )
