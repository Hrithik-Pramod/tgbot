-- 013 — a payment the bot cannot place is written down, whoever is being asked.
--
-- WHY
--
-- held_payments was built on 1 October for one case: two vendors collecting
-- into one account, where the question goes to the Bridge. That case has not
-- lost a rupee since, because the money is on the ledger from the moment it
-- is read and the Bridge is chased until he answers.
--
-- Every OTHER unplaceable payment still waited in memory. The bot asked the
-- client which account, and until they tapped a button nothing existed: not a
-- row, not a reminder, nothing to find. A restart discarded it without trace.
--
--     it keeps doing this peter, the bot has issue, then misses
--     can we fix this so never happens again?
--     — Bridge, 4 October 2026, 11:55pm
--
-- He is describing a real pattern. ₹902,460 went that way on 11 September —
-- four payments in an unanswered prompt, lost to a restart, discovered when
-- the client asked why their trade had not closed. ₹250,000 went the same way
-- on 4 October and was two hours from being missed. Six slips from 28 and
-- 30 September, ₹1,439,018, are still unaccounted for and all of them passed
-- through this door.
--
-- So the door is the same door. Every held payment is a row, from the moment
-- it is read, whoever is being asked about it.
--
-- WHAT CHANGES
--
-- The account becomes optional. A payment held because two vendors share an
-- account HAS one — that is the whole point of that case. A payment held
-- because the name was not recognised does not, and inventing one would be
-- the guess this table exists to avoid. What is known instead is what the
-- client typed, so that is what is kept.
--
-- Idempotent. Safe to run twice.

BEGIN;

ALTER TABLE held_payments
    ADD COLUMN IF NOT EXISTS typed_beneficiary TEXT;

-- The name as the client wrote it: "SUPER TRAD", "royal trading". Not cleaned
-- up, because the point of showing it to the Bridge is to show him what the
-- bot was given, not the bot's opinion of it.
COMMENT ON COLUMN held_payments.typed_beneficiary IS
    'The beneficiary exactly as the client typed it, when it matched nothing.';

ALTER TABLE held_payments ALTER COLUMN account_number DROP NOT NULL;
ALTER TABLE held_payments ALTER COLUMN ifsc           DROP NOT NULL;

-- One of the two must be known, or the row says nothing about where the money
-- went and there is no question to put to anybody.
ALTER TABLE held_payments DROP CONSTRAINT IF EXISTS held_payments_knows_something;
ALTER TABLE held_payments ADD CONSTRAINT held_payments_knows_something
    CHECK (account_number IS NOT NULL OR typed_beneficiary IS NOT NULL);

COMMIT;

-- Check:
--
--   SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS waiting,
--          count(*) FILTER (WHERE account_number IS NULL) AS unmatched_kind,
--          count(*) AS total
--   FROM held_payments;
--
-- Existing rows are untouched: they all have an account number and no typed
-- name, which satisfies the new constraint. Nothing is held by this
-- migration, and the bot behaves exactly as before until the next payment the
-- client's account list cannot place.
