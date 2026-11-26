-- ===========================================================================
-- Put an introducer's share onto a trade that was opened without one.
--
-- WHY IT HAPPENS
--
--   reopen-deposit-as-trade.sql was written on 15 September 2026. The
--   introducer feature — the buy rate and the three-way split — landed on
--   23 November. That script still prices a trade the way it did in
--   September: supply and sell only. The buy_rate column then defaults to
--   supply (trigger buy_rate_defaults_to_supply), which makes the
--   introducer's share exactly zero and leaves it in the desk's margin.
--
--   26 November 2026, RAJO7: 37,843 USDT priced 105.70 / 107.20 with the
--   rate in force carrying buy 106.20. Written as introducer 0.00, margin
--   529.52. The correct division is introducer 178.17, margin 351.35.
--
--   Nobody is short — the client's figure and the rupees to collect are
--   right either way, because the buy rate only decides how the difference
--   between the supply and sell rates is split between the desk and the
--   introducer. But the introducer is not paid, /earned under-reports, and
--   /comms settles a figure that is wrong.
--
--   The proper fix is in reopen-deposit-as-trade.sql, which should read the
--   buy rate and call the same division as a live deposit. Until it does,
--   this repairs the result.
--
-- THE FIGURES COME FROM core.money, NOT FROM SQL
--
--   Deliberately parameters rather than arithmetic in this file. The split
--   is remainder-based so the three shares always sum to the deposit
--   exactly, and re-implementing that here would give two definitions of
--   the same division that agree until a rounding boundary. Compute them
--   with the code that does it everywhere else:
--
--     python -c "
--     from decimal import Decimal as D
--     from core.money import split_three_ways
--     print(split_three_ways(D('<usdt>'), D('<supply>'), D('<buy>'), D('<sell>')))"
--
--   Then pass introducer and margin in. The guard below checks they sum
--   back to the deposit, so a mistyped figure cannot be committed.
--
-- USAGE
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v ref="'REF0'" \
--     -v buy=0.000000 \
--     -v introducer=0.00 \
--     -v margin=0.00 \
--     -f - < deploy/fix-introducer-share.sql
--
--   ref is quoted inside the value. The rest are bare. Nothing after a
--   value on a -v line.
--
-- ONLY BEFORE COLLECTION STARTS
--
--   Refused once a payment exists. Changing the division under a trade the
--   vendor is already paying into means /progress, the vendor's statement
--   and the commission report disagree about the same deal while it is
--   live. After that point it is a settlement conversation, not an UPDATE.
-- ===========================================================================

\echo
\echo '--- before ---'

SELECT t.id, t.reference, t.status, t.usdt_received,
       t.supply_rate, t.buy_rate, t.sell_rate,
       t.inr_expected, t.usdt_owed_client,
       t.introducer_usdt, t.margin_usdt,
       i.label AS introducer,
       (SELECT count(*) FROM payments p WHERE p.trade_id = t.id) AS payments
  FROM trades t
  JOIN parties s ON s.id = t.supplier_id
  LEFT JOIN parties i ON i.id = s.introducer_id
 WHERE t.reference = :ref;

BEGIN;

SELECT
    -- 1. One trade with that reference.
    1 / ((SELECT count(*) = 1 FROM trades WHERE reference = :ref)::int)
                                                        AS trade_exists,

    -- 2. Its vendor actually has an introducer. Without one there is no
    --    share to assign and this would simply be moving the desk's money
    --    to nobody.
    1 / ((SELECT s.introducer_id IS NOT NULL
            FROM trades t JOIN parties s ON s.id = t.supplier_id
           WHERE t.reference = :ref)::int)               AS has_an_introducer,

    -- 3. Nothing collected yet. See the note above: after the first payment
    --    this stops being a correction and becomes a renegotiation.
    1 / ((SELECT count(*) = 0 FROM payments p
            JOIN trades t ON t.id = p.trade_id
           WHERE t.reference = :ref)::int)               AS nothing_collected,

    -- 4. Still live. A completed or cancelled trade is history.
    1 / ((SELECT status IN ('open', 'awaiting_payment')
            FROM trades WHERE reference = :ref)::int)    AS still_live,

    -- 5. The share is currently zero. If it is not, this trade was priced
    --    correctly and something else is being fixed — stop and look.
    1 / ((SELECT coalesce(introducer_usdt, 0) = 0
            FROM trades WHERE reference = :ref)::int)    AS share_is_unset,

    -- 6. THE ARITHMETIC CHECK. The three shares must account for the
    --    deposit to the cent. This is what makes passing figures in safe:
    --    a mistyped introducer or margin cannot be committed.
    1 / ((SELECT usdt_owed_client + :introducer + :margin = usdt_received
            FROM trades WHERE reference = :ref)::int)    AS shares_sum,

    -- 7. The buy rate sits between supply and sell. Below supply the desk
    --    would be paying the introducer out of the client's money; above
    --    sell there is nothing to divide.
    1 / ((SELECT :buy >= supply_rate AND :buy <= sell_rate
            FROM trades WHERE reference = :ref)::int)    AS buy_rate_in_range;

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'trade.introducer_share_fixed', 'trade', t.id,
       jsonb_build_object(
           'reference',       t.reference,
           'buy_rate_was',    t.buy_rate,
           'buy_rate_now',    :buy::numeric,
           'introducer_was',  t.introducer_usdt,
           'introducer_now',  :introducer::numeric,
           'margin_was',      t.margin_usdt,
           'margin_now',      :margin::numeric,
           'why',             'opened by reopen-deposit-as-trade, which '
                              'predates the three-way split'
       )
  FROM trades t
 WHERE t.reference = :ref;

UPDATE trades
   SET buy_rate        = :buy,
       introducer_usdt = :introducer,
       margin_usdt     = :margin
 WHERE reference = :ref;

COMMIT;

\echo
\echo '--- after ---'

SELECT t.reference, t.usdt_received, t.supply_rate, t.buy_rate, t.sell_rate,
       t.usdt_owed_client, t.introducer_usdt, t.margin_usdt,
       t.usdt_owed_client + t.introducer_usdt + t.margin_usdt AS accounted_for
  FROM trades t
 WHERE t.reference = :ref;

\echo
\echo 'The introducer sees it on /earned and /progress immediately.'
\echo 'Nothing the client or the vendor sees has changed.'
\echo
