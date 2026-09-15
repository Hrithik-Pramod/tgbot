-- ===========================================================================
-- Give a deposit its own trade, at the rate in force now.
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 -v hash="fb578a530c185966b608e1dfc2287c617c19d537d76119c88f9ab0450663d3f9" \
--     -f - < deploy/reopen-deposit-as-trade.sql
--
-- WHY
--
-- 15 September 2026. SUPA5 had been open since the 12th, never issued, and
-- already settled by hand. A fresh 37,736 USDT merged into it, so the trade
-- read 66,038 and the Bridge cancelled the whole thing — which was right for
-- the stale half and wrong for the new one. The live deposit went with it:
--
--     can you connect, the deal in progress with the one we couldind load
--     and confirm rates are pick up correctly
--
-- So the deposit needs a trade of its own, priced at the rate now in force
-- rather than the one attached to the trade it was cancelled inside.
--
-- WHAT IT REFUSES TO DO
--
--   a deposit still on a live trade      REFUSED. It already has one, and
--                                        moving it would strip a trade the
--                                        Bridge may have instructed.
--   a pairing with no rate               REFUSED. Nothing to price it at.
--
-- The reference comes from the supplier's counter, so it continues the
-- sequence rather than reusing the cancelled trade's number. Rounding
-- matches core/money: INR to whole rupees, USDT to 2 dp, both half up.
--
-- announced_at is stamped, because the Bridge is being told by hand. He
-- issues it with /issue; leaving it null would have the monitor post a
-- notification saying the supplier had not entered details, which is not
-- what happened here.
-- ===========================================================================

BEGIN;

SELECT set_config('reopen.hash', :'hash', TRUE);

DO $$
DECLARE
    d          deposits%ROWTYPE;
    w          wallets%ROWTYPE;
    r          rates%ROWTYPE;
    old_status trade_status;
    ref        TEXT;
    new_id     BIGINT;
    new_inr    NUMERIC;
    new_owed   NUMERIC;
    new_margin NUMERIC;
BEGIN
    SELECT * INTO d FROM deposits WHERE tx_hash = current_setting('reopen.hash');
    IF d.id IS NULL THEN
        RAISE EXCEPTION 'no deposit with that hash';
    END IF;

    IF d.trade_id IS NOT NULL THEN
        SELECT status INTO old_status FROM trades WHERE id = d.trade_id;
        IF old_status NOT IN ('cancelled') THEN
            RAISE EXCEPTION
                'that deposit is on a % trade — it already has one', old_status;
        END IF;
    END IF;

    SELECT * INTO w FROM wallets WHERE id = d.wallet_id;
    IF NOT w.is_internal THEN
        RAISE EXCEPTION 'that address is not an internal wallet';
    END IF;

    SELECT * INTO r FROM rates
    WHERE supplier_id = w.supplier_id AND client_id = w.client_id
    ORDER BY created_at DESC LIMIT 1;
    IF r.id IS NULL THEN
        RAISE EXCEPTION 'no rate set for this pairing';
    END IF;

    UPDATE supplier_counters SET last_number = last_number + 1
    WHERE supplier_id = w.supplier_id
    RETURNING prefix || last_number INTO ref;
    IF ref IS NULL THEN
        RAISE EXCEPTION 'no deal counter for this supplier';
    END IF;

    new_inr    := round(d.amount_usdt * r.supply_rate, 0);
    new_owed   := round(new_inr / r.sell_rate, 2);
    new_margin := round(d.amount_usdt - new_owed, 2);

    INSERT INTO trades (
        reference, supplier_id, client_id, wallet_id, rate_id,
        supply_rate, sell_rate, usdt_received, inr_expected,
        usdt_owed_client, margin_usdt, status, opened_at, announced_at
    ) VALUES (
        ref, w.supplier_id, w.client_id, w.id, r.id,
        r.supply_rate, r.sell_rate, d.amount_usdt, new_inr,
        new_owed, new_margin, 'awaiting_payment', d.detected_at, now()
    ) RETURNING id INTO new_id;

    UPDATE deposits SET trade_id = new_id WHERE id = d.id;

    INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
    VALUES (NULL, 'trade.reopened_from_deposit', 'trade', new_id,
        jsonb_build_object(
            'reference', ref,
            'usdt', d.amount_usdt,
            'rates', r.supply_rate || ' / ' || r.sell_rate,
            'inr_expected', new_inr,
            'usdt_owed', new_owed,
            'from_cancelled_trade', d.trade_id,
            'tx_hash', d.tx_hash));

    RAISE NOTICE '% opened: % USDT at % / % -> INR %, send on % USDT',
        ref, d.amount_usdt, r.supply_rate, r.sell_rate, new_inr, new_owed;
END $$;

COMMIT;

-- Verify.
--   SELECT reference, status, usdt_received, supply_rate, sell_rate,
--          inr_expected, usdt_owed_client, margin_usdt
--   FROM trades WHERE status IN ('open','awaiting_payment');
