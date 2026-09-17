"""Parser-based validation for agent-written SQL."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class UnsafeQueryError(ValueError):
    """The query is not a single, read-only SELECT over allowed tables."""


# Nodes that can write or lock, even when nested inside a SELECT or CTE.
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Into, exp.Lock)

# Server-admin and side-effect functions: pg_sleep, pg_read_file, lo_import, dblink, set_config...
BLOCKED_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink", "set_config")


def validate_select(sql: str, allowed_tables: set[str]) -> exp.Query:
    """Parse `sql` as Postgres and return the AST if it is safe to run."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except ParseError as e:
        raise UnsafeQueryError(f"Could not parse SQL: {e}") from None

    if len(statements) != 1:
        raise UnsafeQueryError("Send exactly one SQL statement.")
    query = statements[0]
    if not isinstance(query, (exp.Select, exp.SetOperation)):
        raise UnsafeQueryError("Only SELECT queries (including WITH ... SELECT) are allowed.")

    for node in query.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeQueryError(f"{type(node).__name__} is not allowed in a read-only query.")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name.startswith(BLOCKED_FUNCTION_PREFIXES):
                raise UnsafeQueryError(f"Function {name}() is not allowed.")

    cte_names = {cte.alias_or_name for cte in query.find_all(exp.CTE)}
    for table in query.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("Table functions are not allowed in FROM.")
        if not table.db and table.name in cte_names:
            continue
        if table.db not in ("", "public") or table.name not in allowed_tables:
            allowed = ", ".join(sorted(allowed_tables))
            raise UnsafeQueryError(f"Table {table.sql()} is not available. Allowed: {allowed}.")
    return query


def with_row_limit(query: exp.Query, max_rows: int) -> str:
    """Wrap the query so Postgres never returns more than max_rows + 1 rows.

    The extra row tells the caller that results were truncated.
    """
    wrapped = exp.select("*").from_(query.subquery("agent_query")).limit(max_rows + 1)
    return wrapped.sql(dialect="postgres")
