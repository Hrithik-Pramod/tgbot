-- ===========================================================================
-- Re-price an open trade at the rate currently in force.
--
-- SUPERSEDED, 16 September 2026. Use /reprice in the Bridge chat instead: it
-- applies the same guards, shows both figures before anything moves, and
-- re-checks under a row lock. This file stays for the record and for a trade
-- that somehow cannot be reached from the bot.
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 -v ref="SUPA5" \
--     -f - < deploy/reprice-open-trade.sql
--
-- WHY THIS EXISTS
--
-- A trade snapshots the rate when it opens, and that is deliberate: reading
-- the rate through a join would let a later /setrate silently rewrite the
-- arithmetic of trades already agreed. The cost is that a rate which moves
-- between the deposit landing and the instruction going out leaves the trade
-- priced on the old one, with no way to correct it.
--
-- Bridge, 14 September 2026:
--
--     the rate has changed but the funds have come
--     Can i have the option to change the rate and it reflect
--     Apologies this was one off, but as it happened, it could always happen
--
-- WHEN IT IS ALLOWED, AND WHY THE GUARDS ARE NOT NEGOTIABLE
--
--   not instructed   safe. Nothing has been sent to the client, so the only
--                    thing changing is a figure nobody outside has seen.
--
--   instructed       REFUSED. The client is holding a message saying pay a
--                    specific amount. Moving the total underneath it is
--                    exactly what happened on 11 September, when a trade
--                    instructed at 197,054 quietly became 515,054.
--
--   any payment      REFUSED. Money already received was priced at the old
--                    rate. Re-pricing the whole trade would restate what has
--                    already been settled.
--
-- The rate is read from the rates table rather than passed in, so the figure
-- applied is the one the Bridge set through /setrate — which shows the
-- current rate, warns when the sell rate is not above the supply rate, and
-- records who set it. Passing a rate here by hand would bypass all three.
--
-- Rounding matches core/money exactly: INR to whole rupees, USDT to 2 dp,
-- both half up. Postgres rounds numerics half away from zero, which is the
-- same thing for positive amounts.
-- ===========================================================================

BEGIN;

SELECT set_config('reprice.ref', :'ref', TRUE);

DO $$
DECLARE
    t          trades%ROWTYPE;
    r          rates%ROWTYPE;
    paid       NUMERIC;
    new_inr    NUMERIC;
    new_owed   NUMERIC;
    new_margin NUMERIC;
BEGIN
    SELECT * INTO t FROM trades WHERE reference = current_setting('reprice.ref');
    IF t.id IS NULL THEN
        RAISE EXCEPTION 'no trade called %', current_setting('reprice.ref');
    END IF;

    IF t.status NOT IN ('open', 'awaiting_payment') THEN
        RAISE EXCEPTION '% is %, not open', t.reference, t.status;
    END IF;

    IF t.instructed_at IS NOT NULL THEN
        RAISE EXCEPTION
            '% was instructed at % — the client holds a figure. Cancel and '
            're-issue instead of re-pricing underneath them',
            t.reference, t.instructed_at;
    END IF;

    SELECT COALESCE(SUM(amount_inr), 0) INTO paid
    FROM payments WHERE trade_id = t.id;
    IF paid <> 0 THEN
        RAISE EXCEPTION '% already has % INR paid against it', t.reference, paid;
    END IF;

    SELECT * INTO r FROM rates
    WHERE supplier_id = t.supplier_id AND client_id = t.client_id
    ORDER BY created_at DESC LIMIT 1;
    IF r.id IS NULL THEN
        RAISE EXCEPTION 'no rate set for this pairing';
    END IF;

    IF r.id = t.rate_id THEN
        RAISE EXCEPTION
            '% is already on the newest rate (% / %) — set the new one with '
            '/setrate first', t.reference, r.supply_rate, r.sell_rate;
    END IF;

    new_inr    := round(t.usdt_received * r.supply_rate, 0);
    new_owed   := round(new_inr / r.sell_rate, 2);
    new_margin := round(t.usdt_received - new_owed, 2);

    UPDATE trades
    SET rate_id          = r.id,
        supply_rate      = r.supply_rate,
        sell_rate        = r.sell_rate,
        inr_expected     = new_inr,
        usdt_owed_client = new_owed,
        margin_usdt      = new_margin
    WHERE id = t.id;

    INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
    VALUES (NULL, 'trade.repriced', 'trade', t.id, jsonb_build_object(
        'reference',  t.reference,
        'usdt',       t.usdt_received,
        'from_rates', t.supply_rate || ' / ' || t.sell_rate,
        'to_rates',   r.supply_rate || ' / ' || r.sell_rate,
        'from_inr',   t.inr_expected,
        'to_inr',     new_inr,
        'from_owed',  t.usdt_owed_client,
        'to_owed',    new_owed));

    RAISE NOTICE '% repriced: % -> % INR, owed % -> % USDT',
        t.reference, t.inr_expected, new_inr, t.usdt_owed_client, new_owed;
END $$;

COMMIT;

-- Verify.
--   SELECT reference, usdt_received, supply_rate, sell_rate, inr_expected,
--          usdt_owed_client, margin_usdt
--   FROM trades WHERE status IN ('open','awaiting_payment');
