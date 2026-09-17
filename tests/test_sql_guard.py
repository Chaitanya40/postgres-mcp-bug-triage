import re

import pytest

from triage_mcp.sql_guard import UnsafeQueryError, validate_select, with_row_limit

TABLES = {"subscriptions", "payments", "events", "support_users"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT status, count(*) FROM subscriptions GROUP BY status",
        "WITH recent AS (SELECT * FROM payments WHERE status = 'failed') SELECT count(*) FROM recent",
        "SELECT user_id FROM subscriptions UNION SELECT user_id FROM events",
        "SELECT * FROM public.support_users WHERE id = find_user_id_by_email('a@example.com')",
        "SELECT 1 /* ; DROP TABLE payments */",
    ],
)
def test_allows_read_only_selects(sql):
    validate_select(sql, TABLES)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("DELETE FROM payments", "Only SELECT"),
        ("UPDATE subscriptions SET status = 'active'", "Only SELECT"),
        ("INSERT INTO events (user_id, event_type) VALUES (1, 'x')", "Only SELECT"),
        ("DROP TABLE payments", "Only SELECT"),
        ("COPY payments TO '/tmp/out.csv'", "Only SELECT"),
        ("SET default_transaction_read_only = off", "Only SELECT"),
        ("SELECT 1; DELETE FROM payments", "exactly one"),
        ("WITH gone AS (DELETE FROM payments RETURNING *) SELECT * FROM gone", "Delete is not allowed"),
        ("SELECT * INTO stolen FROM payments", "Into is not allowed"),
        ("SELECT * FROM subscriptions FOR UPDATE", "Lock is not allowed"),
        ("SELECT pg_sleep(60)", "pg_sleep() is not allowed"),
        ("SELECT pg_catalog.pg_read_file('/etc/passwd')", "pg_read_file() is not allowed"),
        ("SELECT set_config('statement_timeout', '0', false)", "set_config() is not allowed"),
        ("SELECT email FROM users", "Table users is not available"),
        ("SELECT * FROM pg_catalog.pg_authid", "is not available"),
        ("SELECT * FROM generate_series(1, 1000000000)", "Table functions"),
        ("SELEC * FROM payments", "Could not parse SQL"),
    ],
)
def test_rejects_unsafe_sql(sql, reason):
    with pytest.raises(UnsafeQueryError, match=re.escape(reason)):
        validate_select(sql, TABLES)


def test_row_limit_wraps_the_whole_query():
    query = validate_select("SELECT id FROM payments ORDER BY id LIMIT 10000", TABLES)
    assert with_row_limit(query, 50) == (
        "SELECT * FROM (SELECT id FROM payments ORDER BY id LIMIT 10000) AS agent_query LIMIT 51"
    )
