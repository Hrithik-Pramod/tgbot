-- ===========================================================================
-- Bring a retired vendor back.
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 -v party=0 \
--     -f - < deploy/unretire-vendor.sql
--
-- WHY IT EXISTS
--
--   Eight vendors were retired on 26 November 2026 and one came back the
--   same night under a new group:
--
--       And i have just created a new group / Cloud
--       — Bridge, 23:34
--       replace — Bridge, 00:02
--
--   A replacement keeps the vendor's deal numbering, rates and settlement
--   history; only its Telegram group changes. So the row is reactivated
--   rather than a second vendor being created, which would restart the deal
--   references and split one counterparty's book in two.
--
--   Run this, then point it at the new group with change-vendor-group.sql.
--
-- WHAT IT DOES NOT DO: THE WALLET
--
--   retire-vendor.sql stops monitoring a vendor's wallets, and this turns
--   that back on — but ONLY for wallets that are not themselves retired.
--
--   A wallet retired with /walletremove was taken out of service as an
--   address, which is a different decision from the vendor being switched
--   off, and usually means the address itself is finished with. Cloud's was
--   retired at 03:23 on 26 November, hours before the vendor was. Putting a
--   finished address back into service silently is how a deposit ends up
--   watched at an address nobody is using any more.
--
--   So if every wallet a vendor has is retired, this reports it and the
--   Bridge gives it a fresh address with /walletadd. A vendor with no
--   watched wallet cannot receive a deposit, and the output says so rather
--   than leaving it to be discovered by a transfer that opens nothing.
--
-- SAFETY
--
--   Guards divide by their own answer: true gives 1/1, false gives 1/0,
--   which aborts under ON_ERROR_STOP. Not CASE WHEN ok THEN 1 ELSE 1/0 END
--   — 1/0 is constant, the planner folds it, and it raises even when the
--   check passed. Verified live, 21 October 2026.
-- ===========================================================================

\echo
\echo '--- before ---'

SELECT p.id, p.label, p.telegram_chat_id, p.is_active,
       (SELECT count(*) FROM wallets w
         WHERE w.supplier_id = p.id)                        AS wallets,
       (SELECT count(*) FROM wallets w
         WHERE w.supplier_id = p.id AND w.retired_at IS NULL)
                                                            AS usable_wallets,
       (SELECT count(*) FROM rates r WHERE r.supplier_id = p.id) AS rates
  FROM parties p
 WHERE p.id = :party;

BEGIN;

SELECT
    -- 1. A supplier. The bridge and client rows are not retired or revived
    --    by this script.
    1 / ((SELECT count(*) = 1 FROM parties
           WHERE id = :party AND role = 'supplier')::int)    AS is_a_vendor,

    -- 2. Currently retired. Running this on a live vendor would report
    --    success for doing nothing.
    1 / ((SELECT NOT is_active FROM parties
           WHERE id = :party)::int)                          AS is_retired;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'party.unretired', 'party', p.id,
       jsonb_build_object(
           'label',   p.label,
           'chat_id', p.telegram_chat_id,
           'wallets_rewatched', (
               SELECT coalesce(jsonb_agg(w.id), '[]'::jsonb)
                 FROM wallets w
                WHERE w.supplier_id = p.id
                  AND w.retired_at IS NULL
                  AND NOT w.is_monitored),
           'why',     'brought back by the Bridge'
       )
  FROM parties p
 WHERE p.id = :party;

-- Only addresses that are still in service. A retired address stays retired.
UPDATE wallets
   SET is_monitored = TRUE
 WHERE supplier_id = :party
   AND retired_at IS NULL
   AND NOT is_monitored;

UPDATE parties
   SET is_active = TRUE
 WHERE id = :party
   AND role = 'supplier';

COMMIT;

\echo
\echo '--- after ---'

SELECT p.id, p.label, p.is_active,
       (SELECT count(*) FROM wallets w
         WHERE w.supplier_id = p.id AND w.is_monitored)  AS watched_wallets
  FROM parties p
 WHERE p.id = :party;

SELECT CASE
         WHEN (SELECT count(*) FROM wallets w
                WHERE w.supplier_id = :party AND w.is_monitored) = 0
         THEN 'NO WATCHED WALLET — this vendor cannot receive a deposit. '
              'Give it an address with /walletadd, then /walletlink.'
         ELSE 'Wallet is being watched.'
       END AS deposit_address;

\echo
\echo 'Next: point it at its group with change-vendor-group.sql,'
\echo 'then check the rate with /viewrate before it trades.'
\echo
