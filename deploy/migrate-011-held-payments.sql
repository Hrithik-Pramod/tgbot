-- 011 — a payment waiting on the Bridge survives a restart.
--
-- WHY THIS TABLE EXISTS AT ALL
--
-- When two vendors collect into one bank account and both have an order
-- open, a slip naming that account cannot say which order it belongs to.
-- The information is not on the payment. From 1 October the bot stops
-- guessing and asks the BRIDGE — who knows which order is which, where the
-- client does not.
--
--     think the bot asks something, on my side ... it asks, which trade ...
--     i choose the right path for it to start on
--     — Bridge, 1 October 2026
--
-- Between the question and the answer the payment is not recorded anywhere.
-- That window is exactly how ₹902,460 was lost on 11 September 2026: four
-- payments sat in a pending conversation held in MemoryStorage, nobody
-- tapped a button, the process restarted, and they were gone without trace.
-- The first anyone knew was the client asking why their completed trade had
-- a balance.
--
-- So the wait is written down. A restart, a redeploy or a crash leaves every
-- held payment exactly where it was, and the Bridge is chased until it is
-- answered.
--
-- WHY THE UTR IS UNIQUE HERE TOO
--
-- payments.utr is globally unique because one bank transfer is one payment.
-- The same has to hold while it waits, or a client re-pasting an unanswered
-- slip would queue it twice and record it twice when the Bridge answered.
--
-- Idempotent. Safe to run twice.

BEGIN;

CREATE TABLE IF NOT EXISTS held_payments (
    id              BIGSERIAL PRIMARY KEY,

    client_id       BIGINT      NOT NULL REFERENCES parties(id),

    utr             TEXT        NOT NULL,
    amount_inr      NUMERIC(20, 2) NOT NULL CHECK (amount_inr > 0),

    -- The PHYSICAL account the slip named. Not an account row id: the whole
    -- reason this payment is held is that several rows share these two
    -- values, so pointing at one of them would be the guess we are avoiding.
    account_number  TEXT        NOT NULL,
    ifsc            TEXT        NOT NULL,

    -- Where it came from, so the answer can be acknowledged on the client's
    -- own message rather than in a vacuum.
    chat_id         BIGINT,
    message_id      BIGINT,

    asked_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when the Bridge has been reminded, so he is chased once a cycle
    -- rather than on every sweep.
    chased_at       TIMESTAMPTZ,

    resolved_at     TIMESTAMPTZ,
    resolved_trade_id BIGINT    REFERENCES trades(id),

    CONSTRAINT held_payments_utr_unique UNIQUE (utr)
);

-- The sweep reads this: everything still waiting, oldest first.
CREATE INDEX IF NOT EXISTS held_payments_waiting_idx
    ON held_payments (asked_at) WHERE resolved_at IS NULL;

COMMIT;

-- Check:
--
--   SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS waiting,
--          count(*) AS total
--   FROM held_payments;
--
-- Nothing is held by this migration. The table starts empty and the bot
-- behaves exactly as before until two vendors share an account.
