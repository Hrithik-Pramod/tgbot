-- 009 — a trade keeps the vendor name it was done under.
--
-- WHY
--
-- bot/labels.py rewrites every active party's label from its Telegram group
-- title every 30 minutes. Labels are therefore not stable, and every screen
-- that names a vendor reads it through a live join:
--
--     JOIN parties s ON s.id = t.supplier_id   ->   s.label
--
-- So renaming a group renames that vendor on every trade they have ever
-- done, back to the first. A settled deal reads under a name it was never
-- struck under, and a reconciliation against a bank statement or an invoice
-- issued at the time stops lining up.
--
-- Bridge, 19 September 2026: a trade should keep the name it was done under.
--
-- The rate has been snapshotted on the trade since the first build for
-- exactly this reason — "a later rate change must not silently rewrite
-- history". The name is no different. Both are terms of a deal that has
-- already happened.
--
-- BACKFILL
--
-- Existing trades take the label the party carries now. That is not the name
-- they were necessarily done under — that name is gone, and inventing one
-- would be worse than admitting it. What the backfill buys is that from this
-- point on the name is pinned and cannot drift again.
--
-- Where a rename is already known, party.renamed entries in the audit log
-- hold the old value and a trade can be corrected by hand afterwards.
--
-- Idempotent. Safe to run twice.

BEGIN;

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS supplier_label_at_trade TEXT;

UPDATE trades t
SET    supplier_label_at_trade = s.label
FROM   parties s
WHERE  s.id = t.supplier_id
  AND  t.supplier_label_at_trade IS NULL;

COMMIT;

-- Check: every trade should now carry a name, and it should match the live
-- label until the next rename.
--
--   SELECT count(*) FILTER (WHERE supplier_label_at_trade IS NULL) AS unpinned,
--          count(*) AS total
--   FROM trades;
