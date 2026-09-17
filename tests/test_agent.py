"""Exercise the agent's plumbing without an API key.

The Claude API is replaced by a scripted fake that replays a plausible investigation,
while every tool call still goes through the real MCP server and Postgres.
"""

import json

import httpx2
import pytest
from anthropic import AsyncAnthropic

from conftest import ROOT, TOKEN
from triage_mcp.agent import triage

pytestmark = pytest.mark.anyio

SCRIPTED_TOOL_CALLS = [
    ("find_user_by_email", {"email": "maya.lindqvist@example.com"}),
    ("get_user_recent_activity", {"user_id": 42, "since": "2026-08-01"}),
    ("find_failed_payments", {"since": "2026-08-01"}),
    ("run_select", {"sql": (
        "SELECT date_trunc('month', p.created_at)::date AS month, s.status, count(*) AS failed "
        "FROM payments p JOIN subscriptions s ON s.id = p.subscription_id "
        "WHERE p.status = 'failed' GROUP BY 1, 2 ORDER BY 1"
    )}),
]

FINAL_REPORT = {
    "ticket_id": "187",
    "severity": "sev2",
    "category": "bug",
    "summary": "Failed September renewals leave subscriptions active with no dunning email.",
    "likely_cause": "Since 2026-09-01 the renewal job records failed charges but skips the past_due transition.",
    "affected_customers": 12,
    "evidence": [{"tool": "run_select", "query_summary": "failed payments by month and status",
                  "finding": "Aug: 3 failed, all past_due. Sep: 12 failed, all still active."}],
    "action_items": [{"team": "billing", "title": "Restore past_due transition on failed renewals",
                      "detail": "Compare renewal job changes deployed around 2026-09-01."}],
}


def message(content, stop_reason):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


class ScriptedClaude:
    def __init__(self):
        self.requests: list[dict] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        turn = len(self.requests) - 1
        if turn < len(SCRIPTED_TOOL_CALLS):
            name, args = SCRIPTED_TOOL_CALLS[turn]
            block = {"type": "tool_use", "id": f"toolu_{turn}", "name": name, "input": args}
            return httpx2.Response(200, json=message([block], "tool_use"))
        return httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(FINAL_REPORT)}], "end_turn"))


async def test_agent_investigates_through_mcp_and_returns_structured_report(mcp_server, monkeypatch):
    monkeypatch.setenv("TRIAGE_MCP_URL", mcp_server["url"])
    monkeypatch.setenv("TRIAGE_MCP_TOKEN", TOKEN)
    fake_api = ScriptedClaude()
    claude = AsyncAnthropic(api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake_api)))

    ticket = json.loads((ROOT / "examples" / "ticket.json").read_text())
    report = await triage(ticket, claude=claude)

    assert report.affected_customers == 12
    first = fake_api.requests[0]
    assert first["model"] == "claude-opus-5"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert {t["name"] for t in first["tools"]} == {
        "list_tables", "describe_table", "run_select",
        "find_user_by_email", "get_user_recent_activity", "find_failed_payments",
    }

    # The tool results Claude saw came from the real server, with PII masked.
    tool_results = [
        block["content"][0]["text"]
        for msg in fake_api.requests[-1]["messages"] if msg["role"] == "user" and isinstance(msg["content"], list)
        for block in msg["content"] if block["type"] == "tool_result"
    ]
    assert len(tool_results) == 4
    assert json.loads(tool_results[-1])["rows"][-1] == {"month": "2026-09-01", "status": "active", "failed": 12}
    assert "m***@example.com" in tool_results[0]
    assert not any("lindqvist" in text for text in tool_results)
