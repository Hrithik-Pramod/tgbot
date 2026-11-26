-- ===========================================================================
-- Retire a vendor: their group goes dead, their history stays.
--
-- WHY NOT A DELETE
--
--   Trades, payments and UTRs reference the party. Deleting it would either
--   fail on the foreign keys or, worse, cascade and take a year of
--   settlement history with it. A retired vendor is one that can no longer
--   act, not one that never existed — /export, /vendorstatement and every
--   reconciliation still need their deals.
--
-- WHAT RETIRING ACTUALLY DOES
--
--   is_active = FALSE on the party. party_by_chat_id filters on it, so the
--   group stops being recognised — anything typed there is ignored exactly
--   as an unregistered chat is. It also disappears from /setrate, /addvendor
--   and every other picker, which is the point: a desk carrying eleven
--   vendors when two are trading is a list to mis-tap.
--
-- AND THE WALLET, WHICH IS THE PART THAT MATTERS
--
--   A deposit does not resolve the vendor through the party row. It resolves
--   through the WALLET it landed in — wallets.supplier_id — and that lookup
--   knows nothing about is_active. So a party deactivated on its own leaves a
--   monitored wallet that will still open a trade for a vendor the desk
--   believes is gone, with no group to tell about it.
--
--   So monitoring stops in the same transaction. Either both happen or
--   neither does.
--
-- USAGE — one vendor per run, by id:
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v party=4 \
--     -f - < deploy/retire-vendor.sql
--
--   Bare, not quoted. Nothing after the value on the -v line.
--
-- REVERSING IT
--
--   UPDATE parties SET is_active = TRUE WHERE id = <party>;
--   plus /walletadd or a re-monitor of the wallet. The audit row records the
--   wallet ids that were switched off, so nothing has to be guessed.
--
-- SAFETY
--
--   Guards divide by their own answer: true gives 1/1, false gives 1/0,
--   which aborts under ON_ERROR_STOP. Not written as
--   CASE WHEN ok THEN 1 ELSE 1/0 END — 1/0 is constant, the planner folds it
--   before the CASE runs and it raises even when the check passed. Verified
--   live, 21 October 2026.
-- ===========================================================================

\echo
\echo '--- before ---'

SELECT p.id, p.label, p.telegram_chat_id, p.is_active,
       (SELECT count(*) FROM trades t
         WHERE t.supplier_id = p.id
           AND t.status IN ('open', 'awaiting_payment'))      AS live_trades,
       (SELECT count(*) FROM wallets w
         WHERE w.supplier_id = p.id AND w.is_monitored)       AS watched_wallets
  FROM parties p
 WHERE p.id = :party;

BEGIN;

SELECT
    -- 1. It is a supplier. The bridge row would lock the desk out of itself;
    --    the client row would stop every trade settling.
    1 / ((SELECT count(*) = 1 FROM parties
           WHERE id = :party AND role = 'supplier')::int)      AS is_a_vendor,

    -- 2. Still active. Retiring a retired vendor is a no-op reported as a
    --    success, which sends somebody looking for a second cause.
    1 / ((SELECT is_active FROM parties
           WHERE id = :party)::int)                            AS still_active,

    -- 3. NOTHING COLLECTING. This is the one that matters. A vendor retired
    --    mid-collection leaves rupees arriving against a group the bot no
    --    longer reads: the payments cannot be recorded and the vendor cannot
    --    be told why. Finish or cancel the trade first.
    1 / ((SELECT count(*) = 0 FROM trades t
           WHERE t.supplier_id = :party
             AND t.status IN ('open', 'awaiting_payment'))::int)
                                                               AS nothing_live;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'party.retired', 'party', p.id,
       jsonb_build_object(
           'label',           p.label,
           'chat_id',         p.telegram_chat_id,
           'wallets_unwatched', (
               SELECT coalesce(jsonb_agg(w.id), '[]'::jsonb)
                 FROM wallets w
                WHERE w.supplier_id = p.id AND w.is_monitored),
           'why',             'retired by the Bridge'
       )
  FROM parties p
 WHERE p.id = :party;

-- The wallet first. If anything below fails the whole transaction rolls back,
-- but the ordering says what is being protected: no monitored wallet may
-- outlive the vendor it belongs to.
UPDATE wallets
   SET is_monitored = FALSE
 WHERE supplier_id = :party
   AND is_monitored;

UPDATE parties
   SET is_active = FALSE
 WHERE id = :party
   AND role = 'supplier';

COMMIT;

\echo
\echo '--- after ---'

SELECT p.id, p.label, p.is_active,
       (SELECT count(*) FROM wallets w
         WHERE w.supplier_id = p.id AND w.is_monitored) AS watched_wallets
  FROM parties p
 WHERE p.id = :party;

\echo
\echo 'The group is now ignored and the wallet is no longer watched.'
\echo 'History is untouched: /export and /vendorstatement still have the deals.'
\echo
