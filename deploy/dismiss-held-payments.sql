-- ===========================================================================
-- Close a run of held payments that are not ours to place.
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v account="'00000000000000'" \
--     -v why="'settled by hand on the previous order — Bridge, dd Mmm'" \
--     -f - < deploy/dismiss-held-payments.sql
--
-- WHAT A DISMISSAL IS, AND IS NOT
--
--   It is the same thing as tapping "Not ours — ignore it" on the prompt:
--   resolved_at is stamped, resolved_trade_id stays NULL, and the six-hourly
--   chaser stops. The row keeps its UTR, its amount and its beneficiary for
--   ever.
--
--   It does NOT create a payment, move money, or touch a trade. A dismissed
--   slip is money the desk has decided it is not placing — usually because
--   it was settled outside the system, or it was never ours.
--
-- WHY IT EXISTS AS A SCRIPT
--
--   The button is per slip, and the button is right: each one is a decision.
--   But on 27 November 2026 seventeen of them, ₹4,238,026, belonged to a Raj
--   order that had been settled by hand the day before:
--
--       ok royal i said to clear the slip clear this now
--       i said last night
--       confirm this is done
--       — Bridge, 10:58
--
--   Seventeen identical taps is the same reflex test the audit warns about
--   elsewhere in this project — nineteen prompts in three hours, two of them
--   tapped onto the wrong order. One deliberate instruction covering a named
--   set is safer than seventeen chances to mis-tap.
--
--   So the set is named by ACCOUNT, not by a list of ids, and the reason is
--   required and recorded on every row. Reading the audit log afterwards
--   tells you what was closed, how much, and on whose word.
--
-- REVERSING IT
--
--   UPDATE held_payments SET resolved_at = NULL WHERE id IN (...);
--   The rows are intact; only the stamp is set. The chaser then picks them
--   up again on its next sweep.
--
-- BEFORE RUNNING IT
--
--   Read the "before" list. If any line is money that still needs placing,
--   stop — placing it afterwards means finding it again by hand.
-- ===========================================================================

\echo
\echo '--- will be dismissed ---'

SELECT id, amount_inr, typed_beneficiary,
       asked_at AT TIME ZONE 'Asia/Kolkata' AS asked_ist
  FROM held_payments
 WHERE resolved_at IS NULL
   AND account_number = :account
 ORDER BY id;

SELECT count(*) AS slips, sum(amount_inr) AS total_inr
  FROM held_payments
 WHERE resolved_at IS NULL
   AND account_number = :account;

BEGIN;

SELECT
    -- 1. Something matches. A script that reports success for closing
    --    nothing sends somebody looking for a second cause.
    1 / ((SELECT count(*) > 0 FROM held_payments
           WHERE resolved_at IS NULL AND account_number = :account)::int)
                                                        AS something_to_close,

    -- 2. A reason was given. The audit row is the only record of WHY this
    --    money was not placed, and "no reason recorded" is indistinguishable
    --    from a mistake six months later.
    1 / ((length(trim(:why)) > 10)::int)                AS reason_given;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'payment.dismissed', 'held_payment', h.id,
       jsonb_build_object(
           'utr',         h.utr,
           'amount_inr',  h.amount_inr,
           'beneficiary', h.typed_beneficiary,
           'why',         :why
       )
  FROM held_payments h
 WHERE h.resolved_at IS NULL
   AND h.account_number = :account;

UPDATE held_payments
   SET resolved_at = now()
 WHERE resolved_at IS NULL
   AND account_number = :account;

COMMIT;

\echo
\echo '--- still waiting on an answer ---'

SELECT count(*) AS slips, coalesce(sum(amount_inr), 0) AS total_inr
  FROM held_payments
 WHERE resolved_at IS NULL;

\echo
\echo 'Dismissed, not deleted. Every UTR is still on record and the audit log'
\echo 'carries the reason. No payment was created and no trade was touched.'
\echo
