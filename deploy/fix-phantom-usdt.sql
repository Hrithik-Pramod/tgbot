-- ===========================================================================
-- Restate a cancelled trade to the deposits actually attached to it.
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 -v ref="SUPA5" \
--     -f - < deploy/fix-phantom-usdt.sql
--
-- WHY
--
-- 14 September 2026, 19:06: 28,302 USDT opened SUPA5. It was never issued and
-- was settled by hand. On the 15th a fresh 37,736 USDT merged into the same
-- trade, taking it to 66,038, and the Bridge cancelled the whole thing —
-- right for the stale half, wrong for the live one. The new deposit was then
-- given its own trade, SUPA6, by deploy/reopen-deposit-as-trade.sql.
--
-- That moved the deposit. It did not move the figures:
--
--     SUPA5  usdt_received 66,038   inr_expected 7,000,028   (1 deposit, 28,302)
--     SUPA6  usdt_received 37,736   inr_expected 4,000,016
--
--     66,038 - 28,302 = 37,736         exactly SUPA6
--     7,000,028 - 3,000,012 = 4,000,016  exactly SUPA6
--
-- So 37,736 USDT and ₹4,000,016 are counted on both trades. /export reads
-- usdt_received and inr_expected straight from the row, and includes
-- cancelled trades — so every export since the 15th has overstated the
-- period by ₹4,000,016.
--
-- WHAT THIS IS NOT
--
-- Not a reprice. The trade's own supply and sell rates are kept and the
-- totals recomputed from the deposits that remain on it, because the fault
-- is a double count, not a price. deploy/reprice-open-trade.sql — now
-- superseded by /reprice — is the tool for a rate that moved.
--
-- WHAT IT REFUSES TO DO
--
--   a live trade        REFUSED. Restating a total the client may be holding
--                       is the 11 September fault. Only a cancelled or
--                       completed trade can be touched here.
--   any payment         REFUSED. Money was logged against these figures.
--   no deposits left    ALLOWED on a cancelled trade — its deposit was
--                       given its own trade and this is the other half of
--                       that move; the figures go to zero. Still refused on
--                       a completed one, which really does need looking at.
--   figures already right  REFUSED, loudly, so a second run cannot quietly
--                       look like it did something.
--
-- Rounding matches core/money: INR to whole rupees, USDT to 2 dp, both half
-- up. Postgres rounds numerics half away from zero, the same thing here.
-- ===========================================================================

BEGIN;

SELECT set_config('fix.ref', :'ref', TRUE);

DO $$
DECLARE
    t          trades%ROWTYPE;
    paid       NUMERIC;
    real_usdt  NUMERIC;
    new_inr    NUMERIC;
    new_owed   NUMERIC;
    new_margin NUMERIC;
BEGIN
    SELECT * INTO t FROM trades WHERE reference = current_setting('fix.ref');
    IF t.id IS NULL THEN
        RAISE EXCEPTION 'no trade called %', current_setting('fix.ref');
    END IF;

    IF t.status IN ('open', 'awaiting_payment') THEN
        RAISE EXCEPTION
            '% is still live. Restating a figure the client may be holding '
            'is not something this will do', t.reference;
    END IF;

    SELECT COALESCE(SUM(amount_inr), 0) INTO paid
    FROM payments WHERE trade_id = t.id;
    IF paid <> 0 THEN
        RAISE EXCEPTION
            '% has % INR logged against the current figures', t.reference, paid;
    END IF;

    SELECT COALESCE(SUM(amount_usdt), 0) INTO real_usdt
    FROM deposits WHERE trade_id = t.id;

    -- Zero is a legitimate answer for a CANCELLED trade: its deposit was
    -- given its own trade and the source was never restated. SUPB3 and
    -- SUPD1 were left exactly like that on 18 September — reopened as SUPB5
    -- and SUPD2 by deploy/reopen-deposit-as-trade.sql, which moves the
    -- deposit and stops there. Both went on claiming 2,354 and 7,000 USDT
    -- they no longer held.
    --
    -- The first version of this script refused that case on the grounds
    -- that it "needs a decision". The decision is not in doubt: a cancelled
    -- trade with nothing attached is owed nothing and owes nothing.
    --
    -- A COMPLETED trade with no deposits is a different animal and still
    -- refused. That one really does need looking at.
    IF real_usdt = 0 AND t.status <> 'cancelled' THEN
        RAISE EXCEPTION
            '% is % and has no deposits attached. That needs a decision, '
            'not this', t.reference, t.status;
    END IF;

    IF real_usdt = t.usdt_received THEN
        RAISE EXCEPTION
            '% already reads % USDT, which is what is attached to it',
            t.reference, real_usdt;
    END IF;

    IF real_usdt = 0 THEN
        new_inr := 0; new_owed := 0; new_margin := 0;
    ELSE
        new_inr    := round(real_usdt * t.supply_rate, 0);
        new_owed   := round(new_inr / t.sell_rate, 2);
        new_margin := round(real_usdt - new_owed, 2);
    END IF;

    UPDATE trades
    SET usdt_received    = real_usdt,
        inr_expected     = new_inr,
        usdt_owed_client = new_owed,
        margin_usdt      = new_margin
    WHERE id = t.id;

    INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
    VALUES (NULL, 'trade.restated', 'trade', t.id, jsonb_build_object(
        'reference',  t.reference,
        'reason',     'usdt_received included a deposit moved to another trade',
        'from_usdt',  t.usdt_received,
        'to_usdt',    real_usdt,
        'from_inr',   t.inr_expected,
        'to_inr',     new_inr,
        'from_owed',  t.usdt_owed_client,
        'to_owed',    new_owed,
        'rates_kept', t.supply_rate || ' / ' || t.sell_rate));

    RAISE NOTICE '% restated: % -> % USDT, % -> % INR',
        t.reference, t.usdt_received, real_usdt, t.inr_expected, new_inr;
END $$;

COMMIT;

-- Verify — the trade and its deposits must now agree.
--   SELECT t.reference, t.status, t.usdt_received,
--          COALESCE(sum(d.amount_usdt), 0) AS deposited, t.inr_expected
--   FROM trades t LEFT JOIN deposits d ON d.trade_id = t.id
--   GROUP BY t.id ORDER BY t.opened_at;
