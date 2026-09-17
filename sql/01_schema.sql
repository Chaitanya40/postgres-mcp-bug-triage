-- Fictional SaaS billing schema with seed data and a planted bug.
-- Run as the database owner on the PRIMARY. Everything here replicates to the read replica.

CREATE TABLE users (
    id          bigint PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    plan        text NOT NULL,
    country     text NOT NULL,
    created_at  timestamptz NOT NULL
);
COMMENT ON TABLE users IS 'Customer accounts. Contains PII: agents use support_users instead.';

CREATE TABLE subscriptions (
    id                  bigint PRIMARY KEY,
    user_id             bigint NOT NULL REFERENCES users (id),
    plan                text NOT NULL,
    status              text NOT NULL,
    current_period_end  timestamptz NOT NULL,
    updated_at          timestamptz NOT NULL
);
COMMENT ON TABLE subscriptions IS 'One row per paid subscription.';
COMMENT ON COLUMN subscriptions.status IS 'active | past_due | canceled. A failed renewal should move active to past_due.';

CREATE TABLE payments (
    id               bigint PRIMARY KEY,
    subscription_id  bigint NOT NULL REFERENCES subscriptions (id),
    amount_cents     integer NOT NULL,
    currency         text NOT NULL,
    status           text NOT NULL,
    failure_code     text,
    created_at       timestamptz NOT NULL
);
COMMENT ON TABLE payments IS 'Renewal charge attempts, one per billing cycle.';
COMMENT ON COLUMN payments.status IS 'succeeded | failed | refunded';
COMMENT ON COLUMN payments.failure_code IS 'Processor decline code when status = failed, e.g. card_declined, expired_card.';

CREATE TABLE events (
    id          bigserial PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users (id),
    event_type  text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL
);
COMMENT ON TABLE events IS 'Append-only audit log of billing and lifecycle events.';
COMMENT ON COLUMN events.event_type IS 'e.g. payment.succeeded, payment.failed, subscription.past_due, dunning_email.sent';

-- A PII-safe view for support tooling. Views run with the owner's privileges,
-- so a role can read this without any access to the users table.
CREATE VIEW support_users AS
SELECT id,
       left(email, 1) || '***@' || split_part(email, '@', 2) AS email_masked,
       plan,
       country,
       created_at
FROM users;
COMMENT ON VIEW support_users IS 'Customer accounts with PII masked. Use this instead of users.';

-- Resolve an email to an account ID without granting read access to emails.
CREATE FUNCTION find_user_id_by_email(p_email text) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public
AS $$ SELECT id FROM users WHERE lower(email) = lower(p_email) $$;
REVOKE ALL ON FUNCTION find_user_id_by_email(text) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Seed data: 120 Pro customers billed monthly on the 3rd.
-- ---------------------------------------------------------------------------
INSERT INTO users
SELECT i,
       CASE WHEN i = 42 THEN 'maya.lindqvist@example.com' ELSE 'customer' || i || '@example.com' END,
       CASE WHEN i = 42 THEN 'Maya Lindqvist' ELSE 'Customer ' || i END,
       'pro',
       (ARRAY['US', 'DE', 'IN', 'BR', 'SE'])[1 + i % 5],
       timestamptz '2026-01-15 09:00+00' + (i || ' hours')::interval
FROM generate_series(1, 120) AS i;

INSERT INTO subscriptions
SELECT i, i, 'pro', 'active', timestamptz '2026-10-03 00:00+00', timestamptz '2026-09-03 02:00+00'
FROM generate_series(1, 120) AS i;

-- June to August renewals. Three August renewals failed and were handled correctly.
INSERT INTO payments
SELECT m * 1000 + i, i, 2900, 'USD',
       CASE WHEN m = 8 AND i IN (5, 17, 29) THEN 'failed' ELSE 'succeeded' END,
       CASE WHEN m = 8 AND i IN (5, 17, 29) THEN 'card_declined' END,
       make_timestamptz(2026, m, 3, 2, 0, 0, 'UTC')
FROM generate_series(1, 120) AS i, generate_series(6, 8) AS m;

UPDATE subscriptions SET status = 'past_due', updated_at = timestamptz '2026-08-03 02:05+00'
WHERE id IN (5, 17, 29);

INSERT INTO events (user_id, event_type, payload, created_at)
SELECT i, e.event_type, jsonb_build_object('payment_id', 8000 + i, 'billing_email', 'customer' || i || '@example.com'),
       timestamptz '2026-08-03 02:00+00' + e.offset_minutes * interval '1 minute'
FROM unnest(ARRAY[5, 17, 29]) AS i,
     (VALUES ('payment.failed', 0), ('subscription.past_due', 1), ('dunning_email.sent', 2)) AS e (event_type, offset_minutes);

-- September renewals. THE PLANTED BUG: from 2026-09-01 the renewal job records
-- failed charges but never moves the subscription to past_due or sends dunning email.
INSERT INTO payments
SELECT 9000 + i, i, 2900, 'USD',
       CASE WHEN i % 10 = 2 THEN 'failed' ELSE 'succeeded' END,
       CASE WHEN i % 10 = 2 THEN 'expired_card' END,
       timestamptz '2026-09-03 02:00+00'
FROM generate_series(1, 120) AS i;

INSERT INTO events (user_id, event_type, payload, created_at)
SELECT i,
       CASE WHEN i % 10 = 2 THEN 'payment.failed' ELSE 'payment.succeeded' END,
       jsonb_build_object('payment_id', 9000 + i, 'billing_email',
           CASE WHEN i = 42 THEN 'maya.lindqvist@example.com' ELSE 'customer' || i || '@example.com' END),
       timestamptz '2026-09-03 02:00+00'
FROM generate_series(1, 120) AS i;

INSERT INTO events (user_id, event_type, payload, created_at)
VALUES (42, 'billing_page.viewed', '{"banner": "payment_failed"}', timestamptz '2026-09-12 16:40+00');
