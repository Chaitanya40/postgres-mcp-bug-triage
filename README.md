# triage-mcp

A read-only Postgres MCP server and a Claude-powered triage agent, from the tutorial
"Build a Read-Only Postgres MCP Server for Automated Bug Triage".

- `sql/01_schema.sql`: fictional SaaS billing schema, seed data, and a planted bug
- `sql/02_readonly_role.sql`: the least-privilege `triage_agent` role
- `src/triage_mcp/sql_guard.py`: parser-based SELECT validation (sqlglot)
- `src/triage_mcp/db.py`: read-only, time-boxed query execution (psycopg)
- `src/triage_mcp/masking.py`: PII scrubbing for every returned value
- `src/triage_mcp/server.py`: the MCP server (streamable HTTP, bearer auth, audit log)
- `src/triage_mcp/agent.py`: the triage agent (Claude tool runner + MCP client)
- `examples/ticket.json`: a Microsoft List item as returned by Microsoft Graph

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/) 0.12
- No Postgres install needed for local development: `pgserver` ships Postgres 16 binaries
- Node.js 22.19+ only if you want to use MCP Inspector
- An Anthropic API key only to run the agent against the real API (tests don't need one)

Pinned versions: `mcp` 2.2.0, `anthropic` 1.6.0, `psycopg` 3.3.5, `sqlglot` 30.18.0,
`uvicorn` 0.53.0, `pgserver` 0.1.4 (PostgreSQL 16.2), `pytest` 9.1.1.

## Run the Tests

```bash
uv sync
uv run pytest
```

The tests start a throwaway Postgres, load the schema and role, run the MCP server under
uvicorn, and call it over HTTP with the MCP Python client. The agent test replaces the
Claude API with a scripted fake, so no API key is needed.

## Run Locally

```bash
# 1. Start a local Postgres 16 with the demo data (prints connection strings)
uv run python scripts/local_db.py

# 2. Start the MCP server as the read-only role
export TRIAGE_DB_URL='postgresql://triage_agent:change-me@/billing?host=<socket dir printed above>'
export TRIAGE_MCP_TOKEN='local-dev-token'
uv run uvicorn triage_mcp.server:app --host 127.0.0.1 --port 8000

# 3. In another terminal, call a tool with MCP Inspector
npx @modelcontextprotocol/inspector --cli http://127.0.0.1:8000/mcp --transport http \
  --header "Authorization: Bearer local-dev-token" --method tools/list

# 4. Triage the example ticket (needs ANTHROPIC_API_KEY)
export TRIAGE_MCP_URL='http://127.0.0.1:8000/mcp'
uv run triage-agent examples/ticket.json

# Stop the local database when you're done
uv run python scripts/local_db.py --stop
```

## Configuration

| Variable | Used by | Purpose |
| --- | --- | --- |
| `TRIAGE_DB_URL` | server | Connection string for the `triage_agent` role on the read replica |
| `TRIAGE_MCP_TOKEN` | server, agent | Shared bearer token (replace with IdP-issued JWTs in production) |
| `TRIAGE_MCP_PUBLIC_URL` | server | The URL clients use to reach `/mcp` |
| `TRIAGE_MCP_ALLOWED_HOSTS` | server | Comma-separated `Host` header allowlist, e.g. `10.0.1.25:*` |
| `TRIAGE_STATEMENT_TIMEOUT_MS` | server | Per-query timeout (default 5000) |
| `TRIAGE_MCP_URL` | agent | Where the agent reaches the MCP server |
| `ANTHROPIC_API_KEY` | agent | Claude API credentials |

The seed data, customer names, and schema are fictional.

## Deployment Checklist (AWS)

- Run `sql/01_schema.sql` (your real schema, views, and functions) and `sql/02_readonly_role.sql` on the
  primary; roles and grants replicate to RDS read replicas. Keep the role password in AWS Secrets Manager.
- Point `TRIAGE_DB_URL` at the read replica endpoint with `sslmode=verify-full` and the RDS CA bundle.
- Run the server on EC2 in a private subnet: `uvicorn triage_mcp.server:app --host 0.0.0.0 --port 8000`.
  Allow port 8000 only from the agent's security group, and 5432 on the replica only from the server.
- Terminate TLS at an internal load balancer and run uvicorn with `--proxy-headers --forwarded-allow-ips=<lb address>`.
  Set `TRIAGE_MCP_PUBLIC_URL` and `TRIAGE_MCP_ALLOWED_HOSTS` to the internal hostname.
- Replace `StaticTokenVerifier` with JWT validation against your identity provider (check audience and expiry).
- Ship stderr (JSON audit lines prefixed with `audit`) to CloudWatch Logs and alert on `rejected` and `timeout` entries.
- Monitor the replica's `ReplicaLag` metric; results can trail the primary.
- Under load, replace per-call connections with `psycopg_pool`, sized below the role's `CONNECTION LIMIT 10`.

## Troubleshooting

- `No module named 'mcp.server.fastmcp'`: `mcp` 2.x renamed `FastMCP` to `MCPServer` (`from mcp.server import MCPServer`).
- `421 Misdirected Request`: the request's `Host` header is not in `TRIAGE_MCP_ALLOWED_HOSTS`.
- `401 Unauthorized`: the `Authorization: Bearer <token>` header is missing or doesn't match `TRIAGE_MCP_TOKEN`.
- Timestamps in a non-UTC offset: make sure the role has `timezone = 'UTC'` (set in `02_readonly_role.sql`).
- Audit lines wrapped across several lines: `mcp[cli]` pulls in `rich`, whose log handler wraps long messages.
  The audit logger uses its own `StreamHandler` with `propagate = False` to keep one JSON line per query.
- `Query timed out` from `run_select`: add filters (`user_id`, a `created_at` range) or aggregate; the default
  timeout is 5 seconds (`TRIAGE_STATEMENT_TIMEOUT_MS`).
