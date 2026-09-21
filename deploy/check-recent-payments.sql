-- Reconcile a payment the Bridge believes was made against what the bot holds.
--
-- WHY THIS EXISTS (21 September 2026)
--
-- The Bridge reported a trade paid and named the beneficiary. The bot held
-- that trade as instructed to a DIFFERENT account with nothing collected.
-- Both cannot be true, and acting on either without checking is how the
-- 11 and 16 September losses happened.
--
-- Read-only. Run it before recording anything by hand.
--
--   docker compose exec -T db psql -U $PGUSER -d $PGDATABASE \
--       < deploy/check-recent-payments.sql

\echo '=== 1. every payment recorded in the last 24 hours ==='
SELECT t.reference, p.utr, p.amount_inr, b.account_name AS paid_into,
       p.created_at
FROM payments p
JOIN trades t        ON t.id = p.trade_id
JOIN bank_accounts b ON b.id = p.beneficiary_account_id
WHERE p.created_at > now() - interval '24 hours'
ORDER BY p.created_at;

\echo ''
\echo '=== 2. every live trade and the accounts it was instructed to ==='
SELECT t.reference, t.status, t.inr_expected,
       b.account_name AS instructed_to, s.amount_inr AS slot_amount,
       t.instructed_at
FROM trades t
LEFT JOIN payment_slots s ON s.trade_id = t.id
LEFT JOIN bank_accounts b ON b.id = s.bank_account_id
WHERE t.status IN ('open', 'awaiting_payment')
ORDER BY t.reference, s.id;

\echo ''
\echo '=== 3. every account, and whether it has ever been used ==='
-- An account with times_instructed = 0 has never been put in front of the
-- client, so money cannot have arrived there through the bot.
SELECT b.id, b.account_name, b.is_active, pa.label AS supplier,
       (SELECT count(*) FROM payment_slots s
          WHERE s.bank_account_id = b.id)            AS times_instructed,
       (SELECT count(*) FROM payments p
          WHERE p.beneficiary_account_id = b.id)     AS payments_received
FROM bank_accounts b
JOIN parties pa ON pa.id = b.party_id
ORDER BY pa.label, b.id;
