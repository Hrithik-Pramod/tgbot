-- ===========================================================================
-- Onboard one supplier: party, deal counter, internal wallet, rate.
--
-- USAGE — every value is supplied on the command line, nothing is hardcoded:
--
--   docker compose exec -T db psql -U settlement -d settlement \
--     -v ON_ERROR_STOP=1 \
--     -v label="'Supplier C'" \
--     -v chat_id=-1003992458205 \
--     -v prefix="'SUPC'" \
--     -v wallet="'T...'" \
--     -v client="'Client A'" \
--     -v supply=106 \
--     -v sell=107.2 \
--     -f - < deploy/seed-supplier.sql
--
-- WHAT EACH PIECE IS FOR
--
--   party            the group. telegram_chat_id is the security boundary:
--                    whoever the Bridge adds to that group may act as this
--                    supplier, and no other chat can.
--
--   supplier_counter the SUPC1, SUPC2 ... sequence. Without a row here
--                    next_reference raises and the FIRST deposit fails.
--
--   wallet           one internal wallet per supplier→client pairing. This is
--                    the routing key — a deposit is attributed by which
--                    wallet received it, so two suppliers sharing an address
--                    would be indistinguishable. The schema enforces it.
--
--   rate             a deposit with no rate for its pairing opens no trade;
--                    the Bridge is told and the money waits. So this is not
--                    optional either.
--
-- The wallet is NOT adopted here. The monitor adopts it on its first poll and
-- records adopted_at_ms, so everything already on that address is treated as
-- history and never reported as a deposit. That is deliberate: seeding an
-- adoption timestamp by hand is how 38 old transfers were once ingested as
-- live deposits.
--
-- Safe to re-run: every insert is guarded, so a second run changes nothing.
-- ===========================================================================

BEGIN;

-- The client must already exist; a typo here would otherwise create a pairing
-- against nothing and fail confusingly later, at the first deposit.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM parties WHERE label = :'client' AND role = 'client') THEN
        RAISE EXCEPTION 'no client called % — check the label', :'client';
    END IF;
END $$;

INSERT INTO parties (role, label, telegram_chat_id, is_active)
VALUES ('supplier', :'label', :chat_id, TRUE)
ON CONFLICT (telegram_chat_id) DO NOTHING;

INSERT INTO supplier_counters (supplier_id, prefix, last_number)
SELECT id, :'prefix', 0 FROM parties WHERE label = :'label'
ON CONFLICT (supplier_id) DO NOTHING;

INSERT INTO wallets (address, is_internal, supplier_id, client_id, label, is_monitored)
SELECT :'wallet', TRUE, s.id, c.id,
       :'label' || ' → ' || :'client', TRUE
FROM parties s, parties c
WHERE s.label = :'label' AND c.label = :'client'
ON CONFLICT (address) DO NOTHING;

INSERT INTO rates (supplier_id, client_id, supply_rate, sell_rate, set_by)
SELECT s.id, c.id, :supply, :sell, b.id
FROM parties s, parties c, parties b
WHERE s.label = :'label' AND c.label = :'client' AND b.role = 'bridge';

INSERT INTO audit_log (actor_party_id, action, entity_type, entity_id, detail)
SELECT NULL, 'supplier.onboarded', 'party', id,
       jsonb_build_object('label', :'label', 'client', :'client',
                          'prefix', :'prefix')
FROM parties WHERE label = :'label';

COMMIT;

-- Verify. The supplier should appear with a counter, a monitored wallet and a
-- rate — all four, or the first deposit will fail in a different way for each
-- thing that is missing.
--
--   SELECT p.label, p.telegram_chat_id, sc.prefix, w.address, w.is_monitored,
--          r.supply_rate, r.sell_rate
--   FROM parties p
--   LEFT JOIN supplier_counters sc ON sc.supplier_id = p.id
--   LEFT JOIN wallets w ON w.supplier_id = p.id AND w.is_internal
--   LEFT JOIN LATERAL (SELECT * FROM rates WHERE supplier_id = p.id
--                      ORDER BY created_at DESC LIMIT 1) r ON TRUE
--   WHERE p.role = 'supplier' ORDER BY p.id;
