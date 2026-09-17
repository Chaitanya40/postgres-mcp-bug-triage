"""Start a local Postgres 16 loaded with the demo schema. Development only.

Usage:
    uv run python scripts/local_db.py          # start (idempotent) and print connection strings
    uv run python scripts/local_db.py --stop   # stop the server
"""

import sys
import tempfile
from pathlib import Path

import pgserver
import psycopg

ROOT = Path(__file__).resolve().parents[1]
PGDATA = Path(tempfile.gettempdir()) / "triage-mcp-pgdata"


def main() -> None:
    if "--stop" in sys.argv:
        pgserver.get_server(PGDATA, cleanup_mode="stop").cleanup()
        print("Stopped.")
        return

    server = pgserver.get_server(PGDATA, cleanup_mode=None)  # keep running after exit
    admin_uri = server.get_uri()
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = 'billing'").fetchone()
        if not exists:
            conn.execute("CREATE DATABASE billing")
            billing = server.get_uri("billing")
            with psycopg.connect(billing, autocommit=True) as billing_conn:
                billing_conn.execute((ROOT / "sql" / "01_schema.sql").read_text())
                billing_conn.execute((ROOT / "sql" / "02_readonly_role.sql").read_text())

    billing = server.get_uri("billing")
    print(f"Admin:        {billing}")
    print(f"Agent (RO):   {billing.replace('postgres:@', 'triage_agent:change-me@')}")


if __name__ == "__main__":
    main()
