-- Reset the UAT ledger to a clean slate, keeping the setup.
--
-- For between test rounds. NOT for go-live — that starts from an empty
-- database, not a cleaned one, so nothing invented can survive into real data.
--
-- Keeps:   parties (groups stay registered), bank accounts, rates, wallets.
-- Clears:  every trade, payment, slot, deposit, counter, cursor, and the audit
--          trail those produced.
--
-- STOP THE BOT FIRST. This is not optional.
--
--   docker compose stop bot
--   docker compose exec -T db psql -U settlement -d settlement < deploy/reset-uat.sql
--   docker compose start bot
--
-- Clearing monitor_state under a running bot is a race, and the bot wins. It
-- re-adopts every wallet within one poll interval and writes fresh cursors
-- before you can restart it — so a reset intended to apply a fix instead
-- preserves the behaviour of the version still running. That happened on
-- 10 September 2026: the reset landed, the old container re-adopted twenty
-- seconds later, and the new image started up to find the old cursors already
-- in place and adoption already "done".
--
-- Change a wallet ADDRESS with /walletchange rather than SQL. That command
-- clears the monitoring cursor for the wallet, so the new address is not
-- scanned from an offset that belonged to the old one. An UPDATE here would
-- not.

BEGIN;

DELETE FROM payments;
DELETE FROM payment_slots;
DELETE FROM deposits;
DELETE FROM trades;

-- References restart at 1, so the next trade is SUPA1 again.
UPDATE supplier_counters SET last_number = 0;

-- The cursor decides how far back the monitor looks. Clearing it makes the
-- next poll scan from scratch for that wallet.
DELETE FROM monitor_state;

-- latest_nomination() reads the audit trail, not the trades table. A
-- nomination made during testing would otherwise attach itself to the next
-- trade opened — including a real one.
DELETE FROM audit_log;

COMMIT;

SELECT 'trades' AS table_name, count(*) FROM trades
UNION ALL SELECT 'payments', count(*) FROM payments
UNION ALL SELECT 'deposits', count(*) FROM deposits
UNION ALL SELECT 'monitor_state', count(*) FROM monitor_state
UNION ALL SELECT 'parties (kept)', count(*) FROM parties
UNION ALL SELECT 'bank_accounts (kept)', count(*) FROM bank_accounts
UNION ALL SELECT 'rates (kept)', count(*) FROM rates
UNION ALL SELECT 'wallets (kept)', count(*) FROM wallets;
