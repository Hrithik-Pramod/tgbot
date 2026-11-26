-- ===========================================================================
-- Record a deposit the monitor was never going to see.
--
-- WHEN THIS IS THE RIGHT TOOL, AND WHEN IT IS NOT
--
--   Only for a transfer that arrived BEFORE its address was being watched.
--   That is one situation and it has one shape: /walletchange or /walletadd
--   registers an address, adoption files everything already on it as history,
--   and a transfer sent in the gap is deliberately ignored.
--
--   26 November 2026:
--
--       23:04:09   a five-figure USDT transfer arrives at a new address
--       23:22:37   /walletchange points a vendor at that same address
--       23:22:4x   adoption files the 23:04 transfer as pre-existing history
--
--       a trade was sent but the bot was set to monitor wrong wallet
--       i need to issue — Bridge, 23:24
--
--   The wallet was not wrong and the bot was not broken. The money simply
--   arrived eighteen minutes before anyone told the bot where to look. The
--   adoption rule is what stops a newly added address importing months of
--   unrelated transfers as deposits, and it cost a real deposit here.
--
--   It is NOT for a deposit the monitor should have caught. If the address
--   was already being watched when the money landed, something is wrong and
--   writing the row by hand hides it. Check monitor_state first: a cursor
--   at `transfer + OVERLAP_MS + 1` is adoption's signature; a cursor at
--   exactly `transfer` is a normal poll, and then the deposit should exist.
--
-- VERIFY THE TRANSFER ON CHAIN FIRST. ALL OF IT.
--
--   This script takes your word for every value. Nothing here is checked
--   against TRON — the hash, the amount, the sender and the destination are
--   all asserted by whoever runs it. A wrong amount becomes a trade for the
--   wrong money, and the vendor collects rupees against it.
--
--   Read the destination address off the chain and confirm it is the SAME
--   address the wallet row now holds. The guard below checks that the hash
--   is new and the wallet is one we watch; it cannot check that the transfer
--   you are describing actually went there.
--
-- USAGE
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v wallet=0 \
--     -v hash="'0000000000000000000000000000000000000000000000000000000000000000'" \
--     -v amount=0.000000 \
--     -v sender="'TExampleSenderAddress0000000000000'" \
--     -v block=0 \
--     -f - < deploy/record-missed-deposit.sql
--
--   hash and sender are quoted INSIDE the value, as shown. wallet, amount
--   and block are bare. Put NOTHING after a value on a -v line.
--
-- THEN PRICE IT
--
--   This writes the deposit only. It does NOT open a trade, because pricing
--   belongs to one script and there is no reason to have two:
--
--     docker compose exec -T db psql -U settlement -d settlement \
--       -v ON_ERROR_STOP=1 -v hash="<the same hash>" \
--       -f - < deploy/reopen-deposit-as-trade.sql
--
--   That prices it at the rate in force now and stamps announced_at, so the
--   Bridge issues it with /issue rather than waiting for a notification that
--   was never going to come.
--
-- STATUS IS 'confirmed', DELIBERATELY
--
--   You have just read the transaction on chain. A deposit left 'detected'
--   would be reported to the Bridge as unconfirmed after fifteen minutes,
--   which is a false alarm about a transfer that settled hours ago.
-- ===========================================================================

\echo
\echo '--- the wallet this will be recorded against ---'

SELECT w.id, w.address, w.is_internal, w.is_monitored,
       s.label AS vendor, c.label AS client,
       to_timestamp(m.adopted_at_ms/1000) AT TIME ZONE 'Asia/Kolkata'
           AS adopted_ist
  FROM wallets w
  LEFT JOIN parties s ON s.id = w.supplier_id
  LEFT JOIN parties c ON c.id = w.client_id
  LEFT JOIN monitor_state m ON m.wallet_id = w.id
 WHERE w.id = :wallet;

BEGIN;

SELECT
    -- 1. The wallet exists and is one we collect into. An external wallet
    --    is a payout address; a deposit there opens nothing and recording
    --    one would be a fiction.
    1 / ((SELECT count(*) = 1 FROM wallets
           WHERE id = :wallet AND is_internal)::int)        AS internal_wallet,

    -- 2. It is attached to a vendor and a client. Without both, there is no
    --    pairing to price and reopen-deposit-as-trade would refuse it after
    --    this script had already written the row.
    1 / ((SELECT supplier_id IS NOT NULL AND client_id IS NOT NULL
            FROM wallets WHERE id = :wallet)::int)          AS has_a_pairing,

    -- 3. That vendor can still trade. Recording a deposit against a retired
    --    vendor would create a trade nobody can collect on.
    1 / ((SELECT s.is_active FROM wallets w
            JOIN parties s ON s.id = w.supplier_id
           WHERE w.id = :wallet)::int)                      AS vendor_active,

    -- 4. The hash is new for this wallet. deposits_tx_unique would refuse a
    --    duplicate anyway; this says so before the insert rather than as a
    --    constraint violation.
    1 / ((SELECT count(*) = 0 FROM deposits
           WHERE tx_hash = :hash AND wallet_id = :wallet)::int)
                                                            AS hash_is_new,

    -- 5. A real amount. Above the dust floor the monitor itself applies, so
    --    a hand-entered deposit cannot be something the bot would have
    --    ignored on purpose.
    1 / ((:amount > 0)::int)                                AS amount_positive;

INSERT INTO deposits (
    tx_hash, wallet_id, amount_usdt, from_address, block_number,
    status, confirmed_at
) VALUES (
    :hash, :wallet, :amount, :sender, :block,
    'confirmed', now()
);

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'deposit.recorded_by_hand', 'deposit', d.id,
       jsonb_build_object(
           'tx_hash',     d.tx_hash,
           'wallet_id',   d.wallet_id,
           'amount_usdt', d.amount_usdt,
           'why',         'arrived before the address was being watched; '
                          'adoption filed it as history'
       )
  FROM deposits d
 WHERE d.tx_hash = :hash AND d.wallet_id = :wallet;

COMMIT;

\echo
\echo '--- recorded ---'

SELECT d.id, d.tx_hash, d.amount_usdt, d.trade_id, d.status
  FROM deposits d
 WHERE d.tx_hash = :hash AND d.wallet_id = :wallet;

\echo
\echo 'No trade yet. Price it with:'
\echo '  -v hash="<the same hash>" -f - < deploy/reopen-deposit-as-trade.sql'
\echo 'Then the Bridge issues it with /issue.'
\echo
