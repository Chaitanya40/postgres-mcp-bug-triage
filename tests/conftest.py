"""Shared fixtures: a real Postgres (via pgserver) and the MCP server running under uvicorn."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pgserver
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-token-not-a-secret"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def postgres(tmp_path_factory):
    """Start a throwaway Postgres 16, load the schema, and create the read-only role."""
    server = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="stop")
    admin_uri = server.get_uri()
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS billing")
        conn.execute("DROP ROLE IF EXISTS triage_agent")
        conn.execute("CREATE DATABASE billing")

    billing_admin = admin_uri.replace("/postgres?", "/billing?")
    with psycopg.connect(billing_admin, autocommit=True) as conn:
        conn.execute((ROOT / "sql" / "01_schema.sql").read_text())
        conn.execute((ROOT / "sql" / "02_readonly_role.sql").read_text())

    yield {
        "admin": billing_admin,
        "agent": billing_admin.replace("postgres:@", "triage_agent:change-me@"),
    }
    server.cleanup()


@pytest.fixture(scope="session")
def mcp_server(postgres, tmp_path_factory):
    """Run the MCP server exactly as it runs on EC2: uvicorn serving triage_mcp.server:app."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    log_path = tmp_path_factory.mktemp("logs") / "server.log"
    env = {
        **os.environ,
        "TRIAGE_DB_URL": postgres["agent"],
        "TRIAGE_MCP_TOKEN": TOKEN,
        "TRIAGE_MCP_PUBLIC_URL": url,
        "TRIAGE_STATEMENT_TIMEOUT_MS": "1000",
    }
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "triage_mcp.server:app", "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    try:
        for _ in range(100):
            try:
                if httpx2.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                    break
            except httpx2.TransportError:
                time.sleep(0.1)
        else:
            raise RuntimeError(f"MCP server did not start:\n{log_path.read_text()}")
        yield {"url": url, "log": log_path}
    finally:
        proc.terminate()
        proc.wait(timeout=10)
