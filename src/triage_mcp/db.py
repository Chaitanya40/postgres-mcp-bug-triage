"""Read-only query execution against the Postgres read replica."""

import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from triage_mcp.masking import to_safe_json


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    truncated: bool
    duration_ms: int


class Database:
    def __init__(self, dsn: str, statement_timeout_ms: int = 5000):
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms

    def query(self, sql: str, params: tuple | None = None, max_rows: int = 50) -> QueryResult:
        started = time.monotonic()
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as conn:
            conn.read_only = True  # BEGIN READ ONLY, independent of the role's defaults
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (str(self.statement_timeout_ms),),
                )
                cursor = conn.execute(sql, params)
                rows = cursor.fetchmany(max_rows + 1)
                columns = [col.name for col in cursor.description or []]
        return QueryResult(
            columns=columns,
            rows=[to_safe_json(row) for row in rows[:max_rows]],
            truncated=len(rows) > max_rows,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
