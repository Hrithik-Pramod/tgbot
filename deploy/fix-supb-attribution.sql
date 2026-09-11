-- ===========================================================================
-- One-off repair: move ₹318,000 from SUPB1 to SUPB2, where it belongs.
--
-- RUN WITH THE BOT STOPPED.
--
-- WHAT HAPPENED
--
-- The Bridge settled by hand during the outage and told the client to pay the
-- whole 515,054. They paid it in one message across two accounts:
--
--     625417401220            197,054   Girish Kumar Ahirwar
--     BKIDR12026091100007565  200,000   Wasim Salim Shaikh
--     625417402166            118,000   Wasim Salim Shaikh
--
-- A payment can only be attributed to a trade that has been INSTRUCTED.
-- SUPB1 had been (197,054); SUPB2 had not. So all three attached to SUPB1,
-- which closed overpaid by 318,000, while SUPB2 — expecting exactly 318,000 —
-- showed nothing received.
--
-- WHY THIS IS SAFE
--
-- Nothing is created or destroyed. The same three payment rows keep their
-- UTRs, amounts and beneficiary accounts; two of them change which trade they
-- point at. And the division is exact:
--
--     SUPB1   197,054   expects 197,054
--     SUPB2   318,000   expects 318,000   (200,000 + 118,000)
--
-- Both then close on their own figures, which is what should have happened.
--
-- NOT FIXED HERE
--
-- The attribution rule itself. Until a payment can overflow from a filled
-- trade into the next one, money arriving for a trade that has not been
-- instructed will keep landing on the previous one. The operational cover
-- until then is to issue the instruction before the client pays.
-- ===========================================================================

BEGIN;

DO $$
DECLARE
    b1_total NUMERIC;
    b2_total NUMERIC;
BEGIN
    SELECT COALESCE(SUM(p.amount_inr), 0) INTO b1_total
    FROM payments p JOIN trades t ON t.id = p.trade_id WHERE t.reference = 'SUPB1';

    SELECT COALESCE(SUM(p.amount_inr), 0) INTO b2_total
    FROM payments p JOIN trades t ON t.id = p.trade_id WHERE t.reference = 'SUPB2';

    IF b1_total <> 515054 THEN
        RAISE EXCEPTION 'SUPB1 holds % INR, expected 515054 — situation has moved', b1_total;
    END IF;
    IF b2_total <> 0 THEN
        RAISE EXCEPTION 'SUPB2 already holds % INR — do not run this twice', b2_total;
    END IF;
END $$;

-- The two that were never SUPB1's.
UPDATE payments
SET trade_id = (SELECT id FROM trades WHERE reference = 'SUPB2')
WHERE utr IN ('BKIDR12026091100007565', '625417402166');

-- SUPB2 is now paid in full, so it is closed on its own figures rather than
-- left open against money it already has.
UPDATE trades
SET status = 'completed', completed_at = now()
WHERE reference = 'SUPB2';

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'payment.reattributed', 'trade', id,
       jsonb_build_object(
           'reason', 'paid against a trade that had not been instructed',
           'moved',  '200000 + 118000 = 318000 from SUPB1 to SUPB2',
           'result', 'SUPB1 197054 of 197054, SUPB2 318000 of 318000')
FROM trades WHERE reference IN ('SUPB1', 'SUPB2');

COMMIT;

-- Verify. Both trades must be completed and exact, with nothing over or short.
--   SELECT t.reference, t.status, t.inr_expected,
--          COALESCE(SUM(p.amount_inr),0) AS paid,
--          COALESCE(SUM(p.amount_inr),0) - t.inr_expected AS difference
--   FROM trades t LEFT JOIN payments p ON p.trade_id = t.id
--   GROUP BY t.id, t.reference, t.status, t.inr_expected ORDER BY t.id;
