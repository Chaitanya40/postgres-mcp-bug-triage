"""Integration tests: real Postgres, real HTTP, real MCP client."""

import json
from contextlib import asynccontextmanager

import httpx2
import psycopg
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from conftest import TOKEN

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def connect(url: str):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            yield client


def error_text(result) -> str:
    assert result.is_error, result
    return result.content[0].text


async def test_requests_without_a_valid_token_are_rejected(mcp_server):
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        response = httpx2.post(mcp_server["url"], headers=headers, json={})
        assert response.status_code == 401


async def test_exposes_only_read_only_tools(mcp_server):
    async with connect(mcp_server["url"]) as client:
        tools = (await client.list_tools()).tools
    assert sorted(t.name for t in tools) == [
        "describe_table", "find_failed_payments", "find_user_by_email",
        "get_user_recent_activity", "list_tables", "run_select",
    ]
    assert all(t.annotations.read_only_hint for t in tools)


async def test_schema_discovery_includes_comments(mcp_server):
    async with connect(mcp_server["url"]) as client:
        tables = await client.call_tool("list_tables", {})
        columns = await client.call_tool("describe_table", {"table": "subscriptions"})
        hidden = await client.call_tool("describe_table", {"table": "users"})
    assert [t["name"] for t in tables.structured_content["result"]] == [
        "events", "payments", "subscriptions", "support_users",
    ]
    status = next(c for c in columns.structured_content["result"] if c["name"] == "status")
    assert "past_due" in status["description"]
    assert "Unknown table 'users'" in error_text(hidden)


async def test_investigation_tools_mask_pii(mcp_server):
    async with connect(mcp_server["url"]) as client:
        user = await client.call_tool("find_user_by_email", {"email": "maya.lindqvist@example.com"})
        activity = await client.call_tool(
            "get_user_recent_activity", {"user_id": 42, "since": "2026-09-01"}
        )
    assert user.structured_content == {
        "id": 42, "email_masked": "m***@example.com", "plan": "pro",
        "country": "IN", "created_at": "2026-01-17T03:00:00+00:00",
    }
    data = activity.structured_content
    assert data["subscription"]["status"] == "active"
    assert [e["event_type"] for e in data["events"]] == ["billing_page.viewed", "payment.failed"]
    assert data["events"][1]["payload"]["billing_email"] == "m***@example.com"
    assert "lindqvist" not in json.dumps(data)


async def test_failed_payments_reveal_the_planted_bug(mcp_server):
    async with connect(mcp_server["url"]) as client:
        result = await client.call_tool("find_failed_payments", {"since": "2026-08-01"})
    payments = result.structured_content["payments"]
    august = {p["subscription_status"] for p in payments if p["created_at"].startswith("2026-08")}
    september = {p["subscription_status"] for p in payments if p["created_at"].startswith("2026-09")}
    assert august == {"past_due"}
    assert september == {"active"}
    assert len(payments) == 15


async def test_run_select_measures_impact(mcp_server):
    sql = """
        SELECT date_trunc('month', p.created_at)::date AS month, s.status, count(*) AS failed_payments
        FROM payments p JOIN subscriptions s ON s.id = p.subscription_id
        WHERE p.status = 'failed'
        GROUP BY 1, 2 ORDER BY 1
    """
    async with connect(mcp_server["url"]) as client:
        result = await client.call_tool("run_select", {"sql": sql})
        truncated = await client.call_tool("run_select", {"sql": "SELECT id FROM payments", "max_rows": 3})
    assert result.structured_content["rows"] == [
        {"month": "2026-08-01", "status": "past_due", "failed_payments": 3},
        {"month": "2026-09-01", "status": "active", "failed_payments": 12},
    ]
    assert len(truncated.structured_content["rows"]) == 3
    assert truncated.structured_content["truncated"] is True


async def test_run_select_rejects_writes_and_times_out_slow_queries(mcp_server):
    async with connect(mcp_server["url"]) as client:
        write = await client.call_tool(
            "run_select", {"sql": "WITH x AS (DELETE FROM payments RETURNING id) SELECT * FROM x"}
        )
        slow = await client.call_tool(
            "run_select", {"sql": "SELECT count(*) FROM events a, events b, events c, events d"}
        )
    assert "Delete is not allowed" in error_text(write)
    assert "Query timed out" in error_text(slow)


async def test_database_role_blocks_writes_even_without_the_sql_guard(postgres):
    with psycopg.connect(postgres["agent"], autocommit=True) as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM payments")
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM payments")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT email FROM users")


async def test_every_query_is_audit_logged(mcp_server):
    async with connect(mcp_server["url"]) as client:
        await client.call_tool("run_select", {"sql": "SELECT count(*) FROM payments"})
    log = mcp_server["log"].read_text()
    assert '"tool": "run_select", "client": "triage-agent"' in log
