-- ===========================================================================
-- One-off repair: split SUPB1 back into the two trades it should have been.
--
-- RUN ONLY AFTER migrate-003-instructed.sql, AND ONLY WITH THE BOT STOPPED.
--
-- WHAT HAPPENED
--
--   16:34:30  1,859 USDT   SUPB1 opens          INR 197,054
--   16:37:39  instruction issued to client      INR 197,054
--   16:47:29  3,000 USDT   merged into SUPB1    INR 515,054
--
-- The client holds an instruction for 197,054. The ledger expects 515,054.
-- The trade cannot close, and the Bridge cannot issue against it without
-- either re-instructing the whole thing or leaving 318,000 unaccounted.
--
-- WHY THIS IS SAFE
--
-- The client has paid NOTHING against SUPB1, so no payment has to be
-- re-attributed — the hard and dangerous part of a split does not arise.
--
-- And the arithmetic divides exactly, with no rounding drift:
--
--     1,859 x 106     = 197,054      197,054 / 107.2 = 1,838.19
--     3,000 x 106     = 318,000      318,000 / 107.2 = 2,966.42
--     ---------------------------    ----------------------------
--     4,859             515,054                        4,804.61
--
-- 4,804.61 and the 54.39 margin are exactly what the SUPB1 row holds today.
-- Nothing is lost and nothing is invented; the same totals are simply carried
-- by two rows instead of one.
--
-- AFTERWARDS
--
--   SUPB1  1,859 USDT, INR 197,054, instructed  -> the live instruction stands
--   SUPB2  3,000 USDT, INR 318,000, uninstructed -> Bridge issues with /issue
--
-- The deal counter is advanced so the next automatic trade is SUPB3 and not a
-- second SUPB2, which would fail the reference uniqueness constraint at the
-- worst possible moment — on the next real deposit.
-- ===========================================================================

BEGIN;

-- Refuse to run if the premise has changed. A payment arriving between reading
-- this and running it would make the split a different, unsafe operation.
DO $$
DECLARE
    paid NUMERIC;
    got  NUMERIC;
BEGIN
    SELECT COALESCE(SUM(p.amount_inr), 0) INTO paid
    FROM payments p JOIN trades t ON t.id = p.trade_id
    WHERE t.reference = 'SUPB1';

    SELECT usdt_received INTO got FROM trades WHERE reference = 'SUPB1';

    IF paid <> 0 THEN
        RAISE EXCEPTION
            'SUPB1 now has % INR in payments — do not split, re-attribution needed', paid;
    END IF;

    IF got <> 4859 THEN
        RAISE EXCEPTION
            'SUPB1 holds % USDT, expected 4859 — the situation has moved on', got;
    END IF;
END $$;

-- The 3,000 becomes its own trade, inheriting everything that identifies the
-- pairing and the pricing from the trade it is being carved out of.
INSERT INTO trades (
    reference, supplier_id, client_id, wallet_id, rate_id,
    supply_rate, sell_rate,
    usdt_received, inr_expected, usdt_owed_client, margin_usdt,
    nominated_account_id, status, opened_at
)
SELECT
    'SUPB2', supplier_id, client_id, wallet_id, rate_id,
    supply_rate, sell_rate,
    3000, 318000, 2966.42, 33.58,
    nominated_account_id, 'awaiting_payment',
    -- the moment the 3,000 actually landed, not now
    (SELECT detected_at FROM deposits WHERE amount_usdt = 3000 AND trade_id IS NOT NULL
     ORDER BY detected_at DESC LIMIT 1)
FROM trades WHERE reference = 'SUPB1';

-- SUPB1 goes back to being the trade the client was actually instructed on.
UPDATE trades
SET usdt_received    = 1859,
    inr_expected     = 197054,
    usdt_owed_client = 1838.19,
    margin_usdt      = 20.81
WHERE reference = 'SUPB1';

-- The deposit follows its trade, or the ledger disagrees with itself and the
-- healthcheck's "no deposit is missing its trade" stops meaning anything.
UPDATE deposits
SET trade_id = (SELECT id FROM trades WHERE reference = 'SUPB2')
WHERE tx_hash = 'ecbfd826744990c7711c0eea969563cb80bd94ac32ebce1e1e7ede158245f4e3';

-- Next automatic trade for this supplier must be SUPB3.
UPDATE supplier_counters
SET last_number = GREATEST(last_number, 2)
WHERE supplier_id = (SELECT supplier_id FROM trades WHERE reference = 'SUPB1');

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'trade.split_repair', 'trade', id,
       jsonb_build_object(
           'reason', 'deposit merged into an already-instructed trade',
           'from',   'SUPB1 4859 USDT / 515054 INR',
           'into',   'SUPB1 1859 / 197054 and SUPB2 3000 / 318000')
FROM trades WHERE reference IN ('SUPB1', 'SUPB2');

COMMIT;

-- Verify. Expect 1859/197054 instructed, and 3000/318000 not instructed.
--   SELECT reference, status, usdt_received, inr_expected, usdt_owed_client,
--          margin_usdt, instructed_at
--   FROM trades WHERE reference LIKE 'SUPB%' ORDER BY id;
