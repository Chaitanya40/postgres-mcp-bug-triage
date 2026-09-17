"""Read-only Postgres MCP server for automated bug triage."""

import hmac
import json
import logging
import os
from datetime import date
from typing import Annotated, Any

import psycopg
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from triage_mcp.db import Database, QueryResult
from triage_mcp.sql_guard import UnsafeQueryError, validate_select, with_row_limit

# One JSON line per query on stderr, ready for the CloudWatch agent or journald.
audit_log = logging.getLogger("triage_mcp.audit")
_audit_handler = logging.StreamHandler()
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s audit %(message)s"))
audit_log.addHandler(_audit_handler)
audit_log.setLevel(logging.INFO)
audit_log.propagate = False

# --- Configuration (all from the environment; nothing secret in code) ---

DB = Database(
    dsn=os.environ["TRIAGE_DB_URL"],
    statement_timeout_ms=int(os.environ.get("TRIAGE_STATEMENT_TIMEOUT_MS", "5000")),
)
PUBLIC_URL = os.environ.get("TRIAGE_MCP_PUBLIC_URL", "http://127.0.0.1:8000/mcp")
ALLOWED_HOSTS = os.environ.get("TRIAGE_MCP_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*").split(",")
ALLOWED_TABLES = {"subscriptions", "payments", "events", "support_users"}
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


# --- Authentication ---

class StaticTokenVerifier(TokenVerifier):
    """Accepts one shared bearer token. Swap for JWT validation against your IdP in production."""

    def __init__(self, expected_token: str):
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token, self.expected_token):
            return AccessToken(token=token, client_id="triage-agent", scopes=["db:read"])
        return None


mcp = MCPServer(
    "triage-db",
    instructions=(
        "Read-only access to the billing database replica for bug triage. "
        "Start with the purpose-built tools; use run_select only when they cannot answer. "
        "Data may lag the primary by a few seconds."
    ),
    token_verifier=StaticTokenVerifier(os.environ["TRIAGE_MCP_TOKEN"]),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://auth.example.com"),  # your IdP in production
        resource_server_url=AnyHttpUrl(PUBLIC_URL),
        required_scopes=["db:read"],
        validate_token_resource=False,  # a static token carries no audience claim
    ),
)


def run_query(tool: str, sql: str, params: tuple | None = None, max_rows: int = 50) -> QueryResult:
    """Execute on the replica, write an audit line, and turn DB errors into model-readable errors."""
    token = get_access_token()
    entry: dict[str, Any] = {
        "tool": tool, "client": token.client_id if token else None, "sql": " ".join(sql.split()),
    }
    try:
        result = DB.query(sql, params, max_rows=max_rows)
    except psycopg.errors.QueryCanceled:
        audit_log.warning(json.dumps({**entry, "error": "timeout"}))
        raise ToolError("Query timed out. Add filters (user_id, created_at range) or aggregate.")
    except psycopg.Error as e:
        message = e.diag.message_primary or type(e).__name__
        audit_log.warning(json.dumps({**entry, "error": message}))
        raise ToolError(f"Database error: {message}")
    audit_log.info(json.dumps({**entry, "rows": len(result.rows), "ms": result.duration_ms}))
    return result


# --- Schema discovery ---

class TableInfo(BaseModel):
    name: str
    description: str | None


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    description: str | None


@mcp.tool(annotations=READ_ONLY)
def list_tables() -> list[TableInfo]:
    """List the tables and views you can query, with a short description of each."""
    result = run_query(
        "list_tables",
        """SELECT c.relname AS name, obj_description(c.oid, 'pg_class') AS description
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'public' AND c.relname = ANY(%s) ORDER BY c.relname""",
        (sorted(ALLOWED_TABLES),),
    )
    return [TableInfo(**row) for row in result.rows]


@mcp.tool(annotations=READ_ONLY)
def describe_table(table: str) -> list[ColumnInfo]:
    """Describe the columns of one table or view, including what each column means."""
    if table not in ALLOWED_TABLES:
        raise ToolError(f"Unknown table '{table}'. Call list_tables to see what is available.")
    result = run_query(
        "describe_table",
        """SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
                  NOT a.attnotnull AS nullable, col_description(a.attrelid, a.attnum) AS description
           FROM pg_attribute a
           WHERE a.attrelid = to_regclass('public.' || %s) AND a.attnum > 0 AND NOT a.attisdropped
           ORDER BY a.attnum""",
        (table,),
        max_rows=200,
    )
    return [ColumnInfo(**row) for row in result.rows]


