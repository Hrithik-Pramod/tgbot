-- UAT ONLY. Inserts one open trade as though a deposit had arrived on-chain.
--
-- Exists so the client-side flow (/add, the paste parser, /done, the summary,
-- the near-completion alert) can be tested without waiting for a real TRC20
-- transfer. It writes exactly what monitor/tron.py would write: a trade in
-- 'awaiting_payment' with the rate snapshotted, and a confirmed deposit row.
--
-- Requires that the supplier, client, internal wallet and rate already exist
-- (set up through /setrate and the wallet commands). Change the constants at
-- the top of the CTE if you want different figures.
--
--   deposit        5,000.00 USDT
--   supply 105.50  ->  inr_expected      527,500.00
--   sell   106.50  ->  usdt_owed_client    4,953.05
--                      margin                 46.95
--
-- Run:  docker compose exec -T db psql -U settlement -d settlement < deploy/simulate-trade.sql
--
-- To remove it afterwards:
--   DELETE FROM deposits WHERE tx_hash LIKE 'simulated_%';
--   DELETE FROM trades   WHERE reference = 'SUPA1';
--   UPDATE supplier_counters SET last_number = last_number - 1
--     WHERE supplier_id = (SELECT id FROM parties WHERE label = 'Supplier A');

BEGIN;

-- Take the next reference the same way the monitor does.
UPDATE supplier_counters SET last_number = last_number + 1
WHERE supplier_id = (SELECT id FROM parties WHERE label = 'Supplier A');

WITH ids AS (
    SELECT s.id AS sup,
           c.id AS cli,
           w.id AS wal,
           r.id AS rate,
           (SELECT prefix || last_number FROM supplier_counters
             WHERE supplier_id = s.id) AS ref
      FROM parties s
      JOIN parties c ON c.label = 'Client A'
      JOIN wallets w ON w.supplier_id = s.id
                    AND w.client_id  = c.id
                    AND w.is_internal
      JOIN LATERAL (
           SELECT id FROM rates
            WHERE supplier_id = s.id AND client_id = c.id
            ORDER BY created_at DESC LIMIT 1
      ) r ON TRUE
     WHERE s.label = 'Supplier A'
)
INSERT INTO trades (reference, supplier_id, client_id, wallet_id, rate_id,
                    supply_rate, sell_rate, usdt_received, inr_expected,
                    usdt_owed_client, margin_usdt, status)
SELECT ref, sup, cli, wal, rate,
       105.50, 106.50, 5000, 527500, 4953.05, 46.95, 'awaiting_payment'
  FROM ids;

INSERT INTO deposits (tx_hash, wallet_id, trade_id, amount_usdt,
                      from_address, block_number, status, confirmed_at)
SELECT 'simulated_test_deposit_0001', t.wallet_id, t.id, 5000,
       'TSimulatedSenderAddressForTesting1', 99999999, 'confirmed', now()
  FROM trades t
 WHERE t.reference = (SELECT prefix || last_number FROM supplier_counters
                       WHERE supplier_id = (SELECT id FROM parties
                                             WHERE label = 'Supplier A'));

COMMIT;

SELECT reference, usdt_received, inr_expected, usdt_owed_client,
       margin_usdt, status
  FROM trades ORDER BY opened_at DESC LIMIT 1;
