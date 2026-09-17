-- A dedicated, least-privilege role for the triage agent.
-- Run on the PRIMARY as an admin user. Roles and grants replicate to the read replica.
-- Store the real password in AWS Secrets Manager, not in this file.

CREATE ROLE triage_agent LOGIN PASSWORD 'change-me' CONNECTION LIMIT 10;

-- Session defaults, applied every time the role connects.
ALTER ROLE triage_agent SET default_transaction_read_only = on;
ALTER ROLE triage_agent SET statement_timeout = '5s';
ALTER ROLE triage_agent SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE triage_agent SET timezone = 'UTC';

-- Only the objects the agent needs. No access to the raw users table.
GRANT USAGE ON SCHEMA public TO triage_agent;
GRANT SELECT ON subscriptions, payments, events, support_users TO triage_agent;
GRANT EXECUTE ON FUNCTION find_user_id_by_email(text) TO triage_agent;