# --- Guarded ad-hoc SQL ---

class SelectResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool = Field(description="True if more rows matched than max_rows")


@mcp.tool(annotations=READ_ONLY)
def run_select(
    sql: Annotated[str, Field(max_length=4000, description="One PostgreSQL SELECT statement")],
    max_rows: Annotated[int, Field(ge=1, le=200)] = 50,
) -> SelectResult:
    """Run one read-only SELECT against the billing replica.

    Use for questions the other tools cannot answer, such as counting how many
    customers are affected. Prefer aggregates (COUNT, GROUP BY) over raw rows.
    Only these tables are available: subscriptions, payments, events, support_users.
    """
    try:
        query = validate_select(sql, ALLOWED_TABLES)
    except UnsafeQueryError as e:
        token = get_access_token()
        client = token.client_id if token else None
        audit_log.warning(json.dumps({"tool": "run_select", "client": client, "sql": sql, "rejected": str(e)}))
        raise ToolError(str(e))
    result = run_query("run_select", with_row_limit(query, max_rows), max_rows=max_rows)
    return SelectResult(columns=result.columns, rows=result.rows, truncated=result.truncated)


# --- Purpose-built investigation tools ---

class UserSummary(BaseModel):
    id: int
    email_masked: str
    plan: str
    country: str
    created_at: str


class UserActivity(BaseModel):
    subscription: dict[str, Any] | None
    events: list[dict[str, Any]]
    truncated: bool


class FailedPayments(BaseModel):
    payments: list[dict[str, Any]]
    truncated: bool


@mcp.tool(annotations=READ_ONLY)
def find_user_by_email(
    email: Annotated[str, Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+$")],
) -> UserSummary:
    """Look up a customer account from the email address on a bug report."""
    result = run_query(
        "find_user_by_email",
        "SELECT * FROM support_users WHERE id = find_user_id_by_email(%s)",
        (email,),
        max_rows=1,
    )
    if not result.rows:
        raise ToolError("No account uses that email. Ask the reporter for their account ID.")
    return UserSummary(**result.rows[0])


@mcp.tool(annotations=READ_ONLY)
def get_user_recent_activity(
    user_id: int,
    since: date,
    max_events: Annotated[int, Field(ge=1, le=100)] = 25,
) -> UserActivity:
    """Get a customer's subscription and their audit-log events since a date, newest first."""
    subscription = run_query(
        "get_user_recent_activity",
        "SELECT id, plan, status, current_period_end, updated_at FROM subscriptions WHERE user_id = %s",
        (user_id,),
        max_rows=1,
    )
    events = run_query(
        "get_user_recent_activity",
        """SELECT event_type, payload, created_at FROM events
           WHERE user_id = %s AND created_at >= %s ORDER BY created_at DESC""",
        (user_id, since),
        max_rows=max_events,
    )
    return UserActivity(
        subscription=subscription.rows[0] if subscription.rows else None,
        events=events.rows,
        truncated=events.truncated,
    )


@mcp.tool(annotations=READ_ONLY)
def find_failed_payments(
    since: date,
    user_id: int | None = None,
    max_rows: Annotated[int, Field(ge=1, le=100)] = 25,
) -> FailedPayments:
    """Find failed renewal charges since a date, with the subscription's current status.

    Pass user_id for one customer, or omit it to see failures across all customers.
    """
    result = run_query(
        "find_failed_payments",
        """SELECT p.id AS payment_id, s.user_id, p.amount_cents, p.currency, p.failure_code,
                  p.created_at, s.status AS subscription_status
           FROM payments p JOIN subscriptions s ON s.id = p.subscription_id
           WHERE p.status = 'failed' AND p.created_at >= %s
             AND (%s::bigint IS NULL OR s.user_id = %s::bigint)
           ORDER BY p.created_at DESC, p.id""",
        (since, user_id, user_id),
        max_rows=max_rows,
    )
    return FailedPayments(payments=result.rows, truncated=result.truncated)


# --- HTTP app ---

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(allowed_hosts=ALLOWED_HOSTS),
)
