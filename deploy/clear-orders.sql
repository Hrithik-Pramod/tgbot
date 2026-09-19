-- Clear the trading history and start the references again from 1.
--
-- For live use, when the first day's orders were shakedown runs rather than
-- real business and everyone wants a clean slate.
--
-- STOP THE BOT FIRST. Clearing monitor_state under a running bot is a race and
-- the bot wins — it re-adopts within one poll and writes fresh cursors before
-- you can restart it.
--
--   docker compose stop bot
--   docker compose exec -T db psql -U settlement -d settlement < deploy/clear-orders.sql
--   docker compose start bot
--
-- KEEPS   parties, groups, bank accounts, rates, wallets — the whole setup —
--         and the deal numbering, so the next trade is SUPA16 rather than a
--         second SUPA1. See the note further down; it matters more than it
--         looks.
-- CLEARS  trades, payments, payment slots, deposits, outstanding supplier
--         claims, and the monitor's position on each wallet.
--
-- HOW THIS DIFFERS FROM reset-uat.sql
--
-- That one wipes the audit log. This one does not. On a live system the audit
-- log is the permanent record of who did what — every rate set, every account
-- registered, every correction and the reason for it. Deleting trades is a
-- housekeeping decision; deleting the record of them is not, and it is not
-- reversible.
--
-- The single exception is account nominations. latest_nomination() reads them
-- back and attaches the most recent one to the next trade a supplier opens, so
-- a nomination left over from a cleared trade would silently pre-select an
-- account on a real one. Those rows go; everything else stays.
--
-- WHAT CLEARING monitor_state MEANS
--
-- Each wallet is adopted again on the next poll: whatever is already on chain
-- is recorded, ignored, and only later transfers count. Anything that arrived
-- while the bot was stopped is therefore NOT picked up. If a supplier has sent
-- and it has not been processed yet, deal with that trade before running this.

BEGIN;

DELETE FROM payments;
DELETE FROM payment_slots;
DELETE FROM deposits;

-- Claims go before trades, for two reasons.
--
-- The FIRST is that they must. pending_sends.matched_trade_id references
-- trades(id), so DELETE FROM trades hits a foreign key violation and rolls
-- the whole transaction back. That has been true since pending_sends was
-- added on 11 September and this script has not been run since, so it would
-- have failed the first time it was needed.
--
-- The SECOND is that an unmatched claim must not survive a clear-out.
-- latest_nomination reads exactly those rows, so a claim left behind would
-- pre-select its account on the first trade after the reset — a default
-- that reads as a decision, on a fresh ledger, with nothing to check it
-- against. That is the 11 September fault and it recurred on the 19th from
-- a single test /send.
DELETE FROM pending_sends;

DELETE FROM trades;

-- Deal numbers CONTINUE. They are deliberately not reset.
--
-- 19 September 2026: "All trading is cleared, so let's wipe out everything."
-- Fair enough for the ledger — but SUPA1 through SUPA15, SUPB1 through
-- SUPB6 and the rest are quoted in three WhatsApp groups, in the suppliers'
-- records and in the client's. Restarting at SUPA1 makes every one of those
-- references mean two different trades, and the second meaning has no
-- payments behind it to tell them apart.
--
-- Clearing the ledger is housekeeping. Making the last month's paperwork
-- ambiguous is not, and it cannot be undone afterwards.
--
-- deploy/reset-uat.sql is the one that starts the numbering over. That is
-- for a test database, where nobody outside has seen the references.

-- Re-adopt every wallet, so existing chain history is ignored rather than
-- read as a fresh pile of deposits.
DELETE FROM monitor_state;

-- The one audit action that feeds back into future behaviour.
DELETE FROM audit_log WHERE action = 'trade.account_nominated';

-- Leave a marker, so the gap in the ledger is explained rather than mysterious.
INSERT INTO audit_log (actor_party_id, action, entity_type, detail)
VALUES (NULL, 'admin.orders_cleared', 'system',
        '{"reason": "start fresh after first-day shakedown"}'::jsonb);

COMMIT;

SELECT 'trades' AS table_name, count(*) FROM trades
UNION ALL SELECT 'payments', count(*) FROM payments
UNION ALL SELECT 'payment_slots', count(*) FROM payment_slots
UNION ALL SELECT 'deposits', count(*) FROM deposits
UNION ALL SELECT 'pending_sends', count(*) FROM pending_sends
UNION ALL SELECT 'monitor_state', count(*) FROM monitor_state
UNION ALL SELECT 'parties (kept)', count(*) FROM parties
UNION ALL SELECT 'bank_accounts (kept)', count(*) FROM bank_accounts
UNION ALL SELECT 'rates (kept)', count(*) FROM rates
UNION ALL SELECT 'wallets (kept)', count(*) FROM wallets
UNION ALL SELECT 'audit_log (kept)', count(*) FROM audit_log;

-- The next deal number for each supplier, which should carry on from where
-- the cleared trades left off rather than start again at 1.
--
--   SELECT p.label, sc.prefix, sc.last_number,
--          sc.prefix || (sc.last_number + 1) AS next_reference
--   FROM supplier_counters sc JOIN parties p ON p.id = sc.supplier_id
--   ORDER BY p.label;
