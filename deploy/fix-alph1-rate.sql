-- Correct ALPH1 to the rate the deal was actually done at.
--
-- WHAT HAPPENED
--
-- 21 Sep 17:47  The client is instructed to pay Rs499,990 for 4,708 USDT.
--               That is 4708 x 106.2, Malegao's rate, on SUPB7.
-- 22 Sep 00:49  SUPB7 is cancelled and the deposit reopened under Tata
--               Mahalaxmi as ALPH1. reopen_deposit_as_trade prices at "the
--               rate in force", which for Tata is 106.4, so ALPH1 expects
--               Rs500,931.
-- 22 Sep        The client pays Rs499,990 - the figure they were holding.
--
-- ALPH1 therefore reads Rs941 outstanding on a deal that is finished.
--
-- WHY 106.2 IS THE TRUE RATE AND NOT A CONVENIENCE
--
-- The chain settles it on both sides, not just the rupee side:
--
--   client paid      Rs499,990     = 4708 x 106.2      (106.4 -> Rs500,931)
--   client received  4,642.43 USDT = 499,990 / 107.7   (106.4 -> 4,651.17)
--
-- Deposit 477273 is that 4,642.43 USDT, confirmed on the payout wallet. The
-- money moved at 106.2 in both directions before ALPH1 ever existed. The
-- 106.4 on the trade is a figure no party transacted at.
--
-- So this does not forgive Rs941. It records the price the deal was struck,
-- invoiced and settled at, and the balance goes to zero because there never
-- was one.
--
-- WHY NOT /reprice
--
-- reprice_trade refuses a trade with payments against it, and should: moving
-- the expected figure under a client who has already paid against it is the
-- 11 September SUPB1 failure. This is the opposite case - the payment is
-- correct and the expectation is wrong - which the command cannot express.
-- Hence a script, reviewed, rather than a weakened guard.
--
-- Read-only until you remove the ROLLBACK at the end.

BEGIN;

\echo '=== before ==='
SELECT t.reference, t.status, t.supply_rate, t.sell_rate,
       t.inr_expected, t.usdt_owed_client, t.margin_usdt,
       COALESCE((SELECT sum(p.amount_inr) FROM payments p
                 WHERE p.trade_id = t.id), 0) AS paid
FROM trades t WHERE t.reference = 'ALPH1';

-- Refuse to run against anything but the exact state described above.
-- A guard, not decoration: if this file is ever re-run after the fact, or
-- run against a trade that has moved on, it must do nothing.
DO $$
DECLARE
    v_id     BIGINT;
    v_usdt   NUMERIC;
    v_paid   NUMERIC;
    v_status TEXT;
BEGIN
    SELECT t.id, t.usdt_received, t.status,
           COALESCE((SELECT sum(p.amount_inr) FROM payments p
                     WHERE p.trade_id = t.id), 0)
      INTO v_id, v_usdt, v_status, v_paid
      FROM trades t WHERE t.reference = 'ALPH1';

    IF v_id IS NULL THEN
        RAISE EXCEPTION 'ALPH1 does not exist';
    END IF;
    IF v_status <> 'awaiting_payment' THEN
        RAISE EXCEPTION 'ALPH1 is %, expected awaiting_payment', v_status;
    END IF;
    IF v_usdt <> 4708 THEN
        RAISE EXCEPTION 'ALPH1 holds % USDT, expected 4708', v_usdt;
    END IF;
    IF v_paid <> 499990 THEN
        RAISE EXCEPTION 'ALPH1 has Rs% paid, expected 499990', v_paid;
    END IF;
END $$;

UPDATE trades SET
    supply_rate      = 106.200000,
    sell_rate        = 107.700000,
    inr_expected     = 499990.00,    -- 4708 x 106.2, rounded half up
    usdt_owed_client = 4642.430000,  -- 499990 / 107.7, and the amount that
                                     -- actually left on deposit 477273
    margin_usdt      = 65.570000,    -- 4708 - 4642.43
    status           = 'completed',
    completed_at     = now()
WHERE reference = 'ALPH1';

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'trade.rate_corrected', 'trade', t.id,
       jsonb_build_object(
           'reference',    'ALPH1',
           'from_supply',  '106.400000',
           'to_supply',    '106.200000',
           'from_inr',     '500931.00',
           'to_inr',       '499990.00',
           'from_owed',    '4651.170000',
           'to_owed',      '4642.430000',
           'why',          'priced at the rate in force when reopened from '
                           'SUPB7, but the client was already instructed at '
                           '106.2, paid Rs499,990 and was settled 4,642.43 '
                           'USDT on deposit 477273. Corrected to the rate '
                           'the deal was actually done at.',
           'authorised_by', 'Bridge, 22 September 2026'
       )
FROM trades t WHERE t.reference = 'ALPH1';

\echo ''
\echo '=== after ==='
SELECT t.reference, t.status, t.supply_rate, t.sell_rate,
       t.inr_expected, t.usdt_owed_client, t.margin_usdt,
       COALESCE((SELECT sum(p.amount_inr) FROM payments p
                 WHERE p.trade_id = t.id), 0) AS paid,
       t.inr_expected - COALESCE((SELECT sum(p.amount_inr) FROM payments p
                 WHERE p.trade_id = t.id), 0) AS outstanding
FROM trades t WHERE t.reference = 'ALPH1';

-- Dry run by default. Check the "after" block reads Rs499,990 expected,
-- Rs499,990 paid, 0 outstanding, status completed. Then change this to
-- COMMIT and run it again.
ROLLBACK;
