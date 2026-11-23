-- ===========================================================================
-- The introducer: a fourth kind of party, and the vendor they introduced.
--
--     the introducer of this vendor, wants a collection of 1 inr on the rate
--     the vendor supplies ... the Introducer needs visability, so a separate
--     group they can use a bot to see trade deposits, and check progress
--     — Bridge, 23 November 2026
--
--     1 per vendor
--     — Bridge, asked whether an introducer could cover several
--
-- WHY A ROLE AND NOT A FLAG
--
-- party_role has been exactly bridge/supplier/client since the schema was
-- written, with a comment saying roles are mutually exclusive and enforced
-- by one column rather than a join. An introducer is a counterparty with
-- their own Telegram group, their own bot, and their own view of the book —
-- the same shape as the other three, and nothing like a flag on a supplier.
--
-- Adding a value to an enum cannot be undone in Postgres without rewriting
-- the type, so this is worth being sure about. It is: every other model
-- considered (a boolean, a separate table keyed on party) ends up needing
-- the role anyway, because the middleware resolves who is speaking from the
-- chat id and has to answer "what are you" in one lookup.
--
-- WHY THE LINK LIVES ON THE SUPPLIER ROW
--
-- One introducer per vendor, so the vendor row names its introducer. That
-- makes "one per vendor" a property of the schema rather than a rule
-- somebody has to enforce, and it makes the lookup — which this is on every
-- deposit — a single column read.
--
-- It does NOT stop one introducer covering two vendors later: two supplier
-- rows may point at the same introducer. That was not asked for, and it
-- costs nothing to leave possible.
-- ===========================================================================

BEGIN;

-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block in older
-- Postgres, and is allowed from 12 onwards as long as the new value is not
-- used in the same transaction. It is not: the UPDATE below touches no
-- role column.
ALTER TYPE party_role ADD VALUE IF NOT EXISTS 'introducer';

-- Which introducer brought this vendor. NULL for every existing vendor and
-- for every vendor that simply does not have one, which is all of them
-- today.
ALTER TABLE parties
    ADD COLUMN IF NOT EXISTS introducer_id BIGINT REFERENCES parties(id);

COMMIT;

-- Separate transaction: the constraint reads the enum value added above,
-- which is not visible to the transaction that added it.
BEGIN;

-- HALF OF THIS IS A CHECK AND HALF HAS TO BE A TRIGGER.
--
-- Two things must hold:
--
--   only a supplier may carry an introducer      about THIS row
--   the party named must BE an introducer        about ANOTHER row
--
-- A CHECK constraint can only see the row being written, so it can state
-- the first and not the second. Writing both into a CHECK would look right
-- and silently enforce only half — and the half it dropped is the one that
-- matters, because without it any party can be named, including the Bridge
-- itself, which would route commission quietly to the desk.
ALTER TABLE parties DROP CONSTRAINT IF EXISTS parties_introducer_shape;
ALTER TABLE parties ADD CONSTRAINT parties_introducer_shape
    CHECK (introducer_id IS NULL OR role = 'supplier');

CREATE OR REPLACE FUNCTION introducer_must_be_an_introducer()
RETURNS trigger AS $$
DECLARE
    named_role party_role;
BEGIN
    IF NEW.introducer_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.introducer_id = NEW.id THEN
        RAISE EXCEPTION 'a vendor cannot introduce itself';
    END IF;
    SELECT role INTO named_role FROM parties WHERE id = NEW.introducer_id;
    IF named_role IS DISTINCT FROM 'introducer' THEN
        RAISE EXCEPTION
            'party % is %, not an introducer', NEW.introducer_id, named_role;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS parties_introducer_is_an_introducer ON parties;
CREATE TRIGGER parties_introducer_is_an_introducer
    BEFORE INSERT OR UPDATE OF introducer_id ON parties
    FOR EACH ROW EXECUTE FUNCTION introducer_must_be_an_introducer();

CREATE INDEX IF NOT EXISTS parties_introducer_idx
    ON parties (introducer_id) WHERE introducer_id IS NOT NULL;

COMMIT;

-- Check:
--
--   SELECT role, count(*) FROM parties GROUP BY role ORDER BY role;
--   SELECT count(*) AS vendors_with_an_introducer
--     FROM parties WHERE introducer_id IS NOT NULL;
--
-- Before any introducer is registered the second must be 0, and the first
-- must show no 'introducer' rows.
